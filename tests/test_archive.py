"""Deleted-message archive: storage quirks, edits, media caps, and read-only tools."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from telegram_mcp.archive.collector import attach, capture_status, media_kind, snapshot
from telegram_mcp.archive.store import open_store, reset_store
from telegram_mcp.tools.archive import (
    archive_stats,
    get_message_edit_history,
    list_deleted_messages,
    search_archive,
)


def _message(
    *,
    message_id,
    chat_id,
    text,
    peer="user",
    sender_id=7,
    sender_name="Анна",
    title="Анна",
    date=None,
    photo=False,
    document=False,
    reply_to=None,
    forward_name=None,
):
    date = date or datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    sender = SimpleNamespace(first_name=sender_name, last_name=None, username=None, title=None)
    chat = SimpleNamespace(
        title=title if peer != "user" else None, first_name=sender_name, last_name=None
    )
    photo_obj = None
    if photo:
        photo_obj = SimpleNamespace(sizes=[SimpleNamespace(size=1000)])
    document_obj = SimpleNamespace(size=500) if document else None
    forward = None
    if forward_name:
        forward = SimpleNamespace(
            from_name=forward_name,
            from_id=SimpleNamespace(user_id=9),
            date=date,
            channel_post=None,
            post_author=None,
        )
    return SimpleNamespace(
        id=message_id,
        chat_id=chat_id,
        message=text,
        text=text,
        date=date,
        sender_id=sender_id,
        sender=sender,
        chat=chat,
        is_channel=peer == "channel",
        is_private=peer == "user",
        is_group=peer == "chat",
        peer_id=SimpleNamespace(),
        photo=photo_obj,
        document=document_obj,
        web_preview=None,
        voice=None,
        video=None,
        video_note=None,
        audio=None,
        gif=None,
        sticker=None,
        contact=None,
        geo=None,
        poll=None,
        action=None,
        file=SimpleNamespace(size=1000) if photo or document else None,
        reply_to=SimpleNamespace(reply_to_msg_id=reply_to) if reply_to else None,
        fwd_from=forward,
        forward=None,
    )


class _Client:
    def __init__(self, payload=b"jpeg-bytes"):
        self.handlers = []
        self.payload = payload
        self.downloads = 0

    def add_event_handler(self, callback, event):
        self.handlers.append((callback, event.__class__.__name__))

    async def download_media(self, message, file=None):
        self.downloads += 1
        return self.payload

    def callback(self, name):
        return next(callback for callback, kind in self.handlers if kind == name)


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_MEDIA", "photos")
    monkeypatch.setenv("TELEGRAM_ARCHIVE_MEDIA_MAX_BYTES", "100000")
    monkeypatch.setenv("TELEGRAM_ARCHIVE_MEDIA_TOTAL_BYTES", "1000000")
    reset_store()
    opened = open_store()
    yield opened
    reset_store()


def test_private_delete_does_not_mark_a_channel_message_with_the_same_id(store):
    private = snapshot(_message(message_id=5, chat_id=42, text="secret"), "default")
    channel = snapshot(
        _message(message_id=5, chat_id=-100777, text="public", peer="channel", title="News"),
        "default",
    )
    basic = snapshot(
        _message(message_id=9, chat_id=-30, text="group", peer="chat", title="Friends"),
        "default",
    )
    assert private["peer_type"] == "user"
    assert channel["peer_type"] == "channel"
    assert basic["peer_type"] == "chat"
    store.upsert(private, source="new")
    store.upsert(channel, source="new")
    store.upsert(basic, source="new")

    # Telegram omits the chat id for private chats and basic groups.
    assert store.mark_deleted("default", [5, 9], channel_chat_id=None) == 2
    found = store.search("secret", account="default", chat_id=None, include_deleted=True, limit=10)
    assert len(found) == 1 and found[0]["deleted"] is True and found[0]["chat_id"] == 42
    public = store.search(
        "public", account="default", chat_id=None, include_deleted=True, limit=10
    )
    assert public[0]["deleted"] is False
    group = store.search("group", account="default", chat_id=None, include_deleted=True, limit=10)
    assert group[0]["deleted"] is True

    assert store.mark_deleted("default", [5], channel_chat_id=-100777) == 1
    public = store.search(
        "public", account="default", chat_id=None, include_deleted=True, limit=10
    )
    assert public[0]["deleted"] is True
    # The row is still there.
    assert public[0]["text"] == "public"


def test_edits_keep_earlier_text_and_deletes_do_not_remove_it(store):
    first = snapshot(_message(message_id=3, chat_id=42, text="version one"), "default")
    store.upsert(first, source="new")
    edited = snapshot(_message(message_id=3, chat_id=42, text="version two"), "default")
    store.upsert(edited, source="edit")
    store.mark_deleted("default", [3], channel_chat_id=None)

    history = store.edit_history("default", 42, 3)
    assert history["message"]["text"] == "version two"
    assert history["message"]["deleted"] is True
    assert [item["text"] for item in history["previous_versions"]] == ["version one"]
    # A repeat of the same text does not invent another version.
    store.upsert(edited, source="edit")
    history = store.edit_history("default", 42, 3)
    assert len(history["previous_versions"]) == 1


def test_media_cap_drops_oldest_files_and_keeps_text(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_MEDIA", "all")
    monkeypatch.setenv("TELEGRAM_ARCHIVE_MEDIA_MAX_BYTES", "100")
    monkeypatch.setenv("TELEGRAM_ARCHIVE_MEDIA_TOTAL_BYTES", "10")
    reset_store()
    store = open_store()
    older = snapshot(
        _message(
            message_id=1,
            chat_id=1,
            text="older text",
            date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        "default",
    )
    newer = snapshot(
        _message(
            message_id=2,
            chat_id=1,
            text="newer text",
            date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        ),
        "default",
    )
    store.upsert(older, source="new", media=b"01234567")
    store.upsert(newer, source="new", media=b"89abcdef")
    rows = store.search("text", account="default", chat_id=None, include_deleted=True, limit=10)
    by_id = {row["message_id"]: row for row in rows}
    assert by_id[1]["text"] == "older text"
    assert by_id[2]["text"] == "newer text"
    assert by_id[1]["media_saved"] is False
    assert by_id[2]["media_saved"] is True
    assert store.stats(None)["messages"] == 2


def test_oversized_photo_is_skipped_and_text_is_kept(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_MEDIA_MAX_BYTES", "4")
    reset_store()
    store = open_store()
    snap = snapshot(_message(message_id=8, chat_id=1, text="caption", photo=True), "default")
    assert media_kind(_message(message_id=8, chat_id=1, text="caption", photo=True)) == "photo"
    assert not store.wants_media("photo", 1000)
    store.upsert(snap, source="new", media=b"too-big")
    found = store.search("caption", account="default", chat_id=None, include_deleted=True, limit=5)
    assert found[0]["text"] == "caption"
    assert found[0]["media_saved"] is False


@pytest.mark.asyncio
async def test_handlers_archive_every_chat_and_skip_read_receipts(store):
    client = _Client(payload=b"small-photo")
    assert attach(client, store, "default")
    private = _message(
        message_id=11, chat_id=42, text="hello photo", photo=True, reply_to=4, forward_name="Bob"
    )
    channel = _message(
        message_id=11, chat_id=-1005, text="channel post", peer="channel", title="Channel"
    )
    await client.callback("NewMessage")(SimpleNamespace(message=private))
    await client.callback("NewMessage")(SimpleNamespace(message=channel))
    assert client.downloads == 1  # photos only: the channel post has no photo

    edited = _message(message_id=11, chat_id=42, text="hello edited", photo=True)
    await client.callback("MessageEdited")(SimpleNamespace(message=edited))
    await client.callback("MessageDeleted")(SimpleNamespace(deleted_ids=[11], chat_id=None))

    private_row = store.search(
        "hello", account="default", chat_id=42, include_deleted=True, limit=5
    )
    assert len(private_row) == 1
    assert private_row[0]["deleted"] is True
    assert private_row[0]["text"] == "hello edited"
    assert private_row[0]["reply_to_msg_id"] == 4
    assert private_row[0]["forward"]["from_name"] == "Bob"
    assert private_row[0]["media_saved"] is True
    channel_row = store.search(
        "channel post", account="default", chat_id=None, include_deleted=True, limit=5
    )
    assert channel_row[0]["deleted"] is False

    await client.callback("MessageDeleted")(SimpleNamespace(deleted_ids=[11], chat_id=-1005))
    channel_row = store.search(
        "channel post", account="default", chat_id=None, include_deleted=True, limit=5
    )
    assert channel_row[0]["deleted"] is True


@pytest.mark.asyncio
async def test_photos_only_does_not_download_documents(store):
    client = _Client()
    attach(client, store, "default")
    document = _message(message_id=2, chat_id=3, text="file", document=True)
    await client.callback("NewMessage")(SimpleNamespace(message=document))
    assert client.downloads == 0
    found = store.search("file", account="default", chat_id=None, include_deleted=True, limit=5)
    assert found[0]["media_type"] == "document"
    assert found[0]["media_saved"] is False


@pytest.mark.asyncio
async def test_archive_tools_read_the_database_only(monkeypatch, store):
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    snap = snapshot(
        _message(message_id=4, chat_id=42, text="find me later", sender_name="Анна"), "default"
    )
    store.upsert(snap, source="new")
    store.upsert(
        snapshot(
            _message(message_id=4, chat_id=42, text="find me edited", sender_name="Анна"),
            "default",
        ),
        source="edit",
    )
    store.mark_deleted("default", [4], channel_chat_id=None)

    empty = json.loads(
        await list_deleted_messages(
            chat_id=42, sender="анна", since="2020-01-01", until="2020-01-02"
        )
    )
    assert empty["count"] == 0
    listed = json.loads(
        await list_deleted_messages(
            chat_id=42, sender="анна", since="2020-01-01", until="2035-01-01"
        )
    )
    assert listed["count"] == 1
    assert listed["results"][0]["text"] == "find me edited"
    assert listed["capture"]["transport"] == "stdio"
    assert "disconnects" in listed["capture"]["note"]
    assert "did not contact Telegram" in listed["note"]

    found = json.loads(await search_archive(query="FIND ME", include_deleted=True))
    assert found["count"] == 1
    hidden = json.loads(await search_archive(query="find me", include_deleted=False))
    assert hidden["count"] == 0

    history = json.loads(await get_message_edit_history(chat_id=42, message_id=4))
    assert history["previous_versions"][0]["text"] == "find me later"
    assert history["message"]["deleted"] is True

    monkeypatch.setenv("MCP_TRANSPORT", "http")
    stats = json.loads(await archive_stats())
    assert stats["deleted"] == 1
    assert stats["edits"] == 1
    assert stats["capture"]["live"] is True
    assert capture_status()["transport"] == "http"


@pytest.mark.asyncio
async def test_search_archive_requires_a_query():
    assert "query" in await search_archive(query="  ")
