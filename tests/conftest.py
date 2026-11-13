"""Shared pytest setup for import-time Telegram configuration."""

import os

import pytest

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")
os.environ.setdefault("TELEGRAM_SESSION_NAME", "test_session")
# This fork defaults the hard lock ON. The suite exercises upstream exposure
# modes, so tests opt out unless they turn the lock on themselves.
os.environ.setdefault("TELEGRAM_HARD_READONLY", "0")


@pytest.fixture
def transcript_cache_dir(tmp_path, monkeypatch):
    """Isolated, per-test SQLite transcript cache directory."""
    d = tmp_path / "transcripts"
    monkeypatch.setenv("TELEGRAM_TRANSCRIPT_CACHE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _isolate_archive_dir(tmp_path, monkeypatch):
    """Keep the deleted-message archive out of the working tree."""
    root = tmp_path / "telegram-mcp-data"
    monkeypatch.setenv("TELEGRAM_DATA_DIR", str(root))
    monkeypatch.setenv("TELEGRAM_ARCHIVE_DB", str(root / "archive.sqlite"))
    monkeypatch.setenv("TELEGRAM_ARCHIVE_MEDIA_DIR", str(root / "media"))
    yield
    try:
        from telegram_mcp.archive.store import reset_store

        reset_store()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clear_expected_username(monkeypatch):
    """Keep a developer's TELEGRAM_EXPECTED_USERNAME* (.env) away from fake clients."""
    for key in list(os.environ):
        if key.startswith("TELEGRAM_EXPECTED_USERNAME"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _reset_floodgate():
    """Empty the shared floodgate buckets between tests so calls recorded in
    one test never defer a tool call in another."""
    from telegram_mcp import runtime

    yield
    runtime._FLOOD_SEEN.set(False)  # direct report_flood calls otherwise leak
    if runtime._floodgate_gate is not None:
        runtime._floodgate_gate.reset()
    if runtime._floodgate_breaker is not None:
        runtime._floodgate_breaker.reset()
