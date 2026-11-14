#!/bin/bash
# QR login wrapper. Must be run as root so it can read /etc/telegram-mcp.env.
# Does not print secrets. Do not run until TELEGRAM_API_ID and TELEGRAM_API_HASH
# have been written into that env file.
set -euo pipefail

ENV_FILE="${TELEGRAM_ENV_FILE:-/etc/telegram-mcp.env}"
VENV_PY="/opt/telegram-mcp/venv/bin/python"
SCRIPT="/opt/telegram-mcp/deploy/login_qr.py"
SESSION="${TELEGRAM_SESSION_NAME:-/opt/telegram-mcp/data/telegram_session}"
PASSWORD_FILE="/opt/telegram-mcp/data/.2fa_password"
USER_NAME="telegram-mcp"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 1
fi

# Load KEY=value lines, ignoring comments and blanks. Values are not printed.
while IFS= read -r line || [[ -n "$line" ]]; do
  case "$line" in
    ''|\#*) continue ;;
  esac
  key="${line%%=*}"
  value="${line#*=}"
  case "$key" in
    TELEGRAM_API_ID|TELEGRAM_API_HASH|TELEGRAM_SESSION_NAME|TELEGRAM_DEVICE_MODEL|TELEGRAM_SYSTEM_VERSION|TELEGRAM_APP_VERSION|TELEGRAM_LANG_CODE|TELEGRAM_SYSTEM_LANG_CODE|TELEGRAM_USE_IPV6)
      export "$key=$value"
      ;;
  esac
done < "$ENV_FILE"

if [[ -z "${TELEGRAM_API_ID:-}" || -z "${TELEGRAM_API_HASH:-}" ]]; then
  echo "TELEGRAM_API_ID and TELEGRAM_API_HASH are still empty in $ENV_FILE" >&2
  exit 1
fi

SESSION="${TELEGRAM_SESSION_NAME:-$SESSION}"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 700 /opt/telegram-mcp/data

# If a stale password file exists, do not leave it around.
if [[ -f "$PASSWORD_FILE" ]]; then
  rm -f "$PASSWORD_FILE"
fi

# setpriv, not sudo: sudo would write the preserved environment (including
# TELEGRAM_API_HASH) into the auth log and the journal.
exec setpriv --reuid="$USER_NAME" --regid="$USER_NAME" --init-groups \
  "$VENV_PY" "$SCRIPT" --session "$SESSION" --password-file "$PASSWORD_FILE" "$@"
