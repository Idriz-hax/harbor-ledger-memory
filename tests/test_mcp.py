"""Focused MCP tool contract coverage."""

import asyncio
import json
from pathlib import Path, PurePosixPath
from queue import Empty

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.api.auth import hlm_internal_request, hlm_mcp_token
from harbor_ledger_memory.api.mcp_server import build_mcp_server
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import ActivationVisit, ContextSelection
from harbor_ledger_memory.config import (
    FolderAccess,
    FolderRule,
    McpSettings,
    Settings,
)
from harbor_ledger_memory.services.activity import ActivityService
from harbor_ledger_memory.services.live_traversal import LiveTraversalPublisher
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.services.tokens import TokenService


def test_mcp_exposes_status_and_write_lifecycle_tools(tmp_path: Path) -> None:
    settings = Settings(
        vault_path=tmp_path,
        folder_rules=(
            FolderRule(path=PurePosixPath("Public"), access=FolderAccess.PROPOSE_WRITE),
        ),
        database_url=f"sqlite:///{tmp_path / 'mcp.db'}",
    )
    activity = ActivityService(settings.database_url)
    token_service = TokenService(settings.database_url)
    caller, _ = token_service.create(
        "lifecycle",
        rules=[FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE)],
        approve_own_proposals=True,
    )
    token = hlm_mcp_token.set(caller)
    try:
        transport, manager = build_mcp_server(
            settings, activity, token_service, live_traversal=LiveTraversalPublisher()
        )
        server = transport.state.mcp_server
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert {
            "propose_write",
            "propose_folder",
            "approve_proposal",
            "reject_proposal",
        } <= names
        proposed = asyncio.run(
            server.call_tool(
                "propose_write", {"path": "Public/new.md", "content": "# New"}
            )
        )
        proposal = json.loads(proposed.content[0].text)
        assert not (tmp_path / "new.md").exists()
        assert not (tmp_path / "Public" / "new.md").exists()
        approved = asyncio.run(
            server.call_tool("approve_proposal", {"proposal_id": proposal["id"]})
        )
        assert json.loads(approved.content[0].text)["status"] == "applied"
        assert (tmp_path / "Public" / "new.md").exists()
        folder = asyncio.run(
            server.call_tool("propose_folder", {"path": "Public/Inbox"})
        )
        folder_proposal = json.loads(folder.content[0].text)
        assert folder_proposal["operation"] == "mkdir"
        folder_approved = asyncio.run(
            server.call_tool("approve_proposal", {"proposal_id": folder_proposal["id"]})
        )
        assert json.loads(folder_approved.content[0].text)["created_paths"] == [
            "Public/Inbox"
        ]
        assert (tmp_path / "Public" / "Inbox").is_dir()
        rejected = asyncio.run(
            server.call_tool(
                "propose_write", {"path": "Public/other.md", "content": "# Other"}
            )
        )
        rejected_proposal = json.loads(rejected.content[0].text)
        rejected_result = asyncio.run(
            server.call_tool(
                "reject_proposal", {"proposal_id": rejected_proposal["id"]}
            )
        )
        assert json.loads(rejected_result.content[0].text)["status"] == "rejected"
        assert not (tmp_path / "Public" / "other.md").exists()
        # The caller token has full access, so a path outside the draft rules
        # still queues a pending proposal: draft rules never deny.
        private = asyncio.run(
            server.call_tool(
                "propose_write", {"path": "Private/no.md", "content": "# No"}
            )
        )
        assert json.loads(private.content[0].text)["status"] == "pending"
        assert manager is not None
    finally:
        hlm_mcp_token.reset(token)
        token_service.close()
        activity.close()


def test_mcp_status_policy_shape(tmp_path: Path) -> None:
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'status.db'}",
    )
    activity = ActivityService(settings.database_url)
    internal = hlm_internal_request.set(True)
    try:
        transport, manager = build_mcp_server(
            settings, activity, live_traversal=LiveTraversalPublisher()
        )
        server = transport.state.mcp_server
        status = asyncio.run(server.call_tool("status", {}))
        assert json.loads(status.content[0].text)["write_policy"] == {
            "default_access": "read",
            "rules": [{"path": ".", "access": "auto-write"}],
        }
        assert manager is not None
    finally:
        hlm_internal_request.reset(internal)
        activity.close()


