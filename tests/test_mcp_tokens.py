"""MCP token enforcement: middleware 401s and per-tool policy checks.

Task 3 replaces the legacy scope guards with per-token ``AccessPolicy``
checks on the MCP surface: read tools sweep their results to what the
token can read, write tools check the target path, and scan/feedback
require any write access. Denials surface as tool errors (raised on this
transport, not HTTP 403s); the middleware keeps answering 401 for
unauthenticated requests.
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
from harbor_ledger_memory.api.auth import hlm_internal_request, hlm_mcp_token
from harbor_ledger_memory.api.mcp_server import build_mcp_server
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import MemoryWriteProposal
from harbor_ledger_memory.config import (
    FolderAccess,
    FolderRule,
    McpSettings,
    Settings,
)
from harbor_ledger_memory.services.activity import ActivityService
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.services.tokens import TokenService

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
_AUTH_REQUIRED_BODY = {
    "detail": "authentication required",
    "token_required": True,
    "hint": (
        "No active API token matches this bearer token. Create one with "
        "`hlm token create` and set it as the MCP client's Authorization "
        "bearer."
    ),
}


def _settings(tmp_path: Path, rules: tuple[FolderRule, ...] | None = None) -> Settings:
    """Build settings with the given global folder rules (dirs created).

    ``vault_path=`` (not the ``HLM_VAULT_PATH`` env alias used by
    test_mcp.py): accepted identically at runtime via
    ``populate_by_name=True`` and keeps this file pyright-clean.
    """
    if rules is None:
        rules = (
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
            FolderRule(path=PurePosixPath("Secret"), access=FolderAccess.PROPOSE_WRITE),
        )
    for rule in rules:
        (tmp_path / rule.path.as_posix()).mkdir(parents=True, exist_ok=True)
    return Settings(
        vault_path=tmp_path,
        folder_rules=rules,
        database_url=f"sqlite:///{tmp_path / 'mcp_tokens.db'}",
        mcp=McpSettings(enabled=True),
    )


def _call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(name, arguments))


def test_mcp_http_always_locked(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    service = TokenService(f"sqlite:///{tmp_path / 'mcp_tokens.db'}")
    with TestClient(app) as client:
        # Always locked: no token -> 401 (no open mode).
        locked_resp = client.post("/mcp/", json=_INITIALIZE, headers=_MCP_HEADERS)
        assert locked_resp.status_code == 401
        assert locked_resp.json() == _AUTH_REQUIRED_BODY
        bad_resp = client.post(
            "/mcp/",
            json=_INITIALIZE,
            headers={**_MCP_HEADERS, "Authorization": "Bearer hlm_bad"},
        )
        assert bad_resp.status_code == 401
        assert bad_resp.json() == _AUTH_REQUIRED_BODY
        _, plaintext = service.create("agent")
        assert client.post(
            "/mcp/",
            json=_INITIALIZE,
            headers={**_MCP_HEADERS, "Authorization": f"Bearer {plaintext}"},
        ).status_code == 200


def test_ui_cookie_never_authenticates_mcp(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    origin = "http://127.0.0.1:8765"
    with TestClient(app, base_url=origin) as client:
        assert client.get("/").status_code == 200
        response = client.post("/mcp/", json=_INITIALIZE, headers=_MCP_HEADERS)
        assert response.status_code == 401
        assert response.json() == _AUTH_REQUIRED_BODY


def test_mcp_read_sweeps_hide_unreadable_paths(tmp_path: Path) -> None:
    """Query/status/neighbours results are swept to the token's readable paths."""
    settings = _settings(tmp_path)  # global: AI -> READ, Secret -> PROPOSE_WRITE
    (tmp_path / "AI" / "Knowledge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Secret").mkdir(parents=True, exist_ok=True)
    (tmp_path / "AI" / "Knowledge" / "visible.md").write_text(
        "---\ntype: knowledge\ntags: [memory]\nstatus: active\n---\n"
        "# Visible\nMemory systems and retrieval fundamentals.\n"
        "[[Secret/hidden]]\n",
        encoding="utf-8",
    )
    (tmp_path / "Secret" / "hidden.md").write_text(
        "---\ntype: knowledge\ntags: [memory]\nstatus: active\n---\n"
        "# Hidden\nMemory systems and retrieval private notes.\n",
        encoding="utf-8",
    )
    activity = ActivityService(settings.database_url)
    service = TokenService(settings.database_url)
    ScanService.from_settings(settings).full_scan()
    try:
        transport, _ = build_mcp_server(settings, activity, service)
        server = transport.state.mcp_server
        # reader can read the default (AI) but explicitly denies Secret.
        reader, _ = service.create(
            "reader",
            rules=[FolderRule(path=PurePosixPath("Secret"), access=FolderAccess.NONE)],
        )
        token = hlm_mcp_token.set(reader)
        try:
            # query: selected memories swept to readable paths.
            queried = _call(server, "query", {"text": "memory systems"})
            paths = {
                memory["path"]
                for memory in json.loads(queried.content[0].text)["selected_memories"]
            }
            assert "AI/Knowledge/visible.md" in paths
            assert "Secret/hidden.md" not in paths

            # status: write_policy lists this token's own rules.
            status_payload = json.loads(_call(server, "status", {}).content[0].text)
            assert [
                rule["path"] for rule in status_payload["write_policy"]["rules"]
            ] == ["Secret"]

            # neighbours: entries swept; an unreadable central path hides all
            # edges (mirrors the REST graph edge sweep).
            assert _neighbours(server, "AI/Knowledge/visible.md") == []
            assert _neighbours(server, "Secret/hidden.md") == []
        finally:
            hlm_mcp_token.reset(token)

        # admin reads everything: no sweep.
        admin, _ = service.create("admin", rules=(), admin=True)
        token = hlm_mcp_token.set(admin)
        try:
            queried = _call(server, "query", {"text": "memory systems"})
            paths = {
                memory["path"]
                for memory in json.loads(queried.content[0].text)["selected_memories"]
            }
            assert {"AI/Knowledge/visible.md", "Secret/hidden.md"} <= paths
            status_payload = json.loads(_call(server, "status", {}).content[0].text)
            assert [
                rule["path"] for rule in status_payload["write_policy"]["rules"]
            ] == []
            neighbours = _neighbours(server, "AI/Knowledge/visible.md")
            assert [entry["path"] for entry in neighbours] == ["Secret/hidden.md"]
        finally:
            hlm_mcp_token.reset(token)
    finally:
        activity.close()
        service.close()


