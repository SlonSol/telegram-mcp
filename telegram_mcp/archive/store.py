"""SQLite archive of messages, edits and deletion marks.

Rows are never removed. Media files may be pruned when the disk cap is hit;
the text row stays. Deletes in private chats and basic groups arrive without
a chat id because those message ids are unique across the account. Channel
and supergroup deletes include the channel id and are matched on
``(chat_id, message_id)``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_DEFAULT_MEDIA_MODE = "photos"
_MEDIA_MODES = {"off", "photos", "all"}
_DEFAULT_MAX_FILE_BYTES = 1_500_000
_DEFAULT_TOTAL_BYTES = 512 * 1024 * 1024

_store_lock = threading.Lock()
_store: Optional["ArchiveStore"] = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _data_dir() -> Path:
    raw = os.getenv("TELEGRAM_DATA_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path("data")


def archive_db_path() -> Path:
    raw = os.getenv("TELEGRAM_ARCHIVE_DB", "").strip()
    if raw:
        return Path(raw)
    return _data_dir() / "archive.sqlite"


def archive_media_dir() -> Path:
    raw = os.getenv("TELEGRAM_ARCHIVE_MEDIA_DIR", "").strip()
    if raw:
        return Path(raw)
    return _data_dir() / "archive_media"


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"Invalid {name} '{raw}'. Expected a positive number of bytes.") from None
    if value <= 0:
        raise SystemExit(f"Invalid {name} '{raw}'. Expected a positive number of bytes.")
    return value


def media_mode() -> str:
    raw = os.getenv("TELEGRAM_ARCHIVE_MEDIA", _DEFAULT_MEDIA_MODE).strip().lower()
    if raw not in _MEDIA_MODES:
        accepted = ", ".join(sorted(_MEDIA_MODES))
        raise SystemExit(f"Invalid TELEGRAM_ARCHIVE_MEDIA '{raw}'. Expected one of: {accepted}.")
    return raw


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class ArchiveStore:
    """One process-wide SQLite file. A lock serializes writers."""

    def __init__(
        self,
        db_path: Path,
        media_dir: Path,
        *,
        mode: str,
        max_file_bytes: int,
        total_bytes: int,
    ):
        self.db_path = db_path
        self.media_dir = media_dir
        self.mode = mode
        self.max_file_bytes = max_file_bytes
        self.total_bytes = total_bytes
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        _chmod(self.db_path.parent, 0o700)
        _chmod(self.media_dir, 0o700)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        _chmod(self.db_path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                account TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                peer_type TEXT NOT NULL,
                chat_title TEXT,
                sender_id INTEGER,
                sender_name TEXT,
                sender_name_cf TEXT NOT NULL DEFAULT '',
                message_id INTEGER NOT NULL,
                date TEXT,
                text TEXT NOT NULL DEFAULT '',
                text_cf TEXT NOT NULL DEFAULT '',
                media_type TEXT NOT NULL DEFAULT '',
                media_path TEXT,
                media_bytes INTEGER NOT NULL DEFAULT 0,
                reply_to_msg_id INTEGER,
                forward_json TEXT,
                deleted INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(account, chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_deleted
                ON messages(account, deleted, deleted_at);
            CREATE INDEX IF NOT EXISTS idx_messages_lookup
                ON messages(account, message_id, peer_type);
            CREATE TABLE IF NOT EXISTS edits (
                id INTEGER PRIMARY KEY,
                message_row INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                media_type TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_edits_message ON edits(message_row, id);
            """)
        self._conn.commit()

    def wants_media(self, media_kind: str, size: Optional[int]) -> bool:
        if self.mode == "off" or not media_kind:
            return False
        if self.mode == "photos" and media_kind != "photo":
            return False
        if size is not None and size > self.max_file_bytes:
            return False
        return True

    def upsert(self, snap: dict[str, Any], *, source: str, media: Optional[bytes] = None) -> int:
        """Insert or update one message. Returns the row id.

        ``source`` is ``new`` or ``edit``. An edit of a message we have not
        seen before stores the edited text and has no earlier version. A
        change of text or media type keeps the previous values in ``edits``.
        """
        now = _utc_now()
        with self._lock:
            row = self._find(snap["account"], snap["chat_id"], snap["message_id"])
            if row is None:
                return self._insert(snap, now, media)
            self._record_edit_if_changed(row, snap, now)
            self._apply_update(row, snap, now, source, media)
            self._conn.commit()
            return int(row["id"])

    def mark_deleted(
        self,
        account: str,
        message_ids: list[int],
        *,
        channel_chat_id: Optional[int] = None,
    ) -> int:
        """Mark stored rows deleted. Does not delete them.

        ``channel_chat_id`` set: match that channel or supergroup.
        ``channel_chat_id`` omitted: match private chats and basic groups,
        where Telegram omits the chat id and message ids are account-wide.
        """
        if not message_ids:
            return 0
        now = _utc_now()
        marked = 0
        with self._lock:
            for message_id in message_ids:
                marked += self._mark_one(account, int(message_id), channel_chat_id, now)
            self._conn.commit()
        return marked

    def list_deleted(
        self,
        *,
        account: Optional[str],
        chat_id: Optional[int],
        sender: Optional[str],
        since: Optional[str],
        until: Optional[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses = ["deleted = 1"]
        params: list[Any] = []
        self._append_filters(clauses, params, account, chat_id, sender)
        if since:
            clauses.append("deleted_at >= ?")
            params.append(since)
        if until:
            clauses.append("deleted_at <= ?")
            params.append(until)
        params.append(limit)
        sql = (
            f"SELECT * FROM messages WHERE {' AND '.join(clauses)} "
            "ORDER BY deleted_at DESC, id DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._public_row(row) for row in rows]

    def search(
        self,
        query: str,
        *,
        account: Optional[str],
        chat_id: Optional[int],
        include_deleted: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        pattern = _like(query.casefold())
        clauses = ["text_cf LIKE ? ESCAPE '\\'"]
        params: list[Any] = [pattern]
        self._append_filters(clauses, params, account, chat_id, None)
        if not include_deleted:
            clauses.append("deleted = 0")
        params.append(limit)
        sql = (
            f"SELECT * FROM messages WHERE {' AND '.join(clauses)} "
            "ORDER BY date DESC, id DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._public_row(row) for row in rows]

    def edit_history(
        self, account: Optional[str], chat_id: int, message_id: int
    ) -> Optional[dict]:
        with self._lock:
            row = self._find_flexible(account, chat_id, message_id)
            if row is None:
                return None
            edits = self._conn.execute(
                "SELECT text, media_type, recorded_at FROM edits "
                "WHERE message_row = ? ORDER BY id ASC",
                (row["id"],),
            ).fetchall()
        return {
            "message": self._public_row(row),
            "previous_versions": [
                {
                    "text": item["text"],
                    "media_type": item["media_type"],
                    "recorded_at": item["recorded_at"],
                }
                for item in edits
            ],
        }

    def stats(self, account: Optional[str]) -> dict[str, Any]:
        clause = ""
        params: tuple[Any, ...] = ()
        if account:
            clause = " WHERE account = ?"
            params = (account,)
        with self._lock:
            messages = self._conn.execute(
                f"SELECT COUNT(*) FROM messages{clause}", params
            ).fetchone()
            deleted_sql = f"SELECT COUNT(*) FROM messages{clause}"
            deleted_sql += " AND deleted = 1" if clause else " WHERE deleted = 1"
            deleted = self._conn.execute(deleted_sql, params).fetchone()
            edits = self._conn.execute(
                "SELECT COUNT(*) FROM edits e JOIN messages m ON m.id = e.message_row"
                + (" WHERE m.account = ?" if account else ""),
                params,
            ).fetchone()
            media = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(media_bytes), 0) FROM messages "
                "WHERE media_path IS NOT NULL" + (" AND account = ?" if account else ""),
                params,
            ).fetchone()
        return {
            "messages": int(messages[0]),
            "deleted": int(deleted[0]),
            "edits": int(edits[0]),
            "media_files": int(media[0]),
            "media_bytes": int(media[1]),
            "media_mode": self.mode,
            "media_max_bytes": self.max_file_bytes,
            "media_total_cap_bytes": self.total_bytes,
            "db_path": str(self.db_path),
        }

    def _find(self, account: str, chat_id: int, message_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM messages WHERE account = ? AND chat_id = ? AND message_id = ?",
            (account, chat_id, message_id),
        ).fetchone()

    def _find_flexible(
        self, account: Optional[str], chat_id: int, message_id: int
    ) -> Optional[sqlite3.Row]:
        if account:
            return self._find(account, chat_id, message_id)
        return self._conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id, message_id),
        ).fetchone()

    def _insert(self, snap: dict[str, Any], now: str, media: Optional[bytes]) -> int:
        path, size = self._store_media(snap, media)
        cur = self._conn.execute(
            """
            INSERT INTO messages (
                account, chat_id, peer_type, chat_title, sender_id, sender_name,
                sender_name_cf, message_id, date, text, text_cf, media_type,
                media_path, media_bytes, reply_to_msg_id, forward_json, deleted,
                deleted_at, first_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
            """,
            (
                snap["account"],
                snap["chat_id"],
                snap["peer_type"],
                snap.get("chat_title"),
                snap.get("sender_id"),
                snap.get("sender_name"),
                (snap.get("sender_name") or "").casefold(),
                snap["message_id"],
                snap.get("date"),
                snap.get("text") or "",
                (snap.get("text") or "").casefold(),
                snap.get("media_type") or "",
                path,
                size,
                snap.get("reply_to_msg_id"),
                snap.get("forward_json"),
                now,
                now,
            ),
        )
        self._conn.commit()
        self._prune_media()
        return int(cur.lastrowid)

    def _record_edit_if_changed(self, row: sqlite3.Row, snap: dict[str, Any], now: str) -> None:
        new_text = snap.get("text") or ""
        new_media = snap.get("media_type") or ""
        if new_text == row["text"] and new_media == (row["media_type"] or ""):
            return
        self._conn.execute(
            "INSERT INTO edits (message_row, text, media_type, recorded_at) VALUES (?, ?, ?, ?)",
            (row["id"], row["text"], row["media_type"] or "", now),
        )

    def _apply_update(
        self,
        row: sqlite3.Row,
        snap: dict[str, Any],
        now: str,
        source: str,
        media: Optional[bytes],
    ) -> None:
        path = row["media_path"]
        size = int(row["media_bytes"] or 0)
        if media is not None:
            self._delete_file(path)
            path, size = self._store_media(snap, media)
        deleted = int(row["deleted"] or 0)
        deleted_at = row["deleted_at"]
        if source == "new":
            deleted = 0
            deleted_at = None
        self._conn.execute(
            """
            UPDATE messages SET
                peer_type = ?, chat_title = COALESCE(?, chat_title),
                sender_id = COALESCE(?, sender_id),
                sender_name = COALESCE(?, sender_name),
                sender_name_cf = COALESCE(?, sender_name_cf),
                date = COALESCE(?, date),
                text = ?, text_cf = ?, media_type = ?,
                media_path = ?, media_bytes = ?,
                reply_to_msg_id = COALESCE(?, reply_to_msg_id),
                forward_json = COALESCE(?, forward_json),
                deleted = ?, deleted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                snap["peer_type"],
                snap.get("chat_title"),
                snap.get("sender_id"),
                snap.get("sender_name"),
                (snap.get("sender_name") or "").casefold() or None,
                snap.get("date"),
                snap.get("text") or "",
                (snap.get("text") or "").casefold(),
                snap.get("media_type") or "",
                path,
                size,
                snap.get("reply_to_msg_id"),
                snap.get("forward_json"),
                deleted,
                deleted_at,
                now,
                row["id"],
            ),
        )
        if media is not None:
            self._prune_media()

    def _mark_one(
        self, account: str, message_id: int, channel_chat_id: Optional[int], now: str
    ) -> int:
        if channel_chat_id is None:
            cur = self._conn.execute(
                """
                UPDATE messages SET deleted = 1, deleted_at = ?
                WHERE account = ? AND message_id = ? AND peer_type IN ('user', 'chat')
                  AND deleted = 0
                """,
                (now, account, message_id),
            )
        else:
            cur = self._conn.execute(
                """
                UPDATE messages SET deleted = 1, deleted_at = ?
                WHERE account = ? AND chat_id = ? AND message_id = ?
                  AND peer_type = 'channel' AND deleted = 0
                """,
                (now, account, channel_chat_id, message_id),
            )
        return int(cur.rowcount)

    def _store_media(
        self, snap: dict[str, Any], media: Optional[bytes]
    ) -> tuple[Optional[str], int]:
        if not media:
            return None, 0
        if len(media) > self.max_file_bytes:
            return None, 0
        suffix = _suffix(snap.get("media_type") or "")
        name = f"{int(snap['chat_id'])}_{int(snap['message_id'])}{suffix}"
        path = self.media_dir / name
        temporary = path.with_name(path.name + ".part")
        temporary.write_bytes(media)
        _chmod(temporary, 0o600)
        temporary.replace(path)
        return str(path), len(media)

    def _prune_media(self) -> None:
        """Drop oldest media files until the cap fits. Text rows stay."""
        total = self._media_total()
        if total <= self.total_bytes:
            return
        rows = self._conn.execute("""
            SELECT id, media_path, media_bytes FROM messages
            WHERE media_path IS NOT NULL
            ORDER BY date ASC, id ASC
            """).fetchall()
        for row in rows:
            if total <= self.total_bytes:
                break
            self._delete_file(row["media_path"])
            total -= int(row["media_bytes"] or 0)
            self._conn.execute(
                "UPDATE messages SET media_path = NULL, media_bytes = 0 WHERE id = ?",
                (row["id"],),
            )
        self._conn.commit()

    def _media_total(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(media_bytes), 0) FROM messages WHERE media_path IS NOT NULL"
        ).fetchone()
        return int(row[0])

    def _append_filters(
        self,
        clauses: list[str],
        params: list[Any],
        account: Optional[str],
        chat_id: Optional[int],
        sender: Optional[str],
    ) -> None:
        if account:
            clauses.append("account = ?")
            params.append(account)
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if not sender:
            return
        stripped = sender.strip()
        if _is_int(stripped):
            clauses.append("sender_id = ?")
            params.append(int(stripped))
            return
        clauses.append("sender_name_cf LIKE ? ESCAPE '\\'")
        params.append(_like(stripped.casefold()))

    def _public_row(self, row: sqlite3.Row) -> dict[str, Any]:
        forward = None
        if row["forward_json"]:
            try:
                forward = json.loads(row["forward_json"])
            except json.JSONDecodeError:
                forward = None
        edits = self._conn.execute(
            "SELECT COUNT(*) FROM edits WHERE message_row = ?", (row["id"],)
        ).fetchone()
        return {
            "account": row["account"],
            "chat_id": row["chat_id"],
            "chat_title": row["chat_title"],
            "peer_type": row["peer_type"],
            "sender_id": row["sender_id"],
            "sender_name": row["sender_name"],
            "message_id": row["message_id"],
            "date": row["date"],
            "text": row["text"],
            "media_type": row["media_type"] or "",
            "media_saved": bool(row["media_path"]),
            "reply_to_msg_id": row["reply_to_msg_id"],
            "forward": forward,
            "deleted": bool(row["deleted"]),
            "deleted_at": row["deleted_at"],
            "edit_count": int(edits[0]),
        }

    @staticmethod
    def _delete_file(path: Optional[str]) -> None:
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            pass


def _is_int(value: str) -> bool:
    try:
        int(value)
    except ValueError:
        return False
    return True


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _suffix(media_type: str) -> str:
    return {
        "photo": ".jpg",
        "voice": ".ogg",
        "video": ".mp4",
        "video_note": ".mp4",
        "audio": ".mp3",
        "gif": ".mp4",
    }.get(media_type, ".bin")


def open_store() -> ArchiveStore:
    """Return the process-wide store, opening it on first use."""
    global _store
    with _store_lock:
        path = archive_db_path()
        if _store is not None and _store.db_path == path:
            return _store
        if _store is not None:
            _store.close()
        _store = ArchiveStore(
            path,
            archive_media_dir(),
            mode=media_mode(),
            max_file_bytes=_positive_int(
                "TELEGRAM_ARCHIVE_MEDIA_MAX_BYTES", _DEFAULT_MAX_FILE_BYTES
            ),
            total_bytes=_positive_int("TELEGRAM_ARCHIVE_MEDIA_TOTAL_BYTES", _DEFAULT_TOTAL_BYTES),
        )
        return _store


def reset_store() -> None:
    """Close the cached store so the next open sees new env paths. Tests only."""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
            _store = None
