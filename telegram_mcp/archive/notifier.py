"""Notifications to a dedicated Telegram channel or chat via Bot API.

If TELEGRAM_NOTIFY_BOT_TOKEN and TELEGRAM_NOTIFY_CHAT_ID are not set,
this module remains completely inert and performs no network operations.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_MAX_CAPTION_LEN = 1000
_MAX_TEXT_LEN = 4000
_DEFAULT_NOTIFY_MAX_MEDIA_BYTES = 3 * 1024 * 1024  # 3 MB limit for bot outbound media uploads


def _max_notify_bytes() -> int:
    raw = os.getenv("TELEGRAM_NOTIFY_MAX_BYTES", "").strip()
    if raw.isdigit():
        return int(raw)
    return _DEFAULT_NOTIFY_MAX_MEDIA_BYTES


def _bot_token() -> str:
    return os.getenv("TELEGRAM_NOTIFY_BOT_TOKEN", "").strip()


def _chat_id() -> str:
    return os.getenv("TELEGRAM_NOTIFY_CHAT_ID", "").strip()


def is_enabled() -> bool:
    """True when both bot token and destination chat/channel are configured."""
    return bool(_bot_token() and _chat_id())


def _esc(val: Any) -> str:
    return html.escape(str(val or ""))


def _format_time(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(iso_str)


def _time_diff(iso1: Optional[str], iso2: Optional[str]) -> str:
    if not iso1 or not iso2:
        return ""
    try:
        d1 = datetime.fromisoformat(iso1.replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(iso2.replace("Z", "+00:00"))
        diff = int(abs((d2 - d1).total_seconds()))
        if diff < 60:
            return f"{diff} сек"
        elif diff < 3600:
            return f"{diff // 60} мин {diff % 60} сек"
        elif diff < 86400:
            hours = diff // 3600
            mins = (diff % 3600) // 60
            return f"{hours} ч {mins} мин"
        else:
            days = diff // 86400
            hours = (diff % 86400) // 3600
            return f"{days} дн {hours} ч"
    except Exception:
        return ""


def _format_bytes(b: Any) -> str:
    if not b:
        return ""
    try:
        val = int(b)
    except Exception:
        return ""
    if val <= 0:
        return ""
    if val < 1024:
        return f"{val} B"
    elif val < 1024 * 1024:
        return f"{val / 1024:.1f} KB"
    else:
        return f"{val / (1024 * 1024):.2f} MB"


def _peer_desc(peer_type: Optional[str]) -> str:
    mapping = {
        "user": "Личные",
        "chat": "Группа",
        "channel": "Канал/Супергруппа",
    }
    return mapping.get(str(peer_type or "").lower(), "Чат")


def _media_desc(kind: Optional[str]) -> str:
    mapping = {
        "photo": "Фото",
        "video": "Видео",
        "voice": "Голосовое",
        "video_note": "Видеозаметка",
        "document": "Документ",
        "audio": "Аудио",
        "sticker": "Стикер",
        "animation": "GIF",
        "text": "Текст",
    }
    return mapping.get(str(kind or "text").lower(), kind or "Медиа")


def _chat_link(chat_id: Optional[int], message_id: Optional[int] = None) -> Optional[str]:
    if not chat_id:
        return None
    try:
        cid = int(chat_id)
        if cid < 0:
            s = str(cid)
            if s.startswith("-100"):
                internal_id = s[4:]
                if message_id:
                    return f"https://t.me/c/{internal_id}/{message_id}"
                return f"https://t.me/c/{internal_id}/1"
    except Exception:
        pass
    return None


class TelegramNotifier:
    """Dispatches async notifications via Telegram Bot API without blocking Telethon."""

    def __init__(self, token: Optional[str] = None, target_chat: Optional[str] = None):
        self._token = token or _bot_token()
        self._target_chat = target_chat or _chat_id()

    @property
    def active(self) -> bool:
        return bool(self._token and self._target_chat)

    async def _post(self, method: str, data: dict[str, Any], files: Optional[dict[str, Any]] = None) -> bool:
        if not self.active:
            return False
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                if files:
                    resp = await client.post(url, data=data, files=files)
                else:
                    resp = await client.post(url, json=data)
                result = resp.json()
                if not result.get("ok"):
                    logger.debug("TelegramNotifier %s failed: %s", method, result.get("description"))
                    return False
                return True
        except Exception as exc:
            logger.debug("TelegramNotifier %s network error: %s", method, exc)
            return False

    async def send_text(self, text: str) -> bool:
        if not self.active:
            return False
        data = {
            "chat_id": self._target_chat,
            "text": text[:_MAX_TEXT_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        return await self._post("sendMessage", data)

    async def send_media(self, kind: str, file_path: str, caption: str) -> bool:
        if not self.active or not os.path.isfile(file_path):
            return False

        method = {
            "photo": "sendPhoto",
            "video": "sendVideo",
            "voice": "sendVoice",
            "video_note": "sendVideoNote",
            "audio": "sendAudio",
            "document": "sendDocument",
            "sticker": "sendSticker",
        }.get(kind, "sendDocument")

        field = (
            "video_note"
            if kind == "video_note"
            else (
                "photo"
                if kind == "photo"
                else (
                    "video"
                    if kind == "video"
                    else (
                        "voice"
                        if kind == "voice"
                        else ("audio" if kind == "audio" else "document")
                    )
                )
            )
        )

        data = {"chat_id": self._target_chat}
        if caption and kind != "video_note":
            data["caption"] = caption[:_MAX_CAPTION_LEN]
            data["parse_mode"] = "HTML"

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            files = {field: (os.path.basename(file_path), content)}
            return await self._post(method, data=data, files=files)
        except Exception as exc:
            logger.debug("TelegramNotifier send_media error: %s", exc)
            return False

    async def send_document_bytes(self, filename: str, content: bytes, caption: str) -> bool:
        if not self.active:
            return False
        data = {
            "chat_id": self._target_chat,
            "caption": caption[:_MAX_CAPTION_LEN],
            "parse_mode": "HTML",
        }
        files = {"document": (filename, content, "text/plain; charset=utf-8")}
        return await self._post("sendDocument", data=data, files=files)

    async def notify_edit(self, snap: dict[str, Any], old_text: str, new_text: str) -> None:
        if not self.active:
            return

        sender_name = snap.get("sender_name") or "Неизвестно"
        sender_id = snap.get("sender_id")
        chat_title = snap.get("chat_title") or ("Личные" if snap.get("peer_type") == "user" else "Чат")
        chat_id = snap.get("chat_id")
        peer_type = snap.get("peer_type")
        message_id = snap.get("message_id")
        sent_time = _format_time(snap.get("date"))
        edit_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        link = _chat_link(chat_id, message_id)
        link_html = f' [<a href="{link}">сообщение</a>]' if link else ""

        sender_mention = (
            f'<a href="tg://user?id={sender_id}">{_esc(sender_name)}</a>'
            if sender_id
            else _esc(sender_name)
        )

        text = (
            f"<b>ПРАВКА</b> · <b>{_esc(chat_title)}</b>{link_html}\n"
            f"Чат: <code>{chat_id}</code> ({_peer_desc(peer_type)}) · Автор: {sender_mention} (id: <code>{sender_id or '—'}</code>)\n"
            f"Время: {sent_time} ➔ {edit_time}\n\n"
            f"<b>Было:</b>\n"
            f"<blockquote>{_esc(old_text or 'пусто')}</blockquote>\n"
            f"<b>Стало:</b>\n"
            f"<blockquote>{_esc(new_text or 'пусто')}</blockquote>"
        )
        await self.send_text(text)

    async def notify_delete_single(self, row: dict[str, Any]) -> None:
        if not self.active:
            return

        sender_name = row.get("sender_name") or "Неизвестно"
        sender_id = row.get("sender_id")
        chat_title = row.get("chat_title") or ("Личные" if row.get("peer_type") == "user" else "Чат")
        chat_id = row.get("chat_id")
        peer_type = row.get("peer_type")
        message_id = row.get("message_id")
        media_path = row.get("media_path")
        media_type = row.get("media_type") or "text"
        media_bytes = row.get("media_bytes") or 0
        size_str = f" ({_format_bytes(media_bytes)})" if media_bytes > 0 else ""

        sent_time = _format_time(row.get("date"))
        del_iso = row.get("deleted_at") or datetime.now(timezone.utc).isoformat()
        del_time = _format_time(del_iso)
        lifetime = _time_diff(row.get("date"), del_iso)
        lifetime_str = f" ({lifetime})" if lifetime else ""

        max_notify_bytes = _max_notify_bytes()
        is_lightweight = media_type in ("photo", "voice", "video_note", "sticker")
        can_upload = (
            media_path
            and os.path.isfile(media_path)
            and media_type != "text"
            and (is_lightweight or media_bytes <= max_notify_bytes)
        )

        size_label = _format_bytes(media_bytes)
        if not can_upload and media_path and os.path.isfile(media_path) and media_type != "text":
            size_str = f" ({size_label}, на сервере)" if size_label else " (на сервере)"
        else:
            size_str = f" ({size_label})" if size_label else ""

        meta_extra: list[str] = []
        if row.get("is_ephemeral"):
            ttl = row.get("ttl_seconds")
            meta_extra.append(f"TTL {ttl}s" if ttl else "View-Once")

        fwd_raw = row.get("forward_json")
        if fwd_raw:
            try:
                fwd_data = json.loads(fwd_raw) if isinstance(fwd_raw, str) else fwd_raw
                from_n = fwd_data.get("from_name") or fwd_data.get("from_id")
                if from_n:
                    meta_extra.append(f"переслано от {_esc(from_n)}")
            except Exception:
                pass

        if row.get("reply_to_msg_id"):
            meta_extra.append(f"в ответ на #{row.get('reply_to_msg_id')}")

        extra_str = f" · {', '.join(meta_extra)}" if meta_extra else ""

        link = _chat_link(chat_id, message_id)
        link_html = f' [<a href="{link}">перейти</a>]' if link else ""

        sender_mention = (
            f'<a href="tg://user?id={sender_id}">{_esc(sender_name)}</a>'
            if sender_id
            else _esc(sender_name)
        )

        body_text = row.get("text") or ""
        body_block = f"\n\n<blockquote>{_esc(body_text)}</blockquote>" if body_text else "\n\n<i>(без текста)</i>"

        full_card = (
            f"<b>УДАЛЕНО</b> · <b>{_esc(chat_title)}</b>{link_html}\n"
            f"Чат: <code>{chat_id}</code> ({_peer_desc(peer_type)}) · Автор: {sender_mention} (id: <code>{sender_id or '—'}</code>)\n"
            f"Время: {sent_time} ➔ {del_time}{lifetime_str} · Вложение: {_media_desc(media_type)}{size_str}{extra_str}"
            f"{body_block}"
        )

        if can_upload:
            if len(full_card) <= _MAX_CAPTION_LEN:
                sent = await self.send_media(media_type, media_path, full_card)
                if sent:
                    return
            else:
                compact_caption = (
                    f"<b>УДАЛЕНО</b> · <b>{_esc(chat_title)}</b> · {_esc(sender_name)}\n"
                    f"Время: {sent_time} ➔ {del_time}{lifetime_str} · {_media_desc(media_type)}{size_str}"
                )
                sent = await self.send_media(media_type, media_path, compact_caption)
                if sent:
                    await self.send_text(full_card)
                    return

        await self.send_text(full_card)

    async def notify_delete_bulk(
        self,
        chat_name: str,
        chat_id: Optional[int],
        rows: list[dict[str, Any]],
        total_count: int,
    ) -> None:
        if not self.active or not rows:
            return

        n = len(rows)
        first_row = rows[0]
        peer_type = first_row.get("peer_type")
        chat_title = chat_name or first_row.get("chat_title") or ("Личные" if peer_type == "user" else "Чат")
        extra = f" (из {total_count} удалённых)" if total_count > n else ""

        authors: dict[str, int] = {}
        for r in rows:
            name = r.get("sender_name") or str(r.get("sender_id") or "Неизвестно")
            authors[name] = authors.get(name, 0) + 1
        top_authors = sorted(authors.items(), key=lambda x: x[1], reverse=True)[:4]
        authors_str = ", ".join(f"{_esc(k)} ({v})" for k, v in top_authors)

        dates = [r.get("date") for r in rows if r.get("date")]
        dates.sort()
        time_range = f"{_format_time(dates[0])} — {_format_time(dates[-1])}" if dates else "—"

        link = _chat_link(chat_id)
        link_html = f' [<a href="{link}">чат</a>]' if link else ""

        summary = (
            f"<b>МАССОВОЕ УДАЛЕНИЕ</b> · <b>{_esc(chat_title)}</b>{link_html}\n"
            f"Чат: <code>{chat_id}</code> ({_peer_desc(peer_type)}) · Удалено: <b>{n}</b> сообщений{extra}\n"
            f"Авторы: {authors_str}\n"
            f"Период: {time_range}\n"
            f"<i>Транскрипт прикреплён файлом ниже.</i>"
        )
        await self.send_text(summary)

        # Build clean .txt transcript without unicode decorations
        lines = [
            "--- АРХИВ УДАЛЁННЫХ СООБЩЕНИЙ ---",
            f"Чат: {chat_title} (ID: {chat_id}, {_peer_desc(peer_type)})",
            f"Сообщений в архиве: {n}",
            f"Время дампа: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "---------------------------------",
            "",
        ]
        for r in rows:
            timestamp = _format_time(r.get("date"))
            sender = r.get("sender_name") or str(r.get("sender_id") or "Неизвестно")
            m_kind = r.get("media_type")
            m_size = _format_bytes(r.get("media_bytes"))
            m_prefix = f"[{_media_desc(m_kind)}{' ' + m_size if m_size else ''}] " if m_kind and m_kind != "text" else ""
            msg_id = r.get("message_id")
            body = m_prefix + (r.get("text") or "")
            lines.append(f"[{timestamp}] [ID: {msg_id}] {sender}: {body}")
        lines.append("\n---------------------------------\n")

        filename = f"deleted_{chat_id}_{int(datetime.now(timezone.utc).timestamp())}.txt"
        content = "\n".join(lines).encode("utf-8")
        await self.send_document_bytes(filename, content, f"Транскрипт удалённых сообщений · {_esc(chat_title)}")

        # Send all saved media files (voice notes, videos, photos, files) from the deleted batch
        max_notify_bytes = _max_notify_bytes()
        media_rows = [
            r
            for r in rows
            if r.get("media_path")
            and os.path.isfile(r.get("media_path"))
            and (r.get("media_type") or "text") != "text"
        ]
        for mr in media_rows:
            m_type = mr.get("media_type") or "document"
            m_path = mr.get("media_path")
            m_bytes = mr.get("media_bytes") or 0
            m_sender = mr.get("sender_name") or str(mr.get("sender_id") or "Неизвестно")
            m_time = _format_time(mr.get("date"))
            m_size = _format_bytes(m_bytes)
            m_text = mr.get("text") or ""

            is_lightweight = m_type in ("photo", "voice", "video_note", "sticker")
            can_upload_item = is_lightweight or (m_bytes <= max_notify_bytes)

            if can_upload_item:
                m_size_str = f" ({m_size})" if m_size else ""
                caption = f"<b>{_media_desc(m_type)}</b>{m_size_str} · {_esc(m_sender)} [{m_time}]"
                if m_text:
                    caption += f"\n<blockquote>{_esc(m_text)}</blockquote>"
                await self.send_media(m_type, m_path, caption[:_MAX_CAPTION_LEN])
            else:
                heavy_note = (
                    f"<b>{_media_desc(m_type)}</b> ({m_size}, на сервере) · {_esc(m_sender)} [{m_time}]"
                )
                if m_text:
                    heavy_note += f"\n<blockquote>{_esc(m_text)}</blockquote>"
                await self.send_text(heavy_note)

    async def notify_ephemeral(
        self,
        snap: dict[str, Any],
        file_path: Optional[str],
        ttl_seconds: Optional[int],
    ) -> None:
        if not self.active:
            return

        sender_name = snap.get("sender_name") or "Неизвестно"
        sender_id = snap.get("sender_id")
        chat_title = snap.get("chat_title") or ("Личные" if snap.get("peer_type") == "user" else "Чат")
        chat_id = snap.get("chat_id")
        peer_type = snap.get("peer_type")
        message_id = snap.get("message_id")
        media_type = snap.get("media_type") or "photo"
        sent_time = _format_time(snap.get("date"))

        link = _chat_link(chat_id, message_id)
        link_html = f' [<a href="{link}">перейти</a>]' if link else ""

        sender_mention = (
            f'<a href="tg://user?id={sender_id}">{_esc(sender_name)}</a>'
            if sender_id
            else _esc(sender_name)
        )

        ttl_str = f"TTL {ttl_seconds}s" if ttl_seconds else "View-Once"
        body_text = snap.get("text") or ""
        body_block = f"\n\n<blockquote>{_esc(body_text)}</blockquote>" if body_text else ""

        full_card = (
            f"<b>ИСЧЕЗАЮЩЕЕ МЕДИА ({ttl_str})</b> · <b>{_esc(chat_title)}</b>{link_html}\n"
            f"Чат: <code>{chat_id}</code> ({_peer_desc(peer_type)}) · Автор: {sender_mention} (id: <code>{sender_id or '—'}</code>)\n"
            f"Время: {sent_time} · Вложение: {_media_desc(media_type)}"
            f"{body_block}"
        )

        max_notify_bytes = _max_notify_bytes()
        file_bytes = os.path.getsize(file_path) if (file_path and os.path.isfile(file_path)) else 0
        is_lightweight = media_type in ("photo", "voice", "video_note", "sticker")
        can_upload_eph = file_path and os.path.isfile(file_path) and (is_lightweight or file_bytes <= max_notify_bytes)

        if can_upload_eph:
            if len(full_card) <= _MAX_CAPTION_LEN:
                sent = await self.send_media(media_type, file_path, full_card)
                if sent:
                    return
            else:
                compact_caption = (
                    f"<b>ИСЧЕЗАЮЩЕЕ МЕДИА ({ttl_str})</b> · <b>{_esc(chat_title)}</b> · {_esc(sender_name)}\n"
                    f"Время: {sent_time} · {_media_desc(media_type)}"
                )
                sent = await self.send_media(media_type, file_path, compact_caption)
                if sent:
                    await self.send_text(full_card)
                    return

        await self.send_text(full_card)


_default_notifier: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    global _default_notifier
    if _default_notifier is None:
        _default_notifier = TelegramNotifier()
    return _default_notifier