def _neighbours(server: Any, path: str) -> list[dict[str, Any]]:
    return json.loads(_call(server, "neighbours", {"path": path}).content[0].text)


def test_mcp_propose_write_path_check(tmp_path: Path) -> None:
    """propose_write checks the token's own rules for the target path."""
    settings = _settings(
        tmp_path,
        rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
        ),
    )
    activity = ActivityService(settings.database_url)
    service = TokenService(settings.database_url)
    try:
        transport, _ = build_mcp_server(settings, activity, service)
        server = transport.state.mcp_server
        reader, _ = service.create(
            "reader",
            rules=[FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ)],
        )
        writer, _ = service.create(
            "writer",
            rules=[
                FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE)
            ],
        )
        admin, _ = service.create("admin", rules=(), admin=True)

        # reader: propose to AI (read-only for it) is a tool error with the
        # exact path message.
        token = hlm_mcp_token.set(reader)
        try:
            with pytest.raises(ToolError) as exc_info:
                _call(
                    server, "propose_write", {"path": "AI/x.md", "content": "# X"}
                )
            assert "path 'AI/x.md' not writable by this token" in str(
                exc_info.value
            )
        finally:
            hlm_mcp_token.reset(token)

        # writer: propose to AI succeeds, creating a pending proposal.
        token = hlm_mcp_token.set(writer)
        try:
            proposed = _call(
                server, "propose_write", {"path": "AI/x.md", "content": "# X"}
            )
            proposal = json.loads(proposed.content[0].text)
            assert proposal["status"] == "pending"
        finally:
            hlm_mcp_token.reset(token)

        # admin: management-only — no folder rules means read-only, so
        # propose_write is a tool error like any other read-only token.
        token = hlm_mcp_token.set(admin)
        try:
            with pytest.raises(ToolError) as exc_info:
                _call(
                    server, "propose_write", {"path": "AI/y.md", "content": "# Y"}
                )
            assert "path 'AI/y.md' not writable by this token" in str(
                exc_info.value
            )
        finally:
            hlm_mcp_token.reset(token)
    finally:
        activity.close()
        service.close()


