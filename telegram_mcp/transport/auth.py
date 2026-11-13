"""Bearer-token check for the HTTP transports.

The upstream HTTP endpoint does not authenticate callers. This fork requires
``MCP_AUTH_TOKEN`` whenever the server is not bound to localhost. A localhost
bind may omit the token. When the token is set, every HTTP request must send
``Authorization: Bearer <token>``, including localhost.
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Optional

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def auth_token() -> Optional[str]:
    """The configured bearer token, or None when HTTP auth is off."""
    raw = os.getenv("MCP_AUTH_TOKEN", "").strip()
    return raw or None


def bind_is_local(host: Optional[str]) -> bool:
    """True for a loopback bind. ``0.0.0.0`` and public hostnames are not local."""
    value = (host or "").strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value in _LOCAL_HOSTS


def require_auth_token(host: Optional[str]) -> None:
    """Abort startup when a non-local HTTP bind has no bearer token."""
    if bind_is_local(host) or auth_token():
        return
    raise SystemExit(
        "MCP_AUTH_TOKEN is required when MCP_HOST is not localhost "
        f"(got '{host}'). The HTTP endpoint would otherwise accept anyone who "
        "can reach the port. Set MCP_AUTH_TOKEN to a long random secret, or "
        "bind MCP_HOST=127.0.0.1."
    )


def _presented_token(header: str) -> str:
    scheme, separator, rest = header.strip().partition(" ")
    if separator != " " or scheme.lower() != "bearer":
        return ""
    token = rest.strip()
    if not token or " " in token:
        return ""
    return token


def token_matches(header: str, expected: str) -> bool:
    presented = _presented_token(header)
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)


class BearerAuthMiddleware:
    """Reject HTTP requests that do not carry the configured bearer token.

    When ``MCP_AUTH_TOKEN`` is unset the middleware does nothing, which is
    only safe because startup refuses a non-local bind without a token.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        expected = auth_token()
        if not expected:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        if token_matches(raw, expected):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


def install_bearer_auth() -> None:
    """Wrap FastMCP's HTTP and SSE apps so every request is checked."""
    try:
        from mcp.server.fastmcp import FastMCP
    except (ImportError, AttributeError):
        return
    if getattr(FastMCP, "_bearer_auth_patched", False):
        return

    original_http = FastMCP.streamable_http_app
    original_sse = FastMCP.sse_app

    def streamable_http_app(self: Any, *args: Any, **kwargs: Any) -> Any:
        return BearerAuthMiddleware(original_http(self, *args, **kwargs))

    def sse_app(self: Any, *args: Any, **kwargs: Any) -> Any:
        return BearerAuthMiddleware(original_sse(self, *args, **kwargs))

    FastMCP.streamable_http_app = streamable_http_app  # type: ignore[method-assign]
    FastMCP.sse_app = sse_app  # type: ignore[method-assign]
    FastMCP._bearer_auth_patched = True  # type: ignore[attr-defined]
