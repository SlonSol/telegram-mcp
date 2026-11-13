"""Hard read-only lock for this fork.

``TELEGRAM_HARD_READONLY`` defaults to on. Set it to ``0`` to restore upstream
write behaviour. While the lock is on:

* mutating MCP tools are not registered, whatever ``TELEGRAM_EXPOSED_TOOLS`` says
* ``TelegramClient._call`` refuses any MTProto request that is not a read
* voice notes are not uploaded to Groq or OpenAI

An idle Telethon client does not send ``account.updateStatus``. The lock
refuses that request as well, so this process never sets an online flag of
its own. Telegram may still treat a connected session as online; that is the
same as any idle client that stays connected to receive updates.

Requests the lock allows that still touch Telegram-side state. None of them
mark messages, mentions, reactions or stories as read, and none of them send,
edit, delete or change the profile:

* ``auth.ExportAuthorization`` / ``auth.ImportAuthorization`` — a short-lived
  key so a file stored on another datacenter can be downloaded
* ``upload.ReuploadCdnFile`` — ask Telegram to copy a CDN file onto a
  datacenter this client can download from
* ``messages.TranscribeAudio`` — Telegram's own transcription. The audio
  stays inside Telegram. Groq and OpenAI uploads are blocked separately.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

_ON = {"1", "true", "yes", "on"}
_OFF = {"0", "false", "no", "off"}

# Wrappers. The inner ``query`` is checked; the wrapper itself is not an action.
_WRAPPERS = frozenset(
    {
        "InvokeWithLayerRequest",
        "InitConnectionRequest",
        "InvokeWithoutUpdatesRequest",
        "InvokeAfterMsgRequest",
        "InvokeAfterMsgsRequest",
        "InvokeWithMessagesRangeRequest",
        "InvokeWithTakeoutRequest",
        "InvokeWithBusinessConnectionRequest",
        "InvokeWithGooglePlayIntegrityRequest",
        "InvokeWithApnsSecretRequest",
        "InvokeWithReCaptchaRequest",
    }
)

# Looks like a getter but changes state, or is a story/sponsored/password call
# the owner must not make. Names are the Telethon TLRequest class names.
_EXPLICIT_DENY = frozenset(
    {
        "CheckPasswordRequest",
        "CheckRecoveryPasswordRequest",
        "GetTmpPasswordRequest",
        "GetBotCallbackAnswerRequest",
        "GetCallbackAnswerRequest",
        "GetRequestedWebViewButtonRequest",
        "GetMessagesViewsRequest",
        "GetSponsoredMessagesRequest",
        "GetSponsoredPeersRequest",
        "ViewSponsoredMessageRequest",
        "ClickSponsoredMessageRequest",
        "GetLocatedRequest",
        "ReceivedMessagesRequest",
        "UpdateStatusRequest",
        "InitTakeoutSessionRequest",
        "ReadHistoryRequest",
        "ReadDiscussionRequest",
        "ReadMentionsRequest",
        "ReadReactionsRequest",
        "ReadMessageContentsRequest",
        "ReadStoriesRequest",
        "ReadFeaturedStickersRequest",
        "ReadSavedHistoryRequest",
        "IncrementStoryViewsRequest",
        "GetStoriesViewsRequest",
        "GetAllStoriesRequest",
        "GetAllReadPeerStoriesRequest",
    }
)

# Whole namespaces: stories (viewing/reading), calls, payments, SMS jobs.
_DENY_NAMESPACES = frozenset({"stories", "phone", "payments", "smsjobs"})

_READ_PREFIXES = ("Get", "Search", "Check", "Resolve")

# Not a Get/Search/Check/Resolve, but required for a connected read-only client
# to download media or transcribe inside Telegram. See the module docstring.
_EXPLICIT_ALLOW = frozenset(
    {
        "TranscribeAudioRequest",
        "ExportAuthorizationRequest",
        "ImportAuthorizationRequest",
        "ReuploadCdnFileRequest",
        # Connection keepalive and the initial ping go through the sender, not
        # ``_call``. Allowed here so a manual ping cannot trip the lock.
        "PingRequest",
        "PingDelayDisconnectRequest",
    }
)

_THIRD_PARTY_ENGINES = frozenset({"groq", "openai"})


class HardReadonlyError(PermissionError):
    """Raised when a Telegram request would change state while the lock is on."""

    def __init__(self, request: Any):
        name = type(request).__name__
        self.request_name = name
        super().__init__(
            f"TELEGRAM_HARD_READONLY refused {name}. This fork does not send, "
            "edit, delete, forward, react, mark messages read, view or read "
            "stories, join or leave chats, or change the profile or settings."
        )


def hard_readonly_enabled() -> bool:
    """Return True unless ``TELEGRAM_HARD_READONLY`` is explicitly off.

    Unset or blank means on. A typo aborts startup instead of silently
    unlocking writes.
    """
    raw = os.getenv("TELEGRAM_HARD_READONLY")
    if raw is None or not raw.strip():
        return True
    value = raw.strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    raise SystemExit(
        f"Invalid TELEGRAM_HARD_READONLY '{raw}'. Expected one of: "
        "1, true, yes, on, 0, false, no, off."
    )


def third_party_upload_blocked(engine: str) -> Optional[str]:
    """Error text when ``engine`` would upload audio off the machine, else None."""
    if engine not in _THIRD_PARTY_ENGINES or not hard_readonly_enabled():
        return None
    return (
        "TELEGRAM_HARD_READONLY is on, so recordings are not uploaded to "
        f"{engine}. Use engine='telegram' (stays inside Telegram) or "
        "engine='whisper' (stays on this machine)."
    )


def forced_exposure(
    base: str,
    allowlist: Iterable[str],
    exclusions: Iterable[str],
    readonly_names: Iterable[str],
) -> tuple[str, list[str], list[str]]:
    """Force a read-only tool surface while the lock is on.

    The ``+`` allowlist is dropped, so naming ``send_message`` cannot register
    it. Exclusions of tools the read-only base already hides are dropped too:
    that is redundant, not a startup error. Exclusions of read-only tools
    still apply.
    """
    allow = list(allowlist)
    excluded = list(exclusions)
    if not hard_readonly_enabled():
        return base, allow, excluded
    readonly = set(readonly_names)
    kept = [name for name in excluded if name in readonly]
    return "read-only", [], kept


def _namespace(request: Any) -> str:
    return type(request).__module__.rsplit(".", 1)[-1]


def _request_allowed(name: str, namespace: str) -> bool:
    if name in _EXPLICIT_DENY or namespace in _DENY_NAMESPACES:
        return False
    if name in _EXPLICIT_ALLOW:
        return True
    return name.startswith(_READ_PREFIXES)


def is_readonly_request(request: Any) -> bool:
    """True when ``request`` is on the read-only allowlist."""
    if isinstance(request, (list, tuple)):
        return all(is_readonly_request(item) for item in request)
    name = type(request).__name__
    if name in _WRAPPERS:
        inner = getattr(request, "query", None)
        return inner is not None and is_readonly_request(inner)
    return _request_allowed(name, _namespace(request))


def assert_requests_allowed(request: Any) -> None:
    """Raise ``HardReadonlyError`` unless every request is read-only."""
    if isinstance(request, (list, tuple)):
        for item in request:
            assert_requests_allowed(item)
        return
    if not is_readonly_request(request):
        raise HardReadonlyError(request)


def install_client_guard(client: Any, *, force: bool = False) -> Any:
    """Refuse mutating MTProto calls on ``client`` while the lock is on.

    Patches ``client._call``, which is the choke point for ``client(request)``
    and the high-level helpers (``send_message``, ``send_read_acknowledge``,
    ``download_media``, ``get_messages``, ``get_dialogs``). Connection setup
    and the keepalive ping use the sender directly and are not user actions.
    """
    if not force and not hard_readonly_enabled():
        return client
    if getattr(client, "_hard_readonly_installed", False):
        return client
    original = client._call

    async def guarded_call(sender, request, ordered=False, flood_sleep_threshold=None):
        assert_requests_allowed(request)
        return await original(
            sender,
            request,
            ordered=ordered,
            flood_sleep_threshold=flood_sleep_threshold,
        )

    client._call = guarded_call
    client._hard_readonly_installed = True
    return client
