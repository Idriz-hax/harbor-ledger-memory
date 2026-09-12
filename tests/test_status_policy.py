"""Per-token status policy: status reflects the calling token's own grant.

Reviewer finding: ``token_status_payload`` reported vault-global facts, so two
tokens with different folder rules saw identical status. These tests pin the
corrected behavior: the status payload reports the CALLING token's own
``read_only`` flag, ``write_policy`` (its own rules), and
``effective_read_scope`` — and the REST status route and the MCP status tool
share that one corrected shape.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePosixPath

from fastapi.testclient import TestClient

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


def _settings(tmp_path: Path) -> Settings:
    (tmp_path / "Data").mkdir(parents=True, exist_ok=True)
    return Settings(
        HLM_VAULT_PATH=tmp_path,
        folder_rules=(
            FolderRule(path=PurePosixPath("Data"), access=FolderAccess.READ),
        ),
        database_url=f"sqlite:///{tmp_path / 'status_policy.db'}",
        api=ApiSettings(enabled=True),
    )


def test_rest_status_reports_each_tokens_own_policy(tmp_path: Path) -> None:
    """The REST status route reports the calling token's own policy."""
    settings = _settings(tmp_path)
    app = create_app(settings)
    service = app.state.token_service
    auto = service.create(
        "auto",
        rules=[FolderRule(path=PurePosixPath("Data"), access=FolderAccess.AUTO_WRITE)],
    ).plaintext
    none = service.create(
        "none",
        rules=[FolderRule(path=PurePosixPath("Data"), access=FolderAccess.NONE)],
    ).plaintext
    with TestClient(app) as client:
        a = client.get(
            "/api/v1/status", headers={"Authorization": f"Bearer {auto}"}
        ).json()
        b = client.get(
            "/api/v1/status", headers={"Authorization": f"Bearer {none}"}
        ).json()

    # A auto-writes Data: not read-only; its own auto-write rule is listed.
    assert a["read_only"] is False
    assert a["write_policy"] == {
        "default_access": "read",
        "rules": [{"path": "Data", "access": "auto-write"}],
    }
    assert a["effective_read_scope"] == "."
    # B has none access on Data: read-only; its own none rule is listed.
    assert b["read_only"] is True
    assert b["write_policy"] == {
        "default_access": "read",
        "rules": [{"path": "Data", "access": "none"}],
    }
    assert b["effective_read_scope"] == ". (deny: Data)"
    # The two tokens see different status.
    assert a["read_only"] != b["read_only"]
    assert a["write_policy"] != b["write_policy"]


def test_mcp_status_reports_each_tokens_own_policy(tmp_path: Path) -> None:
    """The MCP status tool reports the calling token's own policy."""
    settings = _settings(tmp_path)
    activity = ActivityService(settings.database_url)
    service = TokenService(settings.database_url)
    try:
        auto, _ = service.create(
            "auto",
            rules=[
                FolderRule(path=PurePosixPath("Data"), access=FolderAccess.AUTO_WRITE),
            ],
        )
        none, _ = service.create(
            "none",
            rules=[FolderRule(path=PurePosixPath("Data"), access=FolderAccess.NONE)],
        )
        transport, _ = build_mcp_server(
            settings, activity, service, live_traversal=LiveTraversalPublisher()
        )
        server = transport.state.mcp_server

        token = hlm_mcp_token.set(auto)
        try:
            a = json.loads(asyncio.run(server.call_tool("status", {})).content[0].text)
        finally:
            hlm_mcp_token.reset(token)

        token = hlm_mcp_token.set(none)
        try:
            b = json.loads(asyncio.run(server.call_tool("status", {})).content[0].text)
        finally:
            hlm_mcp_token.reset(token)
    finally:
        activity.close()
        service.close()

    assert a["read_only"] is False
    assert a["write_policy"] == {
        "default_access": "read",
        "rules": [{"path": "Data", "access": "auto-write"}],
    }
    assert a["effective_read_scope"] == "."
    assert b["read_only"] is True
    assert b["write_policy"] == {
        "default_access": "read",
        "rules": [{"path": "Data", "access": "none"}],
    }
    assert b["effective_read_scope"] == ". (deny: Data)"