def test_mcp_propose_folder_denies_unwritable_missing_parent_before_persisting(
    tmp_path: Path,
) -> None:
    """A writable mkdir leaf cannot bypass a denied missing parent."""
    (tmp_path / "AI").mkdir()
    settings = _settings(
        tmp_path,
        rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.NONE),
            FolderRule(
                path=PurePosixPath("AI/allowed/new"),
                access=FolderAccess.PROPOSE_WRITE,
            ),
        ),
    )
    (tmp_path / "AI" / "allowed" / "new").rmdir()
    (tmp_path / "AI" / "allowed").rmdir()
    activity = ActivityService(settings.database_url)
    service = TokenService(settings.database_url)
    try:
        transport, _ = build_mcp_server(settings, activity, service)
        server = transport.state.mcp_server
        writer, _ = service.create(
            "writer",
            rules=[
                FolderRule(path=PurePosixPath("AI"), access=FolderAccess.NONE),
                FolderRule(
                    path=PurePosixPath("AI/allowed/new"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ],
        )
        token = hlm_mcp_token.set(writer)
        try:
            with pytest.raises(ToolError):
                _call(server, "propose_folder", {"path": "AI/allowed/new"})
        finally:
            hlm_mcp_token.reset(token)
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            assert session.query(MemoryWriteProposal).count() == 0
        finally:
            session.close()
            engine.dispose()
    finally:
        activity.close()
        service.close()


def test_mcp_approve_reject_check_proposal_path(tmp_path: Path) -> None:
    """approve/reject check the token's own rules for the proposal's path."""
    settings = _settings(
        tmp_path,
        rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
        ),
    )
    activity = ActivityService(settings.database_url)
    service = TokenService(settings.database_url)
    try:
        transport, _ = build_mcp_server(settings, activity, service)
        server = transport.state.mcp_server
        reader, _ = service.create(
            "reader",
            rules=[FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ)],
        )
        writer, _ = service.create(
            "writer",
            rules=[
                FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE)
            ],
            approve_own_proposals=True,
        )
        token = hlm_mcp_token.set(writer)
        try:
            first = json.loads(
                _call(
                    server, "propose_write", {"path": "AI/x.md", "content": "# X"}
                ).content[0]
                .text
            )
            second = json.loads(
                _call(
                    server, "propose_write", {"path": "AI/z.md", "content": "# Z"}
                ).content[0]
                .text
            )
        finally:
            hlm_mcp_token.reset(token)

        # reader cannot resolve proposals for paths it cannot write.
        token = hlm_mcp_token.set(reader)
        try:
            with pytest.raises(ToolError) as exc_info:
                _call(server, "approve_proposal", {"proposal_id": first["id"]})
            assert "path 'AI/x.md' not writable by this token" in str(
                exc_info.value
            )
            with pytest.raises(ToolError) as exc_info:
                _call(server, "reject_proposal", {"proposal_id": second["id"]})
            assert "path 'AI/z.md' not writable by this token" in str(
                exc_info.value
            )
        finally:
            hlm_mcp_token.reset(token)

        # writer resolves them.
        token = hlm_mcp_token.set(writer)
        try:
            raw = _call(server, "approve_proposal", {"proposal_id": first["id"]})
            approved = json.loads(raw.content[0].text)
            assert approved["status"] == "applied"
            assert (tmp_path / "AI" / "x.md").is_file()
            raw = _call(server, "reject_proposal", {"proposal_id": second["id"]})
            rejected = json.loads(raw.content[0].text)
            assert rejected["status"] == "rejected"
            assert not (tmp_path / "AI" / "z.md").exists()
        finally:
            hlm_mcp_token.reset(token)
    finally:
        activity.close()
        service.close()


def test_mcp_approval_requires_opt_in_and_creator_match(tmp_path: Path) -> None:
    """MCP approvals require the explicit owner-approval permission."""
    settings = _settings(
        tmp_path,
        rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
        ),
    )
    activity = ActivityService(settings.database_url)
    service = TokenService(settings.database_url)
    try:
        transport, _ = build_mcp_server(settings, activity, service)
        server = transport.state.mcp_server
        rules = [
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE)
        ]
        creator, _ = service.create(
            "creator", rules=rules, approve_own_proposals=True
        )
        other, _ = service.create("other", rules=rules, approve_own_proposals=True)
        plain, _ = service.create("plain", rules=rules)

        token = hlm_mcp_token.set(creator)
        try:
            proposal = json.loads(
                _call(
                    server,
                    "propose_write",
                    {"path": "AI/owned.md", "content": "# Owned"},
                ).content[0].text
            )
            assert proposal["creator_token_id"] == creator.id
        finally:
            hlm_mcp_token.reset(token)

        for caller, message in (
            (plain, "token lacks approve-own-proposals permission"),
            (other, "proposal was created by a different token"),
        ):
            token = hlm_mcp_token.set(caller)
            try:
                with pytest.raises(ToolError) as exc_info:
                    _call(server, "approve_proposal", {"proposal_id": proposal["id"]})
                assert message in str(exc_info.value)
            finally:
                hlm_mcp_token.reset(token)

        token = hlm_mcp_token.set(creator)
        try:
            approved = json.loads(
                _call(server, "approve_proposal", {"proposal_id": proposal["id"]})
                .content[0]
                .text
            )
            assert approved["status"] == "applied"
        finally:
            hlm_mcp_token.reset(token)
    finally:
        activity.close()
        service.close()


