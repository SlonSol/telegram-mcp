"""Listen for new, edited and deleted messages and store them locally.

Handlers only read the update Telegram already pushed. They do not call
``readHistory``, ``readMentions``, ``readReactions``, ``readDiscussion`` or
story-view requests. Optional media downloads use ``upload.getFile``, which
does not mark a chat as read.

In stdio mode the process lives only as long as the MCP client keeps it
open, so capture stops when that client disconnects. The database stays on
disk. HTTP mode is a long-running process and keeps capturing.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from telethon import events

from telegram_mcp.archive.store import ArchiveStore, open_store

_MEDIA_ATTRS = (
    ("voice", "voice"),
    ("video_note", "video_note"),
    ("video", "video"),
    ("audio", "audio"),
    ("gif", "gif"),
    ("sticker", "sticker"),
    ("photo", "photo"),
    ("document", "document"),
    ("contact", "contact"),
    ("geo", "geo"),
    ("poll", "poll"),
)


def capture_status() -> dict[str, Any]:
    """How this process is capturing, for logs and archive tool results."""
    transport = (os.getenv("MCP_TRANSPORT") or "stdio").strip().lower() or "stdio"
    if transport == "stdio":
        return {
            "transport": "stdio",
            "live": False,
            "note": (
                "The archive only records messages while this process is running. "
                "Stdio mode stops when the assistant disconnects, so messages sent "
                "after that are missed until it connects again. "
                "Use MCP_TRANSPORT=http to keep recording in the background."
            ),
        }
    return {
        "transport": transport,
        "live": True,
        "note": "Recording messages for as long as this server process stays running.",
    }


def media_kind(message: Any) -> str:
    """Short media label. A link preview is not an attachment."""
    if getattr(message, "web_preview", None) is not None:
        return ""
    for attr, label in _MEDIA_ATTRS:
        if getattr(message, attr, None) is not None:
            return label
    if getattr(message, "action", None) is not None:
        return "service"
    return ""


def media_size(message: Any) -> Optional[int]:
    """Best known byte size, or None when the update does not say."""
    file = getattr(message, "file", None)
    size = getattr(file, "size", None) if file is not None else None
    if isinstance(size, int) and size > 0:
        return size
    photo = getattr(message, "photo", None)
    sizes = getattr(photo, "sizes", None) if photo is not None else None
    known = [getattr(item, "size", None) for item in sizes or []]
    known_ints = [item for item in known if isinstance(item, int) and item > 0]
    if known_ints:
        return max(known_ints)
    document = getattr(message, "document", None)
    doc_size = getattr(document, "size", None) if document is not None else None
    if isinstance(doc_size, int) and doc_size > 0:
        return doc_size
    return None


def peer_type_of(message: Any) -> str:
    """``channel`` for channels and supergroups, ``chat`` for basic groups, else ``user``.

    Supergroups are channels in MTProto: their deletes include a channel id.
    Private chats and basic groups do not.
    """
    if getattr(message, "is_channel", False):
        return "channel"
    if getattr(message, "is_private", False):
        return "user"
    if getattr(message, "is_group", False):
        return "chat"
    peer_name = type(getattr(message, "peer_id", None)).__name__
    if "Channel" in peer_name:
        return "channel"
    if peer_name == "PeerChat":
        return "chat"
    return "user"


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return None


def _sender_name(message: Any) -> Optional[str]:
    sender = getattr(message, "sender", None)
    if sender is None:
        return None
    title = getattr(sender, "title", None)
    if title:
        return str(title)
    parts = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
    name = " ".join(str(part) for part in parts if part)
    if name:
        return name
    username = getattr(sender, "username", None)
    return str(username) if username else None


def _chat_title(message: Any) -> Optional[str]:
    chat = getattr(message, "chat", None)
    if chat is None:
        return None
    title = getattr(chat, "title", None)
    if title:
        return str(title)
    parts = [getattr(chat, "first_name", None), getattr(chat, "last_name", None)]
    name = " ".join(str(part) for part in parts if part)
    return name or None


def _forward(message: Any) -> Optional[str]:
    forwarded = getattr(message, "fwd_from", None)
    if forwarded is None:
        forwarded = getattr(message, "forward", None)
    if forwarded is None:
        return None
    from_id = getattr(forwarded, "from_id", None)
    sender_id = getattr(from_id, "user_id", None) if from_id is not None else None
    if sender_id is None and from_id is not None:
        sender_id = getattr(from_id, "channel_id", None)
    payload = {
        "from_name": getattr(forwarded, "from_name", None),
        "from_id": sender_id,
        "date": _iso(getattr(forwarded, "date", None)),
        "channel_post": getattr(forwarded, "channel_post", None),
        "post_author": getattr(forwarded, "post_author", None),
    }
    return json.dumps(payload, ensure_ascii=False)


def snapshot(message: Any, account: str) -> Optional[dict[str, Any]]:
    """Fields we persist, or None when the update has no chat and message id."""
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "id", None)
    if chat_id is None or message_id is None:
        return None
    reply = getattr(message, "reply_to", None)
    text = getattr(message, "message", None)
    if text is None:
        text = getattr(message, "text", None) or ""
    return {
        "account": account,
        "chat_id": int(chat_id),
        "peer_type": peer_type_of(message),
        "chat_title": _chat_title(message),
        "sender_id": getattr(message, "sender_id", None),
        "sender_name": _sender_name(message),
        "message_id": int(message_id),
        "date": _iso(getattr(message, "date", None)),
        "text": text or "",
        "media_type": media_kind(message),
        "reply_to_msg_id": getattr(reply, "reply_to_msg_id", None) if reply else None,
        "forward_json": _forward(message),
    }


async def _maybe_download(client: Any, store: ArchiveStore, message: Any) -> Optional[bytes]:
    kind = media_kind(message)
    if not store.wants_media(kind, media_size(message)):
        return None
    download = getattr(client, "download_media", None)
    if not callable(download):
        return None
    try:
        data = await download(message, file=bytes)
    except Exception:
        return None
    if not isinstance(data, (bytes, bytearray)):
        return None
    if len(data) > store.max_file_bytes:
        return None
    return bytes(data)


async def _remember(
    client: Any, store: ArchiveStore, account: str, message: Any, source: str
) -> None:
    snap = snapshot(message, account)
    if snap is None:
        return
    media = await _maybe_download(client, store, message)
    store.upsert(snap, source=source, media=media)


def attach(client: Any, store: ArchiveStore, account: str) -> bool:
    """Register handlers. Returns False when ``client`` cannot listen."""
    add = getattr(client, "add_event_handler", None)
    if not callable(add):
        return False

    async def on_new(event: Any) -> None:
        await _remember(client, store, account, event.message, "new")

    async def on_edit(event: Any) -> None:
        await _remember(client, store, account, event.message, "edit")

    async def on_delete(event: Any) -> None:
        ids = [int(item) for item in (getattr(event, "deleted_ids", None) or [])]
        chat_id = getattr(event, "chat_id", None)
        # No chat id: private chats and basic groups (account-wide message ids).
        # A chat id: channel or supergroup.
        store.mark_deleted(account, ids, channel_chat_id=None if chat_id is None else int(chat_id))

    add(on_new, events.NewMessage())
    add(on_edit, events.MessageEdited())
    add(on_delete, events.MessageDeleted())
    return True


def start_archive(clients: dict[str, Any]) -> str:
    """Open the database and listen on every connected client. Returns the note."""
    store = open_store()
    for label, client in clients.items():
        attach(client, store, label)
    note = capture_status()["note"]
    return note
