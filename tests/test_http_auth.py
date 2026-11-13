"""HTTP bearer auth: required off localhost, checked on every request when set."""

import pytest
from starlette.testclient import TestClient

from telegram_mcp.runtime import mcp
from telegram_mcp.transport.auth import bind_is_local, require_auth_token
from telegram_mcp.transport.http import create_streamable_http_app
from telegram_mcp import runner


def test_bind_is_local_only_for_loopback():
    assert bind_is_local("127.0.0.1")
    assert bind_is_local("localhost")
    assert bind_is_local("::1")
    assert bind_is_local("[::1]")
    assert not bind_is_local("0.0.0.0")
    assert not bind_is_local("10.0.0.5")


def test_public_bind_without_a_token_refuses_to_start(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="MCP_AUTH_TOKEN"):
        require_auth_token("0.0.0.0")
    require_auth_token("127.0.0.1")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret")
    require_auth_token("0.0.0.0")


@pytest.mark.asyncio
async def test_serve_refuses_public_bind_without_a_token(monkeypatch):
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="MCP_AUTH_TOKEN"):
        await runner._serve("http")


def test_bearer_token_gates_the_http_app(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "s3cret-token")
    app = create_streamable_http_app(mcp)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        refused = client.get("/mcp")
        assert refused.status_code == 401
        wrong = client.get("/mcp", headers={"Authorization": "Bearer no"})
        assert wrong.status_code == 401
        ok = client.get("/mcp", headers={"Authorization": "Bearer s3cret-token"})
        assert ok.status_code == 200
        assert "protocolVersions" in ok.json()


def test_requests_without_a_token_stay_open_on_the_default_config(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    app = create_streamable_http_app(mcp)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/mcp")
        assert response.status_code == 200
