#!/usr/bin/env python3
"""QR login for the Telegram MCP service.

Prints a machine-readable QR_LOGIN_URL (tg://login?token=...) so a parent
process can render a QR image. On success it saves a Telethon session file
with mode 600 and never prints the session string.

This script is not started at install time. Run it only after TELEGRAM_API_ID
and TELEGRAM_API_HASH are set in the environment (see login-qr.sh).

Usage:
    /opt/telegram-mcp/venv/bin/python /opt/telegram-mcp/deploy/login_qr.py \\
        --session /opt/telegram-mcp/data/telegram_session

2FA: if a TTY is attached, the password is read with getpass. Otherwise the
script prints STATUS=need_2fa and waits for a password file (mode 600
recommended) at --password-file, then deletes that file after use.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from telethon import errors
from telethon.sync import TelegramClient

from telegram_mcp.client_identity import client_identity_kwargs
from telegram_mcp.install_guard import UnsafeInstallationError, assert_safe_distribution

_QR_MAX_REFRESHES = 10


def _log(msg: str) -> None:
    print(msg, flush=True)


def _check_installation() -> None:
    try:
        assert_safe_distribution()
    except UnsafeInstallationError as exc:
        _log(f"STATUS=error")
        print(str(exc), file=sys.stderr)
        sys.exit(1)


def _seconds_until_expiry(qr) -> float:
    expires = qr.expires
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    return max(1.0, remaining - 1.0)


def _announce_qr(qr) -> None:
    expires = qr.expires
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    _log(f"QR_LOGIN_URL={qr.url}")
    _log(f"QR_EXPIRES={expires.isoformat()}")
    _log("STATUS=waiting_for_scan")


def _read_2fa_password(password_file: Path, timeout: float) -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Two-factor authentication password: ")

    _log(f"PASSWORD_FILE={password_file}")
    _log("STATUS=need_2fa")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if password_file.is_file() and password_file.stat().st_size > 0:
            try:
                data = password_file.read_text(encoding="utf-8").rstrip("\n")
            finally:
                try:
                    password_file.unlink()
                except OSError:
                    pass
            if data:
                return data
        time.sleep(1)
    _log("STATUS=error")
    print("Timed out waiting for the 2FA password file.", file=sys.stderr)
    sys.exit(1)


def _harden_session_file(session_path: Path) -> None:
    session_file = (
        session_path
        if session_path.suffix == ".session"
        else session_path.with_name(session_path.name + ".session")
    )
    if session_file.is_file():
        os.chmod(session_file, stat.S_IRUSR | stat.S_IWUSR)
        _log(f"SESSION_FILE={session_file}")
    journal = Path(str(session_file) + "-journal")
    if journal.is_file():
        os.chmod(journal, stat.S_IRUSR | stat.S_IWUSR)


def _qr_login(client: TelegramClient, password_file: Path, password_timeout: float) -> None:
    qr = client.qr_login()
    _announce_qr(qr)

    for _ in range(_QR_MAX_REFRESHES):
        try:
            client.loop.run_until_complete(qr.wait(timeout=_seconds_until_expiry(qr)))
            return
        except asyncio.TimeoutError:
            client.loop.run_until_complete(qr.recreate())
            _log("STATUS=qr_expired")
            _announce_qr(qr)
        except errors.SessionPasswordNeededError:
            while True:
                pw = _read_2fa_password(password_file, password_timeout)
                try:
                    client.sign_in(password=pw)
                    return
                except errors.PasswordHashInvalidError:
                    _log("STATUS=need_2fa")
                    print("Invalid password, please try again.", file=sys.stderr)

    _log("STATUS=error")
    print("QR code expired too many times. Please run the generator again.", file=sys.stderr)
    client.disconnect()
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="QR-login and save a Telegram session file.")
    parser.add_argument(
        "--session",
        default=os.environ.get(
            "TELEGRAM_SESSION_NAME", "/opt/telegram-mcp/data/telegram_session"
        ),
        help="Telethon session path without requiring a .session suffix.",
    )
    parser.add_argument(
        "--password-file",
        default="/opt/telegram-mcp/data/.2fa_password",
        help="Path to write a 2FA password into when stdin is not a TTY.",
    )
    parser.add_argument(
        "--password-timeout",
        type=int,
        default=600,
        help="Seconds to wait for --password-file (default 600).",
    )
    args = parser.parse_args()

    _check_installation()

    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id_raw or not api_hash:
        _log("STATUS=error")
        print(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in the environment.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        api_id = int(api_id_raw)
    except ValueError:
        _log("STATUS=error")
        print("TELEGRAM_API_ID must be an integer.", file=sys.stderr)
        sys.exit(1)

    session_path = Path(args.session)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(session_path.parent, stat.S_IRWXU)
    except OSError:
        pass

    client = TelegramClient(
        str(session_path),
        api_id,
        api_hash,
        **client_identity_kwargs(),
    )
    try:
        client.connect()
        if client.is_user_authorized():
            _harden_session_file(session_path)
            _log("STATUS=already_authorized")
            return
        _qr_login(client, Path(args.password_file), args.password_timeout)
        if not client.is_user_authorized():
            _log("STATUS=error")
            print("Login finished but the session is not authorized.", file=sys.stderr)
            sys.exit(1)
        _harden_session_file(session_path)
        me = client.get_me()
        # Identity only, no session material.
        username = getattr(me, "username", None) or ""
        _log(f"STATUS=ok")
        if username:
            _log(f"TELEGRAM_USERNAME={username}")
    except Exception as exc:
        _log("STATUS=error")
        print(f"Login failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()