def test_mcp_scan_hides_unreadable_paths_for_limited_token(tmp_path: Path) -> None:
    (tmp_path / "AI" / "Public").mkdir(parents=True)
    (tmp_path / "AI" / "Private").mkdir()
    (tmp_path / "AI" / "Public" / "visible.md").write_text("# visible")
    (tmp_path / "AI" / "Private" / "hidden.md").write_text("# hidden")
    rules = [
        FolderRule(path=PurePosixPath("AI/Public"), access=FolderAccess.PROPOSE_WRITE),
        FolderRule(path=PurePosixPath("AI/Private"), access=FolderAccess.NONE),
    ]
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        index_root="AI",
        database_url=f"sqlite:///{tmp_path / 'mcp.db'}",
    )
    activity = ActivityService(settings.database_url)
    token_service = TokenService(settings.database_url)
    caller, _ = token_service.create("limited", rules=rules)
    token = hlm_mcp_token.set(caller)
    try:
        transport, manager = build_mcp_server(
            settings, activity, token_service, live_traversal=LiveTraversalPublisher()
        )
        result = asyncio.run(transport.state.mcp_server.call_tool("scan", {}))
        payload = json.loads(result.content[0].text)
        assert payload["rebuild"]["indexed_paths"] == ["AI/Public/visible.md"]
        assert manager is not None
    finally:
        hlm_mcp_token.reset(token)
        token_service.close()
        activity.close()


def test_mcp_query_and_write_publish_to_supplied_live_traversal(tmp_path: Path) -> None:
    (tmp_path / "source.md").write_text("# Source\n\n[[target]]", encoding="utf-8")
    (tmp_path / "target.md").write_text(
        "# Target\n\ntelemetry target phrase", encoding="utf-8"
    )
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'telemetry.db'}",
    )
    activity = ActivityService(settings.database_url)
    token_service = TokenService(settings.database_url)
    caller, _ = token_service.create("telemetry", approve_own_proposals=True)
    publisher = LiveTraversalPublisher()
    subscription = publisher.subscribe()
    internal = hlm_internal_request.set(True)
    token = hlm_mcp_token.set(caller)
    try:
        ScanService.from_settings(settings).full_scan()
        transport, _ = build_mcp_server(
            settings, activity, token_service, live_traversal=publisher
        )
        server = transport.state.mcp_server

        queried = asyncio.run(
            server.call_tool("query", {"text": "telemetry target phrase"})
        )
        assert json.loads(queried.content[0].text)["trace_id"]
        query_event = subscription.get_nowait()
        assert query_event.mode == "read"
        assert query_event.node_path == "target.md"

        proposed = asyncio.run(
            server.call_tool(
                "propose_write", {"path": "created.md", "content": "# Created"}
            )
        )
        proposal = json.loads(proposed.content[0].text)
        asyncio.run(
            server.call_tool("approve_proposal", {"proposal_id": proposal["id"]})
        )
        write_event = subscription.get(timeout=1)
        while write_event.mode != "write":
            write_event = subscription.get(timeout=1)
        assert write_event.mode == "write"
        assert write_event.node_path == "created.md"
    finally:
        subscription.close()
        hlm_mcp_token.reset(token)
        hlm_internal_request.reset(internal)
        token_service.close()
        activity.close()


def test_restricted_mcp_query_filters_outputs_and_side_effects(tmp_path: Path) -> None:
    (tmp_path / "Public").mkdir()
    (tmp_path / "Private").mkdir()
    (tmp_path / "Public" / "readable.md").write_text(
        "---\nsummary: See [[Private/secret.md]]\n---\n\n# Readable\n\npublic phrase",
        encoding="utf-8",
    )
    (tmp_path / "Private" / "secret.md").write_text(
        "# Secret\n\nunreadable secret phrase", encoding="utf-8"
    )
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'restricted-query.db'}",
    )
    activity = ActivityService(settings.database_url)
    token_service = TokenService(settings.database_url)
    caller, _ = token_service.create(
        "restricted-query",
        rules=[FolderRule(path=PurePosixPath("Private"), access=FolderAccess.NONE)],
    )
    publisher = LiveTraversalPublisher()
    subscription = publisher.subscribe()
    token = hlm_mcp_token.set(caller)
    try:
        ScanService.from_settings(settings).full_scan()
        transport, _ = build_mcp_server(
            settings, activity, token_service, live_traversal=publisher
        )
        result = json.loads(
            asyncio.run(
                transport.state.mcp_server.call_tool(
                    "query", {"text": "unreadable secret phrase"}
                )
            ).content[0].text
        )

        serialized = json.dumps(result)
        assert "Private/secret.md" not in serialized
        assert result["selected_memories"] == []
        assert result["excluded_nodes"] == []
        assert result["short_term_evidence"]["hit_paths"] == []
        with pytest.raises(Empty):
            subscription.get_nowait()

        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            assert session.scalars(select(ContextSelection)).all() == []
            assert session.scalars(select(ActivationVisit)).all() == []
        finally:
            session.close()
            engine.dispose()
    finally:
        subscription.close()
        hlm_mcp_token.reset(token)
        token_service.close()
        activity.close()


