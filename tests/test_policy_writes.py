"""Policy-driven write decisions: the token's own AccessPolicy wins.

The global ``settings.folder_rules`` are a draft template for the token
UI and never drive enforcement: auto-apply, propose, and approve all
follow the caller's per-token folder rules (the boundary's live rules
only act as a legacy fallback when no policy is supplied).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.api.auth import hlm_mcp_token
from harbor_ledger_memory.api.mcp_server import build_mcp_server
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.config import (
    ApiSettings,
    FolderAccess,
    FolderRule,
    Settings,
)
from harbor_ledger_memory.services.access import AccessPolicy
from harbor_ledger_memory.services.activity import ActivityService
from harbor_ledger_memory.services.tokens import TokenService
from harbor_ledger_memory.services.vault_mutations import VaultMutationService, VaultWriteDenied

_MANAGED = "---\nmanaged_by: harbor-ledger-memory\n---\n# Note\nv1\n"


def _settings(tmp_path: Path, draft: tuple[FolderRule, ...] | None = None) -> Settings:
    """Build settings whose global folder rules act only as a UI draft."""
    if draft is None:
        draft = (
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
        )
    for rule in draft:
        (tmp_path / rule.path.as_posix()).mkdir(parents=True, exist_ok=True)
    return Settings(
        vault_path=tmp_path,
        folder_rules=draft,
        database_url=f"sqlite:///{tmp_path / 'policy_writes.db'}",
        api=ApiSettings(enabled=True),
    )


def _headers(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}


def test_token_auto_write_beats_draft_read(tmp_path: Path) -> None:
    """A token with AUTO_WRITE auto-applies even when the draft is read-only."""
    settings = _settings(
        tmp_path,
        draft=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
        ),
    )
    (tmp_path / "AI" / "note.md").write_text(_MANAGED, encoding="utf-8")
    app = create_app(settings)
    _, plaintext = app.state.token_service.create(
        "auto",
        rules=[
            FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE)
        ],
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/writes",
            json={"path": "AI/note.md", "content": _MANAGED.replace("v1", "v2")},
            headers=_headers(plaintext),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
    assert (tmp_path / "AI" / "note.md").read_text(encoding="utf-8") == (
        _MANAGED.replace("v1", "v2")
    )


def test_token_propose_write_beats_draft_auto(tmp_path: Path) -> None:
    """A propose-only token queues pending even when the draft auto-writes."""
    settings = _settings(
        tmp_path,
        draft=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.AUTO_WRITE),
        ),
    )
    app = create_app(settings)
    _, plaintext = app.state.token_service.create(
        "writer",
        rules=[
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE)
        ],
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/writes",
            json={"path": "AI/queued.md", "content": "# Queued"},
            headers=_headers(plaintext),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"
        assert not (tmp_path / "AI" / "queued.md").exists()


def test_approver_policy_blocks_approval(tmp_path: Path) -> None:
    """A read-only approver 403s the approval; the proposal stays pending."""
    settings = _settings(tmp_path)  # draft: AI -> READ
    app = create_app(settings)
    token_service = app.state.token_service
    _, writer = token_service.create(
        "writer",
        rules=[
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE)
        ],
    )
    _, reader = token_service.create(
        "reader",
        rules=[FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ)],
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/writes",
            json={"path": "AI/note.md", "content": "# Note"},
            headers=_headers(writer),
        )
        assert created.status_code == 200
        proposal_id = created.json()["id"]

        denied = client.post(
            f"/api/v1/writes/{proposal_id}/approve", headers=_headers(reader)
        )
        assert denied.status_code == 403
        assert denied.json() == {
            "detail": "path 'AI/note.md' not writable by this token"
        }
        listing = client.get("/api/v1/writes", headers=_headers(writer)).json()
        proposal = next(
            item for item in listing["proposals"] if item["id"] == proposal_id
        )
        assert proposal["status"] == "pending"
        assert proposal["failure_reason"] is None

        approved = client.post(
            f"/api/v1/writes/{proposal_id}/approve", headers=_headers(writer)
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "applied"
    assert (tmp_path / "AI" / "note.md").is_file()


def test_token_policy_rejects_writable_target_when_missing_parent_is_denied(
    tmp_path: Path,
) -> None:
    """A token's denied missing parent cannot be bypassed by a writable target rule."""
    settings = _settings(
        tmp_path,
        draft=(FolderRule(path=PurePosixPath("AI"), access=FolderAccess.AUTO_WRITE),),
    )
    app = create_app(settings)
    _, plaintext = app.state.token_service.create(
        "writer",
        rules=[
            FolderRule(path=PurePosixPath("AI/new"), access=FolderAccess.NONE),
            FolderRule(
                path=PurePosixPath("AI/new/deep.md"),
                access=FolderAccess.PROPOSE_WRITE,
            ),
        ],
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/writes",
            json={"path": "AI/new/deep.md", "content": "# denied"},
            headers=_headers(plaintext),
        )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "write denied: AI/new/deep.md is denied by folder rule"
    }
    assert not (tmp_path / "AI" / "new" / "deep.md").exists()


