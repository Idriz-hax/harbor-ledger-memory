"""MCP lock visibility: actionable 401s and /health token state.

The ``/mcp`` sub-app is always locked by bearer token. When a client's token
is missing or revoked, the failure must never be silent: the 401 body carries
an actionable ``hint``, and ``/health`` reports MCP enabledness plus the
number of active (non-revoked) tokens so an operator can see a locked
``/mcp`` at a glance.

All tests run against an isolated per-test SQLite file (explicit
``database_url`` under ``tmp_path``) — never the live config database.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from fastapi.testclient import TestClient

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.config import (
    FolderAccess,
    FolderRule,
    McpSettings,
    Settings,
)

_HINT = (
    "No active API token matches this bearer token. Create one with "
    "`hlm token create` and set it as the MCP client's Authorization "
    "bearer."
)
_MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "host": "127.0.0.1:8000",
}
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


def _settings(tmp_path: Path, mcp_enabled: bool = True) -> Settings:
    return Settings(
        vault_path=tmp_path,
        folder_rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
        ),
        database_url=f"sqlite:///{tmp_path / 'mcp_disable.db'}",
        mcp=McpSettings(enabled=mcp_enabled),
    )


def test_mcp_401_without_bearer_is_actionable(tmp_path: Path) -> None:
    """A bare /mcp request is 401 with the existing fields plus a hint."""
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post("/mcp/", json=_INITIALIZE, headers=_MCP_HEADERS)
    assert response.status_code == 401
    body = response.json()
    assert body["detail"] == "authentication required"
    assert body["token_required"] is True
    assert body["hint"] == _HINT


def test_mcp_401_with_invalid_bearer_is_actionable(tmp_path: Path) -> None:
    """A bearer token that matches no active row is 401 with the hint."""
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            json=_INITIALIZE,
            headers={**_MCP_HEADERS, "Authorization": "Bearer hlm_does-not-exist"},
        )
    assert response.status_code == 401
    body = response.json()
    assert body["detail"] == "authentication required"
    assert body["token_required"] is True
    assert body["hint"] == _HINT


def test_health_reports_mcp_enabled_and_active_token_count(tmp_path: Path) -> None:
    """/health surfaces mcp.enabled and the count of non-revoked tokens."""
    settings = _settings(tmp_path, mcp_enabled=True)
    app = create_app(settings)
    service = app.state.token_service
    service.create("first", rules=(), admin=True)
    service.create("second", rules=())
    service.create("third", rules=())
    service.revoke("third")  # revoked rows must not count as active

    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["read_only"], bool)
    assert body["mcp"] == {"enabled": True, "active_tokens": 2}


def test_health_reports_zero_active_tokens_when_mcp_enabled(tmp_path: Path) -> None:
    """mcp.enabled with no tokens: /health shows the lock before it bites."""
    app = create_app(_settings(tmp_path, mcp_enabled=True))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["mcp"] == {"enabled": True, "active_tokens": 0}


def test_health_reports_mcp_disabled_state(tmp_path: Path) -> None:
    """With mcp disabled the /mcp mount is absent and /health says so."""
    app = create_app(_settings(tmp_path, mcp_enabled=False))
    with TestClient(app) as client:
        assert client.get("/mcp").status_code == 404
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["mcp"] == {"enabled": False, "active_tokens": 0}