def test_restricted_mcp_query_redacts_unreadable_summary_link(tmp_path: Path) -> None:
    (tmp_path / "Public").mkdir()
    (tmp_path / "Private").mkdir()
    (tmp_path / "Public" / "readable.md").write_text(
        "---\nsummary: See [[Private/secret.md]]\n---\n\n# Readable\n\npublic phrase",
        encoding="utf-8",
    )
    (tmp_path / "Private" / "secret.md").write_text(
        "# Secret\n\nsecret phrase", encoding="utf-8"
    )
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'restricted-summary.db'}",
    )
    activity = ActivityService(settings.database_url)
    token_service = TokenService(settings.database_url)
    caller, _ = token_service.create(
        "restricted-summary",
        rules=[FolderRule(path=PurePosixPath("Private"), access=FolderAccess.NONE)],
    )
    token = hlm_mcp_token.set(caller)
    try:
        ScanService.from_settings(settings).full_scan()
        transport, _ = build_mcp_server(
            settings,
            activity,
            token_service,
            live_traversal=LiveTraversalPublisher(),
        )
        result = json.loads(
            asyncio.run(
                transport.state.mcp_server.call_tool(
                    "query", {"text": "public phrase"}
                )
            ).content[0].text
        )
        summary = result["selected_memories"][0]["summary"]
        assert "[[REDACTED]]" in summary
        assert "Private/secret.md" not in summary
    finally:
        hlm_mcp_token.reset(token)
        token_service.close()
        activity.close()


def test_restricted_mcp_feedback_rejects_unreadable_trace_path(tmp_path: Path) -> None:
    (tmp_path / "Private").mkdir()
    (tmp_path / "Private" / "secret.md").write_text(
        "# Secret\n\nunreadable feedback phrase", encoding="utf-8"
    )
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'restricted-feedback.db'}",
    )
    activity = ActivityService(settings.database_url)
    token_service = TokenService(settings.database_url)
    unrestricted, _ = token_service.create(
        "unrestricted-feedback",
        rules=[FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE)],
    )
    restricted, _ = token_service.create(
        "restricted-feedback",
        rules=[
            FolderRule(path=PurePosixPath("Public"), access=FolderAccess.PROPOSE_WRITE),
            FolderRule(path=PurePosixPath("Private"), access=FolderAccess.NONE),
        ],
    )
    try:
        ScanService.from_settings(settings).full_scan()
        transport, _ = build_mcp_server(
            settings, activity, token_service, live_traversal=LiveTraversalPublisher()
        )
        server = transport.state.mcp_server
        unrestricted_token = hlm_mcp_token.set(unrestricted)
        try:
            queried = json.loads(
                asyncio.run(
                    server.call_tool(
                        "query", {"text": "unreadable feedback phrase"}
                    )
                ).content[0].text
            )
        finally:
            hlm_mcp_token.reset(unrestricted_token)

        token = hlm_mcp_token.set(restricted)
        try:
            with pytest.raises(ToolError):
                asyncio.run(
                    server.call_tool(
                        "feedback",
                        {
                            "trace_id": queried["trace_id"],
                            "relevant_paths": ["Private/secret.md"],
                        },
                    )
                )
        finally:
            hlm_mcp_token.reset(token)
    finally:
        token_service.close()
        activity.close()


