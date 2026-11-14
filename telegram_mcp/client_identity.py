"""Resolve the Telethon device identity used when connecting to Telegram.

These values are what Telegram shows in Settings > Devices (active sessions).
When they are not provided, Telethon falls back to the host platform (for
example ``arm64``), and because the values are re-sent on every connection,
a long-running server would otherwise overwrite the name chosen during login
on each reconnect. Making them configurable keeps a stable, recognisable name.
"""

import os

# Maps the Telethon constructor keyword to the environment variable that sets it.
_DEVICE_ENV = {
    "device_model": "TELEGRAM_DEVICE_MODEL",
    "system_version": "TELEGRAM_SYSTEM_VERSION",
    "app_version": "TELEGRAM_APP_VERSION",
    "lang_code": "TELEGRAM_LANG_CODE",
    "system_lang_code": "TELEGRAM_SYSTEM_LANG_CODE",
}


def client_identity_kwargs() -> dict:
    """Return TelegramClient device kwargs derived from the environment.

    Only variables that are set to a non-empty value are included, so unset
    ones keep Telethon's own defaults.
    """
    kwargs = {}
    for kwarg, env_var in _DEVICE_ENV.items():
        value = os.environ.get(env_var)
        if value:
            kwargs[kwarg] = value
    # IPv6-only hosts (e.g. a GCP VM without external IPv4) need Telethon to
    # use Telegram's IPv6 datacenter addresses.
    if os.environ.get("TELEGRAM_USE_IPV6", "").strip().lower() in ("1", "true", "yes", "on"):
        kwargs["use_ipv6"] = True
    return kwargs
