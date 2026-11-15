"""Optional notifications to a dedicated Telegram channel or chat via Bot API.

If TELEGRAM_NOTIFY_BOT_TOKEN and TELEGRAM_NOTIFY_CHAT_ID are not set,
this module remains completely inert and performs no network operations.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_MAX_CAPTION_LEN = 1000
_MAX_TEXT_LEN = 4000


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

        field = "video_note" if kind == "video_note" else ("photo" if kind == "photo" else ("video" if kind == "video" else ("voice" if kind == "voice" else ("audio" if kind == "audio" else "document"))))

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
        sender = _esc(snap.get("sender_name") or snap.get("sender_id") or "неизвестно")
        chat = _esc(snap.get("chat_title") or snap.get("chat_id") or "чат")
        text = (
            f"<b>✏️ Правка</b> · <b>{sender}</b> · {chat}\n\n"
            f"<b>Было:</b> {_esc(old_text or 'пусто')}\n"
            f"<b>Стало:</b> {_esc(new_text or 'пусто')}"
        )
        await self.send_text(text)

    async def notify_delete_single(self, row: dict[str, Any]) -> None:
        if not self.active:
            return
        sender = _esc(row.get("sender_name") or row.get("sender_id") or "неизвестно")
        chat = _esc(row.get("chat_title") or row.get("chat_id") or "чат")
        header = f"<b>🗑️ Удалено</b> · <b>{sender}</b> · {chat}"

        media_path = row.get("media_path")
        media_type = row.get("media_type") or "text"

        if media_path and os.path.isfile(media_path) and media_type != "text":
            caption = f"{header}\n{_esc(row.get('text') or '')}"
            sent = await self.send_media(media_type, media_path, caption)
            if sent:
                return

        body = row.get("text") or (f"[{media_type}]" if media_type != "text" else "пустое сообщение")
        text = f"{header}\n\n{_esc(body)}"
        await self.send_text(text)

    async def notify_delete_bulk(self, chat_name: str, chat_id: Optional[int], rows: list[dict[str, Any]], total_count: int) -> None:
        if not self.active or not rows:
            return
        n = len(rows)
        chat_label = _esc(chat_name or str(chat_id or "чат"))
        extra = f" (из {total_count} удаленных)" if total_count > n else ""
        summary = f"<b>🗑️ Удалено {n} сообщений{extra}</b> · <b>{chat_label}</b>"
        await self.send_text(summary)

        # Build clean .txt transcript
        lines = [
            f"Архив удалённых сообщений · {chat_name} (chat {chat_id})",
            f"Сохранено сообщений: {n}",
            f"Время создания дампа: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "",
            "----------------------------------------------------------------",
        ]
        for r in rows:
            timestamp = _format_time(r.get("date"))
            sender = r.get("sender_name") or str(r.get("sender_id") or "неизвестно")
            m_kind = r.get("media_type")
            prefix = f"[{m_kind}] " if m_kind and m_kind != "text" else ""
            body = prefix + (r.get("text") or "")
            lines.append(f"[{timestamp}] {sender}: {body}")
        lines.append("----------------------------------------------------------------\n")

        filename = f"deleted_{chat_id}_{int(datetime.now(timezone.utc).timestamp())}.txt"
        content = "\n".join(lines).encode("utf-8")
        await self.send_document_bytes(filename, content, f"Транскрипт удалённых сообщений ({chat_label})")

    async def notify_ephemeral(self, snap: dict[str, Any], file_path: Optional[str], ttl_seconds: Optional[int]) -> None:
        if not self.active:
            return
        sender = _esc(snap.get("sender_name") or snap.get("sender_id") or "неизвестно")
        chat = _esc(snap.get("chat_title") or snap.get("chat_id") or "чат")
        ttl_str = f"TTL: {ttl_seconds}s" if ttl_seconds else "исчезающее"
        header = f"<b>🔥 Исчезающее медиа ({ttl_str})</b> · <b>{sender}</b> · {chat}"

        media_type = snap.get("media_type") or "photo"
        if file_path and os.path.isfile(file_path):
            caption = f"{header}\n{_esc(snap.get('text') or '')}"
            sent = await self.send_media(media_type, file_path, caption)
            if sent:
                return

        text = f"{header}\n\n{_esc(snap.get('text') or 'медиафайл')}"
        await self.send_text(text)


_default_notifier: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    global _default_notifier
    if _default_notifier is None:
        _default_notifier = TelegramNotifier()
    return _default_notifier