def test_token_policy_revalidates_pending_approval_after_parent_denied(
    tmp_path: Path,
) -> None:
    """Approval rejects a pending token proposal after its parent grant is removed."""
    settings = _settings(
        tmp_path,
        draft=(FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),),
    )
    engine = create_database(settings.database_url)
    with CatalogSession(bind=engine) as session:
        service = VaultMutationService.from_settings(session, settings)
        initial_policy = AccessPolicy(
            (
                FolderRule(path=PurePosixPath("AI/new"), access=FolderAccess.PROPOSE_WRITE),
                FolderRule(
                    path=PurePosixPath("AI/new/deep.md"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            )
        )
        proposal = service.request("AI/new/deep.md", "# pending", policy=initial_policy)

        changed_policy = AccessPolicy(
            (
                FolderRule(path=PurePosixPath("AI/new"), access=FolderAccess.NONE),
                FolderRule(
                    path=PurePosixPath("AI/new/deep.md"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            )
        )
        with pytest.raises(VaultWriteDenied):
            service.approve(proposal.id, policy=changed_policy)

        assert proposal.status == "failed"
        event = next(
            event
            for event in reversed(service.activity_service.history())
            if event.event_type == "vault.mutation.failed"
        )
        assert event.payload["affected_paths"] == [
            "AI/new",
            "AI/new/deep.md",
        ]
    engine.dispose()


def test_mcp_propose_write_follows_token_policy(tmp_path: Path) -> None:
    """MCP propose_write auto-applies per token policy, ignoring the draft."""
    settings = _settings(
        tmp_path,
        draft=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
        ),
    )
    (tmp_path / "AI" / "note.md").write_text(_MANAGED, encoding="utf-8")
    activity = ActivityService(settings.database_url)
    service = TokenService(settings.database_url)
    try:
        transport, _ = build_mcp_server(settings, activity, service)
        server = transport.state.mcp_server
        auto, _ = service.create(
            "auto",
            rules=[
                FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE)
            ],
        )
        reader, _ = service.create(
            "reader",
            rules=[FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ)],
        )

        token = hlm_mcp_token.set(auto)
        try:
            result = _call(
                server,
                "propose_write",
                {
                    "path": "AI/note.md",
                    "content": _MANAGED.replace("v1", "v2"),
                },
            )
            assert json.loads(result.content[0].text)["status"] == "applied"
        finally:
            hlm_mcp_token.reset(token)
        assert (tmp_path / "AI" / "note.md").read_text(encoding="utf-8") == (
            _MANAGED.replace("v1", "v2")
        )

        token = hlm_mcp_token.set(reader)
        try:
            with pytest.raises(ToolError) as exc_info:
                _call(
                    server,
                    "propose_write",
                    {
                        "path": "AI/note.md",
                        "content": _MANAGED.replace("v1", "v3"),
                    },
                )
            assert "path 'AI/note.md' not writable by this token" in str(
                exc_info.value
            )
        finally:
            hlm_mcp_token.reset(token)
    finally:
        activity.close()
        service.close()


def _call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(name, arguments))
