"""Admin flag contract: management-only, folder access comes from rules.

Spec (docs/superpowers/specs/2026-08-31-per-token-folder-access-design.md):
the ``admin`` flag means "may manage tokens and settings" ONLY. An admin
token with no rules gets the default read access like any other token; it
must not short-circuit the ``AccessPolicy`` path checks. REST and MCP
enforce the same contract (the MCP surface has no token/settings tools, so
only the mutation tools have MCP equivalents).
"""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.api.auth import hlm_mcp_token
from harbor_ledger_memory.api.mcp_server import build_mcp_server
from harbor_ledger_memory.config import (
    ApiSettings,
    FolderAccess,
    FolderRule,
    Settings,
)
from harbor_ledger_memory.services.activity import ActivityService
from harbor_ledger_memory.services.live_traversal import LiveTraversalPublisher
from harbor_ledger_memory.services.tokens import TokenService

_NO_WRITE = {"detail": "token has no write access"}
_PATH_NOT_WRITABLE = "path '{path}' not writable by this token"


def _settings(tmp_path: Path) -> Settings:
    """Vault with one globally writable folder (AI) and a fresh catalog DB."""
    (tmp_path / "AI").mkdir()
    return Settings(
        vault_path=tmp_path,
        folder_rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
        ),
        database_url=f"sqlite:///{tmp_path / 'admin.db'}",
        api=ApiSettings(enabled=True),
    )


def _admin_headers(app: Any) -> dict[str, str]:
    """Bearer headers for an admin token created with NO rules."""
    _, plaintext = app.state.token_service.create("boss", rules=(), admin=True)
    return {"Authorization": f"Bearer {plaintext}"}


def test_admin_without_rules_manages_tokens_and_settings(tmp_path: Path) -> None:
    """The admin flag still opens token management and settings writes."""
    app = create_app(_settings(tmp_path))
    headers = _admin_headers(app)
    with TestClient(app) as client:
        listed = client.get("/api/v1/tokens", headers=headers)
        assert listed.status_code == 200
        assert [token["name"] for token in listed.json()] == ["boss"]
        saved = client.put(
            "/api/v1/settings", json={"folder_rules": []}, headers=headers
        )
        assert saved.status_code == 200


def test_admin_without_rules_cannot_scan_or_write(tmp_path: Path) -> None:
    """With no rules, admin gets default read: scan and writes are 403s."""
    app = create_app(_settings(tmp_path))
    headers = _admin_headers(app)
    with TestClient(app) as client:
        scan = client.post("/api/v1/scan", headers=headers)
        assert scan.status_code == 403
        assert scan.json() == _NO_WRITE
        for path in ("AI/note.md", "Other/note.md"):
            resp = client.post(
                "/api/v1/writes",
                json={"path": path, "content": "# N"},
                headers=headers,
            )
            assert resp.status_code == 403
            assert resp.json() == {"detail": _PATH_NOT_WRITABLE.format(path=path)}


def _call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(name, arguments))


def test_mcp_admin_without_rules_denied_on_mutation_tools(tmp_path: Path) -> None:
    """MCP mirrors REST: scan/feedback need a write rule, writes need a path rule."""
    settings = _settings(tmp_path)
    activity = ActivityService(settings.database_url)
    service = TokenService(settings.database_url)
    try:
        transport, _ = build_mcp_server(
            settings, activity, service, live_traversal=LiveTraversalPublisher()
        )
        server = transport.state.mcp_server
        record, _ = service.create("boss", rules=(), admin=True)
        token = hlm_mcp_token.set(record)
        try:
            with pytest.raises(ToolError) as exc_info:
                _call(server, "scan", {})
            assert "token has no write access" in str(exc_info.value)
            with pytest.raises(ToolError) as exc_info:
                _call(
                    server,
                    "feedback",
                    {"trace_id": "missing-trace", "relevant_paths": ["AI/note.md"]},
                )
            assert "token has no write access" in str(exc_info.value)
            with pytest.raises(ToolError) as exc_info:
                _call(server, "propose_write", {"path": "AI/x.md", "content": "# X"})
            assert "path 'AI/x.md' not writable by this token" in str(exc_info.value)
        finally:
            hlm_mcp_token.reset(token)
    finally:
        activity.close()
        service.close()