def test_mcp_scan_and_feedback_require_write_access(tmp_path: Path) -> None:
    """scan and feedback need at least one writable folder rule."""
    settings = _settings(
        tmp_path,
        rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
        ),
    )
    (tmp_path / "AI").mkdir(parents=True, exist_ok=True)
    (tmp_path / "AI" / "target.md").write_text(
        "# Target\n\nfeedback target\n", encoding="utf-8"
    )
    activity = ActivityService(settings.database_url)
    service = TokenService(settings.database_url)
    ScanService.from_settings(settings).full_scan()
    try:
        transport, _ = build_mcp_server(settings, activity, service)
        server = transport.state.mcp_server
        read_only, _ = service.create("read-only", rules=())

        # No write access: both mutating tools are tool errors.
        token = hlm_mcp_token.set(read_only)
        try:
            with pytest.raises(ToolError) as exc_info:
                _call(server, "scan", {})
            assert "token has no write access" in str(exc_info.value)
            with pytest.raises(ToolError) as exc_info:
                _call(
                    server,
                    "feedback",
                    {"trace_id": "missing-trace", "relevant_paths": ["AI/target.md"]},
                )
            assert "token has no write access" in str(exc_info.value)
        finally:
            hlm_mcp_token.reset(token)

        # With write access: both proceed to their normal behavior.
        writer, _ = service.create(
            "writer",
            rules=[
                FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE)
            ],
        )
        token = hlm_mcp_token.set(writer)
        try:
            scanned = json.loads(_call(server, "scan", {}).content[0].text)
            assert scanned["files_indexed"] >= 1
            queried_raw = _call(server, "query", {"text": "feedback target"})
            queried = json.loads(queried_raw.content[0].text)
            selected_path = queried["selected_memories"][0]["path"]
            applied = _call(
                server,
                "feedback",
                {
                    "trace_id": queried["trace_id"],
                    "relevant_paths": [selected_path],
                    "irrelevant_paths": [],
                },
            )
            assert json.loads(applied.content[0].text) == {
                "applied": True,
                "adjustments_count": 1,
            }
        finally:
            hlm_mcp_token.reset(token)
    finally:
        activity.close()
        service.close()


def test_mcp_internal_bypass_and_unauthenticated(tmp_path: Path) -> None:
    """Internal callers retain access except proposal approval needs a token."""
    settings = _settings(
        tmp_path,
        rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
        ),
    )
    activity = ActivityService(settings.database_url)
    service = TokenService(settings.database_url)
    try:
        transport, _ = build_mcp_server(settings, activity, service)
        server = transport.state.mcp_server
        service.create("someone", rules=())

        # No token record and no internal flag: tool error (the middleware
        # already 401s over HTTP; this guards direct in-process drives).
        with pytest.raises(ToolError) as exc_info:
            _call(server, "query", {"text": "anything"})
        assert "authentication required" in str(exc_info.value)

        # The internal bypass can create a proposal without a token, but it
        # cannot approve one without an opted-in creator token.
        token = hlm_internal_request.set(True)
        try:
            proposed = _call(
                server, "propose_write", {"path": "AI/int.md", "content": "# I"}
            )
            proposal = json.loads(proposed.content[0].text)
            with pytest.raises(ToolError) as exc_info:
                _call(server, "approve_proposal", {"proposal_id": proposal["id"]})
            assert "approve-own-proposals" in str(exc_info.value)
            assert not (tmp_path / "AI" / "int.md").exists()
        finally:
            hlm_internal_request.reset(token)
    finally:
        activity.close()
        service.close()