def test_mcp_neighbours_rejects_out_of_scope_paths(tmp_path: Path) -> None:
    (tmp_path / "Public").mkdir()
    (tmp_path / "Public" / "inside.md").write_text("# Inside", encoding="utf-8")
    (tmp_path / "outside.md").write_text("# Outside", encoding="utf-8")
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        index_root="Public",
        database_url=f"sqlite:///{tmp_path / 'graph.db'}",
    )
    activity = ActivityService(settings.database_url)
    internal = hlm_internal_request.set(True)
    try:
        ScanService.from_settings(settings).full_scan()
        transport, _ = build_mcp_server(
            settings, activity, live_traversal=LiveTraversalPublisher()
        )
        server = transport.state.mcp_server
        with pytest.raises(ToolError):
            asyncio.run(server.call_tool("neighbours", {"path": "outside.md"}))
    finally:
        hlm_internal_request.reset(internal)
        activity.close()


def test_mcp_feedback_applies_to_query_trace_and_rejects_invalid_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "Public").mkdir()
    (tmp_path / "Public" / "selected.md").write_text(
        "# Selected\n\nfeedback target", encoding="utf-8"
    )
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'feedback.db'}",
    )
    activity = ActivityService(settings.database_url)
    internal = hlm_internal_request.set(True)
    try:
        ScanService.from_settings(settings).full_scan()
        transport, _ = build_mcp_server(
            settings, activity, live_traversal=LiveTraversalPublisher()
        )
        server = transport.state.mcp_server
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert "feedback" in names

        queried = asyncio.run(server.call_tool("query", {"text": "feedback target"}))
        result = json.loads(queried.content[0].text)
        selected_path = result["selected_memories"][0]["path"]
        applied = asyncio.run(
            server.call_tool(
                "feedback",
                {
                    "trace_id": result["trace_id"],
                    "relevant_paths": [selected_path],
                    "irrelevant_paths": [],
                },
            )
        )
        assert json.loads(applied.content[0].text) == {
            "applied": True,
            "adjustments_count": 0,
            "recorded_count": 1,
        }
        with pytest.raises(ToolError):
            asyncio.run(
                server.call_tool(
                    "feedback",
                    {
                        "trace_id": "missing-trace",
                        "relevant_paths": [selected_path],
                    },
                )
            )
        with pytest.raises(ToolError):
            asyncio.run(
                server.call_tool(
                    "feedback",
                    {
                        "trace_id": result["trace_id"],
                        "relevant_paths": ["not-selected.md"],
                    },
                )
            )
    finally:
        hlm_internal_request.reset(internal)
        activity.close()


def test_mounted_mcp_http_lifecycle_uses_app_lifespan(tmp_path: Path) -> None:
    (tmp_path / "Public").mkdir()
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        folder_rules=(
            FolderRule(path=PurePosixPath("Public"), access=FolderAccess.PROPOSE_WRITE),
        ),
        database_url=f"sqlite:///{tmp_path / 'http.db'}",
        mcp=McpSettings(enabled=True),
    )

    application = create_app(settings)
    _, plaintext = application.state.token_service.create("lifecycle", admin=True)
    # The installed MCP client (mcp 2.x) with httpx2.ASGITransport cannot
    # complete initialize in-process: ClientSession.initialize() raises
    # MCPError("Server returned an error response"). Keep this as a genuine
    # mounted transport check rather than claiming a false E2E lifecycle test.
    from fastapi.testclient import TestClient

    with TestClient(application, base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            "/mcp/",
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "host": "127.0.0.1:8000",
                "authorization": f"Bearer {plaintext}",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200
        assert response.headers.get("mcp-session-id")


def test_mounted_mcp_uses_app_live_traversal_for_scan(tmp_path: Path) -> None:
    (tmp_path / "scan.md").write_text("# Scan target", encoding="utf-8")
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'mounted-live.db'}",
        mcp=McpSettings(enabled=True),
    )
    application = create_app(settings)
    publisher = application.state.live_traversal
    subscription = publisher.subscribe()
    internal = hlm_internal_request.set(True)
    try:
        with TestClient(application):
            # The MCP transport attached at /mcp closes over the same publisher
            # held by the application, rather than a process-global fallback.
            mounted = next(
                route.app for route in application.routes if route.path == "/mcp"
            )
            server = mounted.state.mcp_server
            while subscription.get(timeout=1).node_path != "scan.md":
                pass

            asyncio.run(server.call_tool("scan", {}))
            event = subscription.get(timeout=1)
            while event.node_path != "scan.md":
                event = subscription.get(timeout=1)
            assert event.mode == "read"
    finally:
        subscription.close()
        hlm_internal_request.reset(internal)
