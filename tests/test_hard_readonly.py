"""The hard read-only lock must survive TELEGRAM_EXPOSED_TOOLS and bad requests."""

import inspect
import pkgutil
import importlib

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from telethon.tl import TLRequest
from telethon.tl import functions

from telegram_mcp import hard_readonly, runtime
from telegram_mcp import runner


def _fake(name: str, namespace: str = "messages", **attrs):
    cls = type(name, (), {"__module__": f"telethon.tl.functions.{namespace}"})
    obj = cls()
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def _request_classes():
    modules = [functions]
    for module in pkgutil.iter_modules(functions.__path__):
        modules.append(importlib.import_module(f"telethon.tl.functions.{module.name}"))
    found = []
    for module in modules:
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, TLRequest) and obj is not TLRequest:
                found.append(obj)
    return found


def _allowed(cls) -> bool:
    namespace = cls.__module__.rsplit(".", 1)[-1]
    return hard_readonly._request_allowed(cls.__name__, namespace)


_MUST_DENY = [
    "messages.ReadHistoryRequest",
    "channels.ReadHistoryRequest",
    "messages.ReadMentionsRequest",
    "messages.ReadReactionsRequest",
    "messages.ReadDiscussionRequest",
    "messages.ReadMessageContentsRequest",
    "stories.ReadStoriesRequest",
    "stories.IncrementStoryViewsRequest",
    "stories.GetStoriesViewsRequest",
    "stories.GetAllStoriesRequest",
    "messages.GetMessagesViewsRequest",
    "messages.ReceivedMessagesRequest",
    "messages.SendMessageRequest",
    "messages.EditMessageRequest",
    "messages.DeleteMessagesRequest",
    "messages.DeleteHistoryRequest",
    "messages.ForwardMessagesRequest",
    "messages.SendReactionRequest",
    "messages.SendVoteRequest",
    "messages.SaveDraftRequest",
    "messages.SetTypingRequest",
    "messages.GetBotCallbackAnswerRequest",
    "channels.DeleteMessagesRequest",
    "channels.JoinChannelRequest",
    "channels.LeaveChannelRequest",
    "messages.ImportChatInviteRequest",
    "account.UpdateProfileRequest",
    "account.UpdateStatusRequest",
    "account.SetPrivacyRequest",
    "photos.UploadProfilePhotoRequest",
    "photos.DeletePhotosRequest",
    "messages.GetInlineBotResultsRequest",
    "messages.GetWebPagePreviewRequest",
    "channels.SearchPostsRequest",
    "messages.GetBotAppRequest",
    "messages.GetAttachMenuBotRequest",
]

_MUST_ALLOW = [
    "updates.GetStateRequest",
    "updates.GetDifferenceRequest",
    "updates.GetChannelDifferenceRequest",
    "help.GetConfigRequest",
    "help.GetNearestDcRequest",
    "messages.GetDialogsRequest",
    "messages.GetHistoryRequest",
    "messages.GetMessagesRequest",
    "messages.SearchRequest",
    "messages.SearchGlobalRequest",
    "messages.GetPeerDialogsRequest",
    "messages.GetRepliesRequest",
    "messages.GetDiscussionMessageRequest",
    "messages.GetUnreadMentionsRequest",
    "messages.GetUnreadReactionsRequest",
    "messages.GetAllDraftsRequest",
    "messages.GetFullChatRequest",
    "channels.GetMessagesRequest",
    "channels.GetFullChannelRequest",
    "users.GetUsersRequest",
    "users.GetFullUserRequest",
    "contacts.ResolveUsernameRequest",
    "contacts.GetContactsRequest",
    "contacts.SearchRequest",
    "upload.GetFileRequest",
    "photos.GetUserPhotosRequest",
    "messages.TranscribeAudioRequest",
    "auth.ExportAuthorizationRequest",
    "auth.ImportAuthorizationRequest",
    "upload.ReuploadCdnFileRequest",
]

_MUTATING_PREFIXES = (
    "Send",
    "Edit",
    "Delete",
    "Forward",
    "Join",
    "Leave",
    "Update",
    "Set",
    "Toggle",
    "Read",
    "Upload",
    "Save",
    "Install",
    "Block",
    "Unblock",
    "Import",
    "Create",
    "Hide",
    "Report",
    "Vote",
    "React",
    "Mark",
    "Increment",
    "Destroy",
    "Drop",
    "Reset",
    "Accept",
    "Decline",
    "Invite",
    "Migrate",
    "Fave",
    "Click",
    "View",
)


