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
        "user": "👤 Личные сообщения (DM)",
        "chat": "👥 Обычная группа",
        "channel": "📢 Канал / Супергруппа",
    }
    return mapping.get(str(peer_type or "").lower(), "💬 Чат")


def _media_desc(kind: Optional[str]) -> str:
    mapping = {
        "photo": "📸 Фотография",
        "video": "🎥 Видеозапись",
        "voice": "🎙 Голосовое сообщение",
        "video_note": "📹 Кружочек (видеозаметка)",
        "document": "📄 Документ / Файл",
        "audio": "🎵 Аудиозапись",
        "sticker": "🎭 Стикер",
        "animation": "🎞 GIF / Анимация",
        "text": "💬 Текст",
    }
    return mapping.get(str(kind or "text").lower(), f"📎 {kind or 'медиа'}")


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
        chat_title = snap.get("chat_title") or ("Личные сообщения" if snap.get("peer_type") == "user" else "Чат")
        chat_id = snap.get("chat_id")
        peer_type = snap.get("peer_type")
        message_id = snap.get("message_id")
        sent_time = _format_time(snap.get("date"))
        edit_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        link = _chat_link(chat_id, message_id)
        link_line = f'🔗 <b>Ссылка:</b> <a href="{link}">Перейти к сообщению</a>\n' if link else ""

        sender_mention = (
            f'<a href="tg://user?id={sender_id}">{_esc(sender_name)}</a>'
            if sender_id
            else _esc(sender_name)
        )

        text = (
            f"✏️ <b>ОТРЕДАКТИРОВАНО СООБЩЕНИЕ</b>\n\n"
            f"💬 <b>Чат:</b> <b>{_esc(chat_title)}</b>\n"
            f"📁 <b>Тип:</b> {_peer_desc(peer_type)}\n"
            f"🆔 <b>Chat ID:</b> <code>{chat_id}</code>\n"
            f"{link_line}\n"
            f"👤 <b>Автор:</b> {sender_mention}\n"
            f"🆔 <b>User ID:</b> <code>{sender_id or '—'}</code>\n\n"
            f"🕒 <b>Отправлено:</b> {sent_time}\n"
            f"⏱ <b>Отредактировано:</b> {edit_time}\n\n"
            f"📝 <b>Было:</b>\n"
            f"<blockquote>{_esc(old_text or 'пусто')}</blockquote>\n\n"
            f"📝 <b>Стало:</b>\n"
            f"<blockquote>{_esc(new_text or 'пусто')}</blockquote>"
        )
        await self.send_text(text)

    async def notify_delete_single(self, row: dict[str, Any]) -> None:
        if not self.active:
            return

        sender_name = row.get("sender_name") or "Неизвестно"
        sender_id = row.get("sender_id")
        chat_title = row.get("chat_title") or ("Личные сообщения" if row.get("peer_type") == "user" else "Чат")
        chat_id = row.get("chat_id")
        peer_type = row.get("peer_type")
        message_id = row.get("message_id")
        media_path = row.get("media_path")
        media_type = row.get("media_type") or "text"
        media_bytes = row.get("media_bytes") or 0
        size_str = f"({_format_bytes(media_bytes)})" if media_bytes > 0 else ""

        sent_time = _format_time(row.get("date"))
        del_iso = row.get("deleted_at") or datetime.now(timezone.utc).isoformat()
        del_time = _format_time(del_iso)
        lifetime = _time_diff(row.get("date"), del_iso)
        lifetime_str = f" (прожило: {lifetime})" if lifetime else ""

        eph_str = ""
        if row.get("is_ephemeral"):
            ttl = row.get("ttl_seconds")
            eph_str = f"\n⏳ <b>Исчезающее:</b> TTL {ttl}s" if ttl else "\n⏳ <b>Исчезающее:</b> View-Once"

        fwd_str = ""
        fwd_raw = row.get("forward_json")
        if fwd_raw:
            try:
                fwd_data = json.loads(fwd_raw) if isinstance(fwd_raw, str) else fwd_raw
                from_n = fwd_data.get("from_name") or fwd_data.get("from_id")
                if from_n:
                    fwd_str = f"\n↪️ <b>Переслано от:</b> {_esc(from_n)}"
            except Exception:
                pass

        reply_str = ""
        if row.get("reply_to_msg_id"):
            reply_str = f"\n↩️ <b>В ответ на:</b> #{row.get('reply_to_msg_id')}"

        link = _chat_link(chat_id, message_id)
        link_line = f'🔗 <b>Ссылка:</b> <a href="{link}">Перейти к месту в чате</a>\n' if link else ""

        sender_mention = (
            f'<a href="tg://user?id={sender_id}">{_esc(sender_name)}</a>'
            if sender_id
            else _esc(sender_name)
        )

        body_text = row.get("text") or ""
        body_block = f"\n\n📝 <b>Текст:</b>\n<blockquote>{_esc(body_text)}</blockquote>" if body_text else "\n\n<i>(без текстового описания)</i>"

        full_card = (
            f"🗑 <b>УДАЛЕНО СООБЩЕНИЕ</b>\n\n"
            f"💬 <b>Чат:</b> <b>{_esc(chat_title)}</b>\n"
            f"📁 <b>Тип:</b> {_peer_desc(peer_type)}\n"
            f"🆔 <b>Chat ID:</b> <code>{chat_id}</code>\n"
            f"{link_line}\n"
            f"👤 <b>Автор:</b> {sender_mention}\n"
            f"🆔 <b>User ID:</b> <code>{sender_id or '—'}</code>\n\n"
            f"🕒 <b>Отправлено:</b> {sent_time}\n"
            f"⏱ <b>Удалено:</b> {del_time}{lifetime_str}\n"
            f"📎 <b>Вложение:</b> {_media_desc(media_type)} {size_str}{eph_str}{fwd_str}{reply_str}"
            f"{body_block}"
        )

        # If media file exists, send with media
        if media_path and os.path.isfile(media_path) and media_type != "text":
            if len(full_card) <= _MAX_CAPTION_LEN:
                sent = await self.send_media(media_type, media_path, full_card)
                if sent:
                    return
            else:
                compact_caption = (
                    f"🗑 <b>УДАЛЕНО {_media_desc(media_type).upper()}</b>\n"
                    f"💬 <b>{_esc(chat_title)}</b> · {_esc(sender_name)}\n"
                    f"⏱ Удалено: {del_time} {size_str}"
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
        chat_title = chat_name or first_row.get("chat_title") or ("Личные сообщения" if peer_type == "user" else "Чат")
        extra = f" (из {total_count} удалённых)" if total_count > n else ""

        # Analyze authors
        authors: dict[str, int] = {}
        for r in rows:
            name = r.get("sender_name") or str(r.get("sender_id") or "Неизвестно")
            authors[name] = authors.get(name, 0) + 1
        top_authors = sorted(authors.items(), key=lambda x: x[1], reverse=True)[:4]
        authors_str = ", ".join(f"{_esc(k)} ({v})" for k, v in top_authors)

        # Time range
        dates = [r.get("date") for r in rows if r.get("date")]
        dates.sort()
        time_range = f"с {_format_time(dates[0])} по {_format_time(dates[-1])}" if dates else "—"

        link = _chat_link(chat_id)
        link_line = f'🔗 <b>Ссылка:</b> <a href="{link}">Перейти в чат</a>\n' if link else ""

        summary = (
            f"🗑 <b>МАССОВОЕ УДАЛЕНИЕ СООБЩЕНИЙ</b>\n\n"
            f"💬 <b>Чат:</b> <b>{_esc(chat_title)}</b>\n"
            f"📁 <b>Тип:</b> {_peer_desc(peer_type)}\n"
            f"🆔 <b>Chat ID:</b> <code>{chat_id}</code>\n"
            f"{link_line}\n"
            f"📊 <b>Удалено сообщений в архиве:</b> <b>{n}</b>{extra}\n"
            f"👥 <b>Авторы:</b> {authors_str}\n"
            f"🕒 <b>Период переписки:</b> {time_range}\n\n"
            f"📎 <i>Полный текстовый транскрипт переписки сформирован и отправлен файлом ниже:</i>"
        )
        await self.send_text(summary)

        # Build clean, rich .txt transcript
        lines = [
            "=" * 64,
            "АРХИВ УДАЛЁННЫХ СООБЩЕНИЙ",
            "=" * 64,
            f"Чат: {chat_title}",
            f"Тип чата: {_peer_desc(peer_type)}",
            f"Chat ID: {chat_id}",
            f"Сообщений в дампе: {n}",
            f"Время создания дампа: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "=" * 64,
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
        lines.append("\n" + "=" * 64 + "\n")

        filename = f"deleted_{chat_id}_{int(datetime.now(timezone.utc).timestamp())}.txt"
        content = "\n".join(lines).encode("utf-8")
        await self.send_document_bytes(filename, content, f"📄 Транскрипт удалённых сообщений · {_esc(chat_title)}")

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
        chat_title = snap.get("chat_title") or ("Личные сообщения" if snap.get("peer_type") == "user" else "Чат")
        chat_id = snap.get("chat_id")
        peer_type = snap.get("peer_type")
        message_id = snap.get("message_id")
        media_type = snap.get("media_type") or "photo"
        sent_time = _format_time(snap.get("date"))

        link = _chat_link(chat_id, message_id)
        link_line = f'🔗 <b>Ссылка:</b> <a href="{link}">Перейти к сообщению</a>\n' if link else ""

        sender_mention = (
            f'<a href="tg://user?id={sender_id}">{_esc(sender_name)}</a>'
            if sender_id
            else _esc(sender_name)
        )

        ttl_str = f"{ttl_seconds} сек" if ttl_seconds else "View-Once (1 просмотр)"
        body_text = snap.get("text") or ""
        body_block = f"\n\n📝 <b>Подпись:</b>\n<blockquote>{_esc(body_text)}</blockquote>" if body_text else ""

        full_card = (
            f"🔥 <b>ПЕРЕХВАЧЕНО ИСЧЕЗАЮЩЕЕ МЕДИА</b>\n\n"
            f"💬 <b>Чат:</b> <b>{_esc(chat_title)}</b>\n"
            f"📁 <b>Тип:</b> {_peer_desc(peer_type)}\n"
            f"🆔 <b>Chat ID:</b> <code>{chat_id}</code>\n"
            f"{link_line}\n"
            f"👤 <b>Автор:</b> {sender_mention}\n"
            f"🆔 <b>User ID:</b> <code>{sender_id or '—'}</code>\n\n"
            f"🕒 <b>Отправлено:</b> {sent_time}\n"
            f"⏳ <b>Таймер самоуничтожения:</b> {ttl_str}\n"
            f"📎 <b>Вложение:</b> {_media_desc(media_type)}"
            f"{body_block}"
        )

        if file_path and os.path.isfile(file_path):
            if len(full_card) <= _MAX_CAPTION_LEN:
                sent = await self.send_media(media_type, file_path, full_card)
                if sent:
                    return
            else:
                compact_caption = (
                    f"🔥 <b>ИСЧЕЗАЮЩЕЕ МЕДИА ({ttl_str})</b>\n"
                    f"💬 <b>{_esc(chat_title)}</b> · {_esc(sender_name)}"
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