def _by_name():
    return {
        f"{cls.__module__.rsplit('.', 1)[-1]}.{cls.__name__}": cls for cls in _request_classes()
    }


def test_hard_readonly_defaults_on(monkeypatch):
    monkeypatch.delenv("TELEGRAM_HARD_READONLY", raising=False)
    assert hard_readonly.hard_readonly_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_hard_readonly_can_be_turned_off(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_HARD_READONLY", value)
    assert hard_readonly.hard_readonly_enabled() is False


def test_hard_readonly_rejects_typos(monkeypatch):
    monkeypatch.setenv("TELEGRAM_HARD_READONLY", "maybe")
    with pytest.raises(SystemExit, match="TELEGRAM_HARD_READONLY"):
        hard_readonly.hard_readonly_enabled()


def test_read_paths_are_allowed_and_read_receipts_are_not():
    classes = _by_name()
    missing = [name for name in _MUST_ALLOW + _MUST_DENY if name not in classes]
    assert not missing
    for name in _MUST_ALLOW:
        assert _allowed(classes[name]), name
    for name in _MUST_DENY:
        assert not _allowed(classes[name]), name


def test_stories_calls_payments_are_never_allowed():
    for cls in _request_classes():
        namespace = cls.__module__.rsplit(".", 1)[-1]
        if namespace in {"stories", "phone", "payments", "smsjobs"}:
            assert not _allowed(cls), cls.__name__


def test_allowlist_has_no_mutating_prefix_outside_the_documented_exceptions():
    """A new TL method whose name starts with Send/Read/Edit/... must not slip through."""
    allowed_exceptions = set(hard_readonly._EXPLICIT_ALLOW)
    for cls in _request_classes():
        if not _allowed(cls):
            continue
        assert (
            not cls.__name__.startswith(_MUTATING_PREFIXES) or cls.__name__ in allowed_exceptions
        )


def test_wrappers_are_judged_by_the_inner_request():
    inner_ok = _fake("GetStateRequest", "updates")
    inner_bad = _fake("ReadHistoryRequest", "messages")
    allowed = _fake("InvokeWithoutUpdatesRequest", "functions", query=inner_ok)
    refused = _fake("InvokeWithLayerRequest", "functions", query=inner_bad)
    assert hard_readonly.is_readonly_request(allowed)
    assert not hard_readonly.is_readonly_request(refused)
    assert not hard_readonly.is_readonly_request([inner_ok, inner_bad])


@pytest.mark.asyncio
async def test_client_guard_blocks_read_receipts_and_allows_history():
    class Client:
        def __init__(self):
            self.sent = []

        async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
            self.sent.append(type(request).__name__)
            return "ok"

    client = Client()
    hard_readonly.install_client_guard(client, force=True)
    history = _fake("GetHistoryRequest")
    assert await client._call(None, history) == "ok"
    assert client.sent == ["GetHistoryRequest"]

    for name, namespace in (
        ("ReadHistoryRequest", "messages"),
        ("ReadHistoryRequest", "channels"),
        ("ReadMentionsRequest", "messages"),
        ("ReadReactionsRequest", "messages"),
        ("ReadDiscussionRequest", "messages"),
        ("ReadStoriesRequest", "stories"),
        ("GetMessagesViewsRequest", "messages"),
        ("UpdateStatusRequest", "account"),
        ("SendMessageRequest", "messages"),
    ):
        with pytest.raises(hard_readonly.HardReadonlyError):
            await client._call(None, _fake(name, namespace))
    assert client.sent == ["GetHistoryRequest"]


def test_guard_is_installed_on_new_clients_when_the_lock_is_on(monkeypatch):
    monkeypatch.setenv("TELEGRAM_HARD_READONLY", "1")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self._call = self._raw

        async def _raw(self, *args, **kwargs):
            return "sent"

    monkeypatch.setattr(runtime, "TelegramClient", FakeClient)
    client = runtime._build_client("dummy_session", "default")
    assert client._hard_readonly_installed is True


def _synthetic_server():
    server = FastMCP("hard-readonly-test")

    @server.tool(annotations=ToolAnnotations(title="Read", readOnlyHint=True))
    def get_messages():
        return "read"

    @server.tool(annotations=ToolAnnotations(title="Write", destructiveHint=True))
    def mark_as_read():
        return "write"

    @server.tool(annotations=ToolAnnotations(title="Send", destructiveHint=True))
    def send_message():
        return "send"

    return server


@pytest.mark.parametrize(
    "mode", ["all", "read-only+mark_as_read,send_message", "all-mark_as_read"]
)
def test_hard_readonly_hides_mutating_tools_whatever_exposure_says(monkeypatch, mode):
    monkeypatch.setenv("TELEGRAM_HARD_READONLY", "1")
    server = _synthetic_server()

    hidden, excluded = runtime._apply_exposed_tools_mode(server, mode)

    assert {tool.name for tool in server._tool_manager.list_tools()} == {"get_messages"}
    assert "mark_as_read" in hidden
    assert "send_message" in hidden
    assert excluded == []


def test_real_read_tools_stay_visible_and_mutating_tools_are_not_annotated_readonly():
    registered = {tool.name: tool for tool in runtime.mcp._tool_manager.list_tools()}
    for name in (
        "get_messages",
        "list_messages",
        "get_history",
        "list_chats",
        "get_me",
        "list_deleted_messages",
        "search_archive",
        "get_message_edit_history",
        "archive_stats",
    ):
        assert registered[name].annotations.readOnlyHint is True
    for name in (
        "mark_as_read",
        "mark_topic_as_read",
        "send_message",
        "edit_message",
        "delete_message",
        "delete_messages_bulk",
        "forward_message",
        "send_reaction",
    ):
        assert registered[name].annotations.readOnlyHint is not True


def _deepest(fn):
    seen = set()
    while getattr(fn, "__wrapped__", None) is not None and id(fn) not in seen:
        seen.add(id(fn))
        fn = fn.__wrapped__
    return fn


_READ_STATUS_CALLS = (
    "send_read_acknowledge",
    "ReadHistoryRequest",
    "ReadDiscussionRequest",
    "ReadMentionsRequest",
    "ReadReactionsRequest",
    "ReadStoriesRequest",
    "ReadMessageContentsRequest",
    "GetMessagesViewsRequest",
    "IncrementStoryViews",
    "UpdateStatusRequest",
    "ReceivedMessagesRequest",
)


def test_registered_read_tools_do_not_mark_anything_read():
    for tool in runtime.mcp._tool_manager.list_tools():
        if not getattr(tool.annotations, "readOnlyHint", False):
            continue
        source = inspect.getsource(_deepest(tool.fn))
        for snippet in _READ_STATUS_CALLS:
            assert snippet not in source, f"{tool.name} contains {snippet}"


def test_startup_warms_dialogs_and_does_not_mark_read():
    source = inspect.getsource(runner._main)
    assert "get_dialogs" in source
    for snippet in _READ_STATUS_CALLS:
        assert snippet not in source


def test_archive_collector_does_not_mark_read():
    from telegram_mcp.archive import collector

    source = inspect.getsource(collector)
    for snippet in _READ_STATUS_CALLS:
        assert snippet not in source


@pytest.mark.asyncio
async def test_hard_readonly_blocks_third_party_transcription(monkeypatch):
    from telegram_mcp import transcription

    monkeypatch.setenv("TELEGRAM_HARD_READONLY", "1")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE_OPENAI_URL", "https://api.openai.com/v1")

    class Client:
        async def download_media(self, *args, **kwargs):
            raise AssertionError("audio was downloaded for upload")

    for engine in ("groq", "openai"):
        message = transcription.engine_config_error(engine)
        assert message and "TELEGRAM_HARD_READONLY" in message
        result = await transcription.transcribe(Client(), None, object(), engine)
        assert result["status"] == "error"
        assert "TELEGRAM_HARD_READONLY" in result["error"]

    assert transcription.engine_config_error("telegram") is None
    whisper_error = transcription.engine_config_error("whisper")
    assert whisper_error is None or "HARD_READONLY" not in whisper_error
