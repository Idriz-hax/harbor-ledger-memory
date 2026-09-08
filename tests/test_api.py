"""Temporary-catalog coverage for the localhost API scaffold."""

import asyncio
import json
import queue
import threading
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import authed_client
from harbor_ledger_memory.api import app as app_module
from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import MemoryWriteProposal, Note
from harbor_ledger_memory.config import (
    ApiSettings,
    FolderAccess,
    FolderRule,
)
from harbor_ledger_memory.config import Settings as BaseSettings
from harbor_ledger_memory.graph.builder import GraphBuilder


def Settings(**kwargs: Any) -> BaseSettings:
    """Keep legacy API tests explicitly on the enabled external API."""
    kwargs.setdefault("api", ApiSettings(enabled=True))
    return BaseSettings(**kwargs)


class FakeScanner:
    boundary = object()
    calls = 0

    def full_scan(self) -> None:
        self.calls += 1


class FakeWatcher:
    started = False
    stopped = False

    def __init__(self, boundary: object, scanner: FakeScanner) -> None:
        pass

    def start(self) -> None:
        type(self).started = True

    def stop(self) -> None:
        type(self).stopped = True


def test_api_exposes_write_capable_health_and_status(tmp_path: Path) -> None:
    (tmp_path / "AI").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        folder_rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
        ),
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    client = authed_client(settings)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "read_only": False,
        "mcp": {"enabled": False, "active_tokens": 1},
    }

    status = client.get("/api/v1/status")
    assert status.status_code == 200
    assert status.json()["status"] == "ok"
    assert status.json()["read_only"] is False
    assert status.json()["vault_scope"] == "."
    assert status.json()["effective_read_scope"] == "."
    assert status.json()["indexed_notes"] == 0

    scan_resp = client.post("/api/v1/scan")
    assert scan_resp.status_code == 200
    scan_data = scan_resp.json()
    assert scan_data["files_indexed"] == 0
    assert scan_data["broken_links"] == 0
    assert scan_data["ambiguous_links"] == 0
    assert scan_data["diagnostics_count"] == 0


def test_health_and_status_report_read_only_when_no_rule_grants_writes(
    tmp_path: Path,
) -> None:
    """read_only is derived from the caller's rules: no write rule means read-only."""
    (tmp_path / "AI").mkdir()

    def _read_only_client(settings: Settings) -> TestClient:
        app = create_app(settings)
        token = app.state.token_service.create("read-only", rules=()).plaintext
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    # No folder rules at all: every path is read-only by default.
    bare = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'bare.db'}",
    )
    with _read_only_client(bare) as client:
        assert client.get("/health").json() == {
            "status": "ok",
            "read_only": True,
            "mcp": {"enabled": False, "active_tokens": 1},
        }
        assert client.get("/api/v1/status").json()["read_only"] is True

    # Read/deny rules only: still no write capability anywhere.
    read_only = Settings(
        vault_path=tmp_path,
        index_root="AI",
        folder_rules=(FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),),
        database_url=f"sqlite:///{tmp_path / 'readonly.db'}",
    )
    with _read_only_client(read_only) as client:
        assert client.get("/health").json() == {
            "status": "ok",
            "read_only": True,
            "mcp": {"enabled": False, "active_tokens": 1},
        }
        assert client.get("/api/v1/status").json()["read_only"] is True


def test_status_exposes_the_calling_tokens_own_write_policy(tmp_path: Path) -> None:
    (tmp_path / "Public").mkdir()
    (tmp_path / "Private").mkdir()
    rules = (
        FolderRule(path=PurePosixPath("Public"), access=FolderAccess.PROPOSE_WRITE),
        FolderRule(path=PurePosixPath("Private"), access=FolderAccess.NONE),
    )
    settings = Settings(
        vault_path=tmp_path,
        folder_rules=rules,
        database_url=f"sqlite:///{tmp_path / 'policy.db'}",
    )
    app = create_app(settings)
    token = app.state.token_service.create("mixed", rules=list(rules)).plaintext
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        payload = client.get("/api/v1/status").json()
    assert payload["read_only"] is False
    assert payload["write_policy"] == {
        "default_access": "read",
        "rules": [
            {"path": "Public", "access": "propose-write"},
            {"path": "Private", "access": "none"},
        ],
    }


def test_settings_update_does_not_split_live_rest_and_mcp_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "Public").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        folder_rules=(
            FolderRule(path=PurePosixPath("Public"), access=FolderAccess.PROPOSE_WRITE),
        ),
        database_url=f"sqlite:///{tmp_path / 'drift.db'}",
    )
    with authed_client(settings) as client:
        saved = client.put("/api/v1/settings", json={"folder_rules": []})
        assert saved.status_code == 200
        # Restart is required: live REST and mounted MCP continue using the
        # same startup policy rather than diverging after a hot rebind.
        assert client.get("/api/v1/status").json()["read_only"] is False
        assert (
            client.post(
                "/api/v1/writes", json={"path": "Public/a.md", "content": "a"}
            ).status_code
            == 200
        )

def test_app_lifespan_scans_then_starts_and_stops_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanner = FakeScanner()
    FakeWatcher.started = False
    FakeWatcher.stopped = False
    settings = Settings(vault_path=tmp_path)
    monkeypatch.setattr(app_module.ScanService, "from_settings", lambda _: scanner)
    monkeypatch.setattr(app_module, "VaultWatchService", FakeWatcher)

    with authed_client(settings):
        assert scanner.calls == 1
        assert FakeWatcher.started is True
    assert FakeWatcher.stopped is True


def test_app_lifespan_scan_failure_aborts_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingScanner:
        boundary = object()

        def full_scan(self) -> None:
            raise RuntimeError("scan failed")

    FakeWatcher.started = False
    scanner = FailingScanner()
    settings = Settings(vault_path=tmp_path)
    monkeypatch.setattr(app_module.ScanService, "from_settings", lambda _: scanner)
    monkeypatch.setattr(app_module, "VaultWatchService", FakeWatcher)

    with pytest.raises(RuntimeError, match="scan failed"):
        with authed_client(settings):
            pass
    assert FakeWatcher.started is False


def test_serve_base_layout(tmp_path: Path) -> None:
    (tmp_path / "AI").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    client = authed_client(settings)

    response = client.get("/")
    assert response.status_code == 200
    assert "Harbor Ledger Memory" in response.text
    assert '<div id="root"></div>' in response.text
    assert 'type="module"' in response.text
    assert 'src="/assets/index-' in response.text
    assert 'href="/assets/index-' in response.text


def test_query_screen_renders(tmp_path: Path) -> None:
    (tmp_path / "AI").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    client = authed_client(settings)

    response = client.get("/query")
    assert response.status_code == 200
    assert 'action="/query"' in response.text
    assert 'name="q"' in response.text


def test_query_submission_renders_results(tmp_path: Path) -> None:
    (tmp_path / "AI").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    client = authed_client(settings)

    response = client.post("/query", data={"q": "memory", "project": ""})

    assert response.status_code == 200
    assert "Results" in response.text


def test_status_screen_renders(tmp_path: Path) -> None:
    (tmp_path / "AI").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    client = authed_client(settings)

    response = client.get("/status")
    assert response.status_code == 200
    assert "indexed_notes" in response.text
    assert "scan_runs" in response.text
    assert "broken_links" in response.text
    assert "ambiguous_links" in response.text
    assert "Short-Term Cache" in response.text


def test_settings_page_and_api_persist_application_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "AI").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    client = authed_client(settings)

    page = client.get("/settings")
    assert page.status_code == 200
    assert "Index root" in page.text

    response = client.put(
        "/api/v1/settings",
        json={
            "index_root": "AI",
            "folder_rules": [{"path": "AI/Private", "access": "deny"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["saved"] is True
    assert response.json()["restart_required"] is True
    assert (tmp_path / "home/.config/harbor-ledger-memory/config.toml").exists()


def test_settings_update_response_reflects_saved_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saving folder rules must return (and keep returning) the saved rules.

    Regression: the save response previously echoed the startup snapshot, so
    the UI reset every folder to the default (read-only) after saving.
    """
    (tmp_path / "AI").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    client = authed_client(settings)
    response = client.put(
        "/api/v1/settings",
        json={"folder_rules": [{"path": "AI", "access": "propose-write"}]},
    )
    assert response.status_code == 200
    saved = response.json()["settings"]["folder_rules"]
    assert any(r["path"] == "AI" and r["access"] == "propose-write" for r in saved)
    # A later read must agree with what was saved (no reset to read-only).
    reloaded = client.get("/api/v1/settings").json()["folder_rules"]
    assert any(r["path"] == "AI" and r["access"] == "propose-write" for r in reloaded)


def test_cache_status_endpoint(tmp_path: Path) -> None:
    (tmp_path / "AI").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    client = authed_client(settings)

    response = client.get("/api/v1/cache-status")
    assert response.status_code == 200
    data = response.json()
    assert "entry_count" in data
    assert "capacity" in data
    assert "ttl_days" in data
    assert "max_boost" in data


def test_update_status_endpoint(tmp_path: Path) -> None:
    """GET /api/v1/update-status returns current version and update availability."""
    (tmp_path / "AI").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    client = authed_client(settings)

    response = client.get("/api/v1/update-status")
    assert response.status_code == 200
    data = response.json()
    assert "current_version" in data
    assert "latest_version" in data
    assert "update_available" in data
    # current_version should be a valid version string
    assert isinstance(data["current_version"], str)
    assert len(data["current_version"]) > 0


def test_trace_replay(tmp_path: Path) -> None:
    """Execute a query, then fetch the trace via the replay endpoint."""
    (tmp_path / "AI").mkdir()
    db_path = str(tmp_path / "catalog.db")
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{db_path}",
    )

    # Seed the catalog with notes so query returns results
    engine = create_database(settings.database_url)
    session = CatalogSession(bind=engine)
    try:
        session.add(
            Note(
                path="AI/Knowledge/memory.md",
                title="Memory",
                content=(
                    "Memory systems are important. See [[AI/Knowledge/context.md]]."
                ),
                summary="About memory systems",
                frontmatter_json='{"tags":["memory"]}',
                content_hash="aaa",
            )
        )
        session.add(
            Note(
                path="AI/Knowledge/context.md",
                title="Context",
                content="Context windows matter for LLMs.",
                summary="About context windows",
                frontmatter_json='{"tags":["context"]}',
                content_hash="bbb",
            )
        )
        session.commit()
        GraphBuilder(session).build()
    finally:
        session.close()
        engine.dispose()

    client = authed_client(settings)

    # Execute a query via the API
    query_resp = client.post(
        "/api/v1/queries",
        json={"query": "memory systems"},
    )
    assert query_resp.status_code == 200
    query_data = query_resp.json()
    trace_id = query_data["trace_id"]
    assert trace_id is not None
    # Validate it's a proper UUID string
    UUID(trace_id)

    # Replay the trace
    replay_resp = client.get(f"/api/v1/traces/{trace_id}")
    assert replay_resp.status_code == 200
    replay_data = replay_resp.json()
    assert replay_data["trace_id"] == trace_id
    assert replay_data["query"] == "memory systems"
    assert "selected_paths" in replay_data
    assert isinstance(replay_data["selected_paths"], list)
    if replay_data["selected_paths"]:
        sel = replay_data["selected_paths"][0]
        assert "path" in sel
        assert "rank" in sel
        assert "retrieval_score" in sel
        assert "activation_score" in sel
    assert "activation_graph" in replay_data
    assert isinstance(replay_data["activation_graph"], list)
    if replay_data["activation_graph"]:
        visit = replay_data["activation_graph"][0]
        assert "path" in visit
        assert "activation_score" in visit
        assert "hop" in visit
    assert "created_at" in replay_data
    assert isinstance(replay_data["created_at"], str)

    # Non-existent trace returns 404
    not_found_resp = client.get("/api/v1/traces/00000000-0000-0000-0000-000000000000")
    assert not_found_resp.status_code == 404


def test_scan_endpoint_rescans_vault_and_returns_counts(tmp_path: Path) -> None:
    """POST /api/v1/scan rebuilds the catalog and returns concise counts."""
    ai_dir = tmp_path / "AI"
    ai_dir.mkdir()
    # Create a note with a broken link
    (ai_dir / "note1.md").write_text("# Note 1\n\nSee [[note2]]. Also [[nonexistent]].")
    (ai_dir / "note2.md").write_text("# Note 2\n\nContent.")
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    client = authed_client(settings)

    # Lifespan already did a scan on startup; trigger a rescan
    resp = client.post("/api/v1/scan")
    assert resp.status_code == 200
    data = resp.json()
    assert data["files_indexed"] == 2
    assert data["broken_links"] == 1
    assert data["ambiguous_links"] == 0
    assert "diagnostics_count" in data
    assert data["diagnostics_count"] >= 1

    # Status should reflect the re-scan
    status = client.get("/api/v1/status")
    assert status.json()["indexed_notes"] == 2


def test_scan_endpoint_records_activity(tmp_path: Path) -> None:
    """POST /api/v1/scan emits an activity event visible via /api/v1/activity."""
    ai_dir = tmp_path / "AI"
    ai_dir.mkdir()
    (ai_dir / "x.md").write_text("# X")
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    client = authed_client(settings)

    client.post("/api/v1/scan")

    activity = client.get("/api/v1/activity")
    assert activity.status_code == 200
    events = activity.json()["events"]
    scan_events = [e for e in events if e["event_type"] == "scan"]
    assert len(scan_events) >= 1
    last_scan = scan_events[-1]
    assert "files_indexed" in last_scan["payload"]


# ---------------------------------------------------------------------------
# Write proposal API
# ---------------------------------------------------------------------------


def _write_settings(tmp_path: Path) -> Settings:
    """Settings with explicit write rules for the write API tests."""
    vault = tmp_path / "vault"
    (vault / "AI").mkdir(parents=True)
    return Settings(
        vault_path=vault,
        index_root="AI",
        folder_rules=(
            FolderRule(
                path=PurePosixPath("AI/proposed"), access=FolderAccess.PROPOSE_WRITE
            ),
            FolderRule(path=PurePosixPath("AI/denied"), access=FolderAccess.DENY),
            FolderRule(path=PurePosixPath("AI/readonly"), access=FolderAccess.READ),
        ),
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )


def _client_with_rules(
    settings: Settings,
    rules: list[FolderRule],
    admin: bool = True,
) -> TestClient:
    """Client whose token carries exactly ``rules``.

    ``authed_client`` always grants ``.`` AUTO_WRITE, which would let the
    token policy decide every write; these tests need narrower per-token
    grants to exercise policy-driven proposals and denials.
    """
    application = create_app(settings)
    _, plaintext = application.state.token_service.create(
        f"test-token-{uuid4().hex}", rules=rules, admin=admin
    )
    return TestClient(
        application, headers={"Authorization": f"Bearer {plaintext}"}
    )


def test_proposal_requires_approval_then_writes(tmp_path: Path) -> None:
    """POST /api/v1/writes queues a pending proposal; approve applies it."""
    settings = _write_settings(tmp_path)
    client = _client_with_rules(
        settings,
        [
            FolderRule(
                path=PurePosixPath("AI/proposed"), access=FolderAccess.PROPOSE_WRITE
            )
        ],
    )

    created = client.post(
        "/api/v1/writes",
        json={"path": "AI/proposed/new.md", "content": "# New\n"},
    )
    assert created.status_code == 200
    data = created.json()
    assert data["status"] == "pending"
    assert data["operation"] == "create"
    assert data["path"] == "AI/proposed/new.md"
    assert data["content"] == "# New\n"
    assert data["rule_access"] == "propose-write"
    assert data["requested_at"]
    assert data["resolved_at"] is None
    assert data["failure_reason"] is None
    proposal_id = data["id"]
    target = settings.vault_path / "AI" / "proposed" / "new.md"
    assert not target.exists()

    approved = client.post(f"/api/v1/writes/{proposal_id}/approve")
    assert approved.status_code == 200
    approved_data = approved.json()
    assert approved_data["status"] == "applied"
    assert approved_data["resolved_at"] is not None
    assert approved_data["content"] == "# New\n"
    assert target.read_text(encoding="utf-8") == "# New\n"

    # Approving a resolved proposal is a validation error.
    again = client.post(f"/api/v1/writes/{proposal_id}/approve")
    assert again.status_code == 400


def test_proposal_rejection_keeps_vault_unchanged(tmp_path: Path) -> None:
    """POST /api/v1/writes/{id}/reject resolves without touching the vault."""
    settings = _write_settings(tmp_path)
    client = authed_client(settings)

    created = client.post(
        "/api/v1/writes",
        json={"path": "AI/proposed/draft.md", "content": "draft content"},
    )
    assert created.status_code == 200
    proposal_id = created.json()["id"]

    rejected = client.post(f"/api/v1/writes/{proposal_id}/reject")
    assert rejected.status_code == 200
    data = rejected.json()
    assert data["status"] == "rejected"
    assert data["resolved_at"] is not None
    assert not (settings.vault_path / "AI" / "proposed" / "draft.md").exists()

    # The rejection audit event carries the deterministic graph refs.
    history = client.get("/api/v1/activity").json()["events"]
    rejected_events = [
        event
        for event in history
        if event["event_type"] == "vault.mutation.rejected"
        and event["payload"].get("proposal_id") == proposal_id
    ]
    assert len(rejected_events) == 1
    assert rejected_events[0]["payload"]["graph_refs"] == ["AI/proposed/draft.md"]

    # Rejecting a resolved proposal is a validation error.
    again = client.post(f"/api/v1/writes/{proposal_id}/reject")
    assert again.status_code == 400


def test_denied_and_read_only_writes_return_403(tmp_path: Path) -> None:
    """none, explicit read, and unruled (default read) token grants all 403.

    The token's own rules decide: the route rejects the write before the
    mutation service runs, so no denied-audit event is recorded here
    (service-level denial auditing is covered by test_vault_mutations).
    """
    settings = _write_settings(tmp_path)
    client = _client_with_rules(
        settings,
        [
            FolderRule(
                path=PurePosixPath("AI/proposed"), access=FolderAccess.PROPOSE_WRITE
            ),
            FolderRule(path=PurePosixPath("AI/denied"), access=FolderAccess.NONE),
            FolderRule(path=PurePosixPath("AI/readonly"), access=FolderAccess.READ),
        ],
    )

    denied = client.post(
        "/api/v1/writes",
        json={"path": "AI/denied/secret.md", "content": "s"},
    )
    assert denied.status_code == 403
    assert denied.json() == {
        "detail": "path 'AI/denied/secret.md' not writable by this token"
    }

    read_only = client.post(
        "/api/v1/writes",
        json={"path": "AI/readonly/note.md", "content": "n"},
    )
    assert read_only.status_code == 403
    assert read_only.json() == {
        "detail": "path 'AI/readonly/note.md' not writable by this token"
    }

    unruled = client.post(
        "/api/v1/writes",
        json={"path": "AI/elsewhere/note.md", "content": "n"},
    )
    assert unruled.status_code == 403
    assert unruled.json() == {
        "detail": "path 'AI/elsewhere/note.md' not writable by this token"
    }


def test_unknown_proposal_returns_404(tmp_path: Path) -> None:
    settings = _write_settings(tmp_path)
    client = authed_client(settings)

    approve = client.post("/api/v1/writes/999/approve")
    assert approve.status_code == 404

    reject = client.post("/api/v1/writes/999/reject")
    assert reject.status_code == 404


def test_write_listing_returns_pending_and_recent_resolved_newest_first(
    tmp_path: Path,
) -> None:
    """GET /api/v1/writes lists pending + terminal resolved, newest first.

    Proposals are seeded directly with explicit timestamps so the ordering
    assertions are deterministic (no sleeps), two rows share a timestamp to
    pin the deterministic tie-break, a ``reconciliation_required`` row
    proves that every terminal state is listed, and a transient
    ``applying`` row proves that in-flight writes are never listed.
    """
    settings = _write_settings(tmp_path)
    engine = create_database(settings.database_url)
    session = CatalogSession(bind=engine)
    try:
        session.add(
            MemoryWriteProposal(
                path="AI/proposed/a.md",
                content="A",
                operation="create",
                status="rejected",
                rule_access="propose-write",
                requested_at="2026-01-01T00:00:00+00:00",
                resolved_at="2026-01-01T00:00:01+00:00",
            )
        )
        session.add(
            MemoryWriteProposal(
                path="AI/proposed/applied.md",
                content="A1",
                operation="create",
                status="applied",
                rule_access="propose-write",
                requested_at="2026-01-02T00:00:00+00:00",
                resolved_at="2026-01-02T00:00:01+00:00",
                applied_content_hash="a1-hash",
            )
        )
        session.add(
            MemoryWriteProposal(
                path="AI/proposed/a-tie.md",
                content="A2",
                operation="create",
                status="rejected",
                rule_access="propose-write",
                requested_at="2026-01-01T00:00:00+00:00",
                resolved_at="2026-01-01T00:00:02+00:00",
            )
        )
        session.add(
            MemoryWriteProposal(
                path="AI/proposed/b.md",
                content="B",
                operation="create",
                status="pending",
                rule_access="propose-write",
                requested_at="2026-01-03T00:00:00+00:00",
            )
        )
        session.add(
            MemoryWriteProposal(
                path="AI/proposed/applying.md",
                content="C",
                operation="create",
                status="applying",
                rule_access="propose-write",
                requested_at="2026-01-04T00:00:00+00:00",
                applying_at="2026-01-04T00:00:01+00:00",
            )
        )
        session.add(
            MemoryWriteProposal(
                path="AI/proposed/recon.md",
                content="R",
                operation="create",
                status="reconciliation_required",
                rule_access="propose-write",
                requested_at="2026-01-06T00:00:00+00:00",
                resolved_at="2026-01-06T00:00:01+00:00",
                failure_reason=(
                    "reconciliation-required: write error; rollback failed"
                ),
            )
        )
        session.commit()
    finally:
        session.close()
        engine.dispose()

    client = authed_client(settings)
    listing = client.get("/api/v1/writes")
    assert listing.status_code == 200
    proposals = listing.json()["proposals"]

    # Newest requested_at first; the two rows sharing a timestamp are
    # tie-broken deterministically by id (newest id first).
    assert [p["path"] for p in proposals] == [
        "AI/proposed/recon.md",
        "AI/proposed/b.md",
        "AI/proposed/applied.md",
        "AI/proposed/a-tie.md",
        "AI/proposed/a.md",
    ]
    assert [p["status"] for p in proposals] == [
        "reconciliation_required",
        "pending",
        "applied",
        "rejected",
        "rejected",
    ]
    # Every listed record is either pending or a terminal record with a
    # resolved_at; the transient ``applying`` row is never listed.
    for item in proposals:
        if item["status"] == "pending":
            assert item["resolved_at"] is None
        else:
            assert item["status"] in {
                "applied",
                "rejected",
                "failed",
                "reconciliation_required",
            }
            assert item["resolved_at"] is not None
    assert all(p["status"] != "applying" for p in proposals)

    for item in proposals:
        assert set(item) == {
            "id",
            "path",
            "content",
            "operation",
            "status",
            "rule_access",
            "requested_at",
            "resolved_at",
            "failure_reason",
        }
        assert item["rule_access"] == "propose-write"
        assert item["requested_at"]
    assert proposals[1]["content"] == "B"
    assert proposals[3]["status"] == "rejected"
    assert proposals[3]["failure_reason"] is None
    # The reconciliation_required record is a listed terminal state.
    recon = proposals[0]
    assert recon["status"] == "reconciliation_required"
    assert recon["resolved_at"] is not None
    assert "reconciliation-required" in recon["failure_reason"]


def test_write_request_validation_errors_return_422(tmp_path: Path) -> None:
    settings = _write_settings(tmp_path)
    client = authed_client(settings)

    missing_content = client.post("/api/v1/writes", json={"path": "AI/proposed/x.md"})
    assert missing_content.status_code == 422

    missing_path = client.post("/api/v1/writes", json={"content": "x"})
    assert missing_path.status_code == 422

    extra_field = client.post(
        "/api/v1/writes",
        json={"path": "AI/proposed/x.md", "content": "x", "unexpected": 1},
    )
    assert extra_field.status_code == 422


def _auto_write_settings(tmp_path: Path) -> Settings:
    """Settings with an auto-write rule for the auto-write API tests."""
    vault = tmp_path / "vault"
    (vault / "AI" / "auto").mkdir(parents=True)
    return Settings(
        vault_path=vault,
        index_root="AI",
        folder_rules=(
            FolderRule(path=PurePosixPath("AI/auto"), access=FolderAccess.AUTO_WRITE),
        ),
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )


def test_write_events_reach_preconnected_sse_and_rest_history(tmp_path: Path) -> None:
    """Write audit events reach an already-connected SSE stream and REST history.

    The stream is connected (and confirmed via the replayed startup scan
    frame) *before* the write happens, proving the routes publish through
    the application-owned activity service. Every write audit event carries
    deterministic ``graph_refs``.

    TestClient's transport runs the ASGI app to completion before returning
    a response, so an infinite SSE body can never stream incrementally
    through it.  The stream is therefore driven directly as a background
    task on the client portal and torn down by sending http.disconnect.

    Teardown costs one keep-alive interval: disconnecting the stream
    abandons the bounded ``queue.get`` of its keep-alive worker thread, and
    closing the event loop waits for that worker to time out.
    """
    settings = _write_settings(tmp_path)
    (settings.vault_path / "AI" / "seed.md").write_text("# Seed\n", encoding="utf-8")
    application = create_app(settings)
    _, plaintext = application.state.token_service.create(
        "test-admin",
        rules=[
            FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE)
        ],
        admin=True,
    )

    write_path = "AI/proposed/stream.md"
    payloads: list[dict[str, object]] = []
    preconnected = threading.Event()
    requested_seen = threading.Event()
    applied_seen = threading.Event()

    def handle_frame(body: bytes) -> None:
        for line in body.decode("utf-8").splitlines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line.removeprefix("data: "))
            payloads.append(payload)
            event_type = str(payload["event_type"])
            if event_type == "scan":
                preconnected.set()
            elif event_type == "vault.mutation.requested":
                requested_seen.set()
            elif event_type == "vault.mutation.applied":
                applied_seen.set()

    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/v1/activity/stream",
        "raw_path": b"/api/v1/activity/stream",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {plaintext}".encode("ascii"))],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "extensions": {"http.response.debug": {}},
        "state": {},
    }

    client = TestClient(
        application, headers={"Authorization": f"Bearer {plaintext}"}
    )
    with client:  # lifespan records the startup scan before the stream opens
        portal = client.portal
        assert portal is not None
        disconnect = portal.call(asyncio.Event)

        async def run_stream() -> None:
            async def receive() -> dict[str, str]:
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message: dict[str, object]) -> None:
                if message["type"] == "http.response.start":
                    assert message["status"] == 200
                elif message["type"] == "http.response.body":
                    handle_frame(bytes(message.get("body", b"")))

            await application(scope, receive, send)

        stream_future = portal.start_task_soon(run_stream)
        try:
            assert preconnected.wait(10), "SSE stream did not deliver the replay"

            created = client.post(
                "/api/v1/writes",
                json={"path": write_path, "content": "# Stream\n"},
            )
            assert created.status_code == 200
            proposal_id = created.json()["id"]
            approved = client.post(f"/api/v1/writes/{proposal_id}/approve")
            assert approved.status_code == 200

            assert requested_seen.wait(10), "SSE stream missed the proposal event"
            assert applied_seen.wait(10), "SSE stream missed the applied event"

            requested = [
                payload
                for payload in payloads
                if payload["event_type"] == "vault.mutation.requested"
                and payload["payload"].get("proposal_id") == proposal_id
            ]
            applied = [
                payload
                for payload in payloads
                if payload["event_type"] == "vault.mutation.applied"
                and payload["payload"].get("proposal_id") == proposal_id
            ]
            assert len(requested) == 1
            assert len(applied) == 1
            assert requested[0]["payload"]["graph_refs"] == [write_path]
            assert applied[0]["payload"]["graph_refs"] == [write_path]

            # The REST history endpoint exposes the same audited trail.
            history = client.get("/api/v1/activity")
            assert history.status_code == 200
            events = history.json()["events"]
            history_requested = [
                event
                for event in events
                if event["event_type"] == "vault.mutation.requested"
                and event["payload"].get("proposal_id") == proposal_id
            ]
            history_applied = [
                event
                for event in events
                if event["event_type"] == "vault.mutation.applied"
                and event["payload"].get("proposal_id") == proposal_id
            ]
            assert len(history_requested) == 1
            assert len(history_applied) == 1
            assert history_requested[0]["payload"]["graph_refs"] == [write_path]
            assert history_applied[0]["payload"]["graph_refs"] == [write_path]
        finally:
            # Always stop the stream: on a failed assertion the task would
            # otherwise await ``disconnect`` forever and block portal
            # shutdown when the client exits.
            portal.call(disconnect.set)
            try:
                stream_future.result(timeout=10)
            except Exception:
                # The test already failed; a stream-task error must not
                # mask the original assertion.
                pass


def test_auto_write_applies_managed_update_immediately(tmp_path: Path) -> None:
    """An auto-write rule applies a managed update without any approval."""
    settings = _auto_write_settings(tmp_path)
    target = settings.vault_path / "AI" / "auto" / "managed.md"
    original = "---\nmanaged_by: harbor-ledger-memory\n---\n# Managed\nv1\n"
    target.write_text(original, encoding="utf-8")
    client = authed_client(settings)

    updated = original.replace("v1", "v2")
    response = client.post(
        "/api/v1/writes",
        json={"path": "AI/auto/managed.md", "content": updated},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "applied"
    assert data["rule_access"] == "auto-write"
    assert data["resolved_at"] is not None
    assert target.read_text(encoding="utf-8") == updated


def test_auto_write_keeps_create_proposals_pending(tmp_path: Path) -> None:
    """Auto-writes never create files; create proposals stay pending."""
    settings = _auto_write_settings(tmp_path)
    client = authed_client(settings)

    response = client.post(
        "/api/v1/writes", json={"path": "AI/auto/new.md", "content": "# New\n"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "pending"
    assert data["operation"] == "create"
    assert data["resolved_at"] is None
    assert not (settings.vault_path / "AI" / "auto" / "new.md").exists()


def test_auto_write_keeps_unmanaged_update_proposals_pending(tmp_path: Path) -> None:
    """Auto-writes only apply to plugin-managed notes; others stay pending."""
    settings = _auto_write_settings(tmp_path)
    target = settings.vault_path / "AI" / "auto" / "unmanaged.md"
    original = "# Unmanaged\nno managed_by frontmatter\n"
    target.write_text(original, encoding="utf-8")
    client = authed_client(settings)

    response = client.post(
        "/api/v1/writes",
        json={"path": "AI/auto/unmanaged.md", "content": original + "edited\n"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "pending"
    assert data["operation"] == "update"
    assert data["rule_access"] == "auto-write"
    assert data["resolved_at"] is None
    assert target.read_text(encoding="utf-8") == original


def _managed_vault(tmp_path: Path) -> tuple[Path, Settings, str]:
    """Vault with one managed note plus settings whose draft says propose."""
    vault = tmp_path / "vault"
    (vault / "AI" / "managed").mkdir(parents=True)
    target = vault / "AI" / "managed" / "note.md"
    original = "---\nmanaged_by: harbor-ledger-memory\n---\n# Note\nv1\n"
    target.write_text(original, encoding="utf-8")
    settings = Settings(
        vault_path=vault,
        index_root="AI",
        folder_rules=(
            FolderRule(
                path=PurePosixPath("AI/managed"), access=FolderAccess.PROPOSE_WRITE
            ),
        ),
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    return vault, settings, original


def _bearer_headers(
    token_service: Any, names: list[str]
) -> dict[str, dict[str, str]]:
    """Bearer header dicts for freshly created single-rule tokens."""
    access_by_name = {
        "propose": FolderAccess.PROPOSE_WRITE,
        "auto": FolderAccess.AUTO_WRITE,
        # Key kept for the tests that name the write-denying grant: the
        # retired "deny" level's equivalent is now "none".
        "deny": FolderAccess.NONE,
        "reader": FolderAccess.READ,
    }
    headers: dict[str, dict] = {}
    for name in names:
        _, plaintext = token_service.create(
            name,
            rules=[
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=access_by_name[name],
                )
            ],
        )
        headers[name] = {"Authorization": f"Bearer {plaintext}"}
    return headers


def test_approval_revalidates_access_rules_after_changes(tmp_path: Path) -> None:
    """Approvals revalidate the approver's current rules, not proposal time.

    The approver's own token grant decides: an auto-write grant allows the
    approval, a deny grant 403s it, and the proposal stays pending so a
    caller with write access can still resolve it.
    """
    vault, settings, original = _managed_vault(tmp_path)
    target = vault / "AI" / "managed" / "note.md"
    application = create_app(settings)
    headers = _bearer_headers(
        application.state.token_service, ["propose", "auto", "deny"]
    )
    client = TestClient(application)

    # A managed update under propose-write stays pending for approval.
    created = client.post(
        "/api/v1/writes",
        json={"path": "AI/managed/note.md", "content": original.replace("v1", "v2")},
        headers=headers["propose"],
    )
    assert created.status_code == 200
    assert created.json()["status"] == "pending"
    update_id = created.json()["id"]

    # The approver holds an auto-write grant: revalidation allows it.
    approved = client.post(
        f"/api/v1/writes/{update_id}/approve", headers=headers["auto"]
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "applied"
    assert target.read_text(encoding="utf-8") == original.replace("v1", "v2")

    # Creates stay pending even under an auto-write grant.
    create_resp = client.post(
        "/api/v1/writes",
        json={"path": "AI/managed/later.md", "content": "later"},
        headers=headers["auto"],
    )
    assert create_resp.status_code == 200
    assert create_resp.json()["status"] == "pending"
    create_id = create_resp.json()["id"]

    # The approver's grant denies the path: the approval 403s and the
    # proposal stays pending for a caller that can write it.
    denied = client.post(
        f"/api/v1/writes/{create_id}/approve", headers=headers["deny"]
    )
    assert denied.status_code == 403
    assert denied.json() == {
        "detail": "path 'AI/managed/later.md' not writable by this token"
    }

    listing = {
        proposal["id"]: proposal
        for proposal in client.get(
            "/api/v1/writes", headers=headers["propose"]
        ).json()["proposals"]
    }
    assert listing[create_id]["status"] == "pending"
    assert listing[create_id]["failure_reason"] is None
    assert not (vault / "AI" / "managed" / "later.md").exists()


def test_read_or_deny_rules_before_approval_block_the_write(tmp_path: Path) -> None:
    """A read-only or deny grant on the approver blocks the approval.

    The route rejects the approval before the mutation service runs, so
    the proposal stays pending for a caller that can write the path, and
    no failed-audit event is recorded.
    """
    vault, settings, original = _managed_vault(tmp_path)
    target = vault / "AI" / "managed" / "note.md"
    application = create_app(settings)
    headers = _bearer_headers(
        application.state.token_service, ["propose", "reader", "deny"]
    )
    client = TestClient(application)

    created = client.post(
        "/api/v1/writes",
        json={"path": "AI/managed/note.md", "content": original.replace("v1", "v2")},
        headers=headers["propose"],
    )
    assert created.status_code == 200
    assert created.json()["status"] == "pending"
    update_id = created.json()["id"]

    # The approver's grant is read-only: the approval 403s and the
    # proposal stays pending.
    read_only = client.post(
        f"/api/v1/writes/{update_id}/approve", headers=headers["reader"]
    )
    assert read_only.status_code == 403
    assert read_only.json() == {
        "detail": "path 'AI/managed/note.md' not writable by this token"
    }

    # A fresh proposal blocked by a deny grant 403s the same way.
    second = client.post(
        "/api/v1/writes",
        json={"path": "AI/managed/note.md", "content": original.replace("v1", "v3")},
        headers=headers["propose"],
    )
    assert second.status_code == 200
    second_id = second.json()["id"]
    denied = client.post(
        f"/api/v1/writes/{second_id}/approve", headers=headers["deny"]
    )
    assert denied.status_code == 403
    assert denied.json() == {
        "detail": "path 'AI/managed/note.md' not writable by this token"
    }

    # Both proposals are still pending and neither write touched the vault.
    listing = {
        proposal["id"]: proposal
        for proposal in client.get(
            "/api/v1/writes", headers=headers["propose"]
        ).json()["proposals"]
    }
    assert listing[update_id]["status"] == "pending"
    assert listing[update_id]["failure_reason"] is None
    assert listing[second_id]["status"] == "pending"
    assert listing[second_id]["failure_reason"] is None
    assert target.read_text(encoding="utf-8") == original

    # A caller with write access can still resolve the blocked proposal.
    approved = client.post(
        f"/api/v1/writes/{update_id}/approve", headers=headers["propose"]
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "applied"


def test_unrecovered_create_write_escalates_to_reconciliation_and_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create write that raises without a durable result needs reconciliation.

    The proposal resolves as the reconciliation_required terminal state, is
    listed by GET /api/v1/writes with a resolved_at, and its audit event
    carries deterministic graph refs.
    """
    settings = _write_settings(tmp_path)

    def boom(self, identity, content):
        raise OSError("disk full during fsync")

    monkeypatch.setattr(app_module.VaultBoundary, "atomic_create", boom)
    client = authed_client(settings)

    created = client.post(
        "/api/v1/writes", json={"path": "AI/proposed/r.md", "content": "# R\n"}
    )
    assert created.status_code == 200
    proposal_id = created.json()["id"]

    approved = client.post(f"/api/v1/writes/{proposal_id}/approve")
    assert approved.status_code == 200
    data = approved.json()
    assert data["status"] == "reconciliation_required"
    assert data["resolved_at"] is not None
    assert "reconciliation-required" in data["failure_reason"]

    # The terminal state is listed like any other resolved record.
    listing = {
        proposal["path"]: proposal
        for proposal in client.get("/api/v1/writes").json()["proposals"]
    }
    listed = listing["AI/proposed/r.md"]
    assert listed["status"] == "reconciliation_required"
    assert listed["resolved_at"] is not None

    history = client.get("/api/v1/activity").json()["events"]
    failed_events = [
        event
        for event in history
        if event["event_type"] == "vault.mutation.failed"
        and event["payload"].get("proposal_id") == proposal_id
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["payload"]["graph_refs"] == ["AI/proposed/r.md"]


def test_post_write_scan_reaches_application_activity_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-write rescan publishes through the application activity service.

    A live subscriber on the application-owned service must observe the
    rescan event that follows a write — not only the mutation audit
    events — so SSE clients see the catalog update in real time.  The
    file watcher is disabled so the post-write rescan is the only scan
    that can publish an event after the subscription.
    """
    settings = _write_settings(tmp_path)
    (settings.vault_path / "AI" / "seed.md").write_text("# Seed\n", encoding="utf-8")
    monkeypatch.setattr(app_module, "VaultWatchService", FakeWatcher)
    application = create_app(settings)
    _, plaintext = application.state.token_service.create(
        "test-admin",
        rules=[
            FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE)
        ],
        admin=True,
    )
    client = TestClient(
        application, headers={"Authorization": f"Bearer {plaintext}"}
    )
    with client:  # startup scan is recorded before the subscription
        subscriber = application.state.activity_service.subscribe()
        write_path = "AI/proposed/rescan.md"
        created = client.post(
            "/api/v1/writes", json={"path": write_path, "content": "# Rescan\n"}
        )
        assert created.status_code == 200
        proposal_id = created.json()["id"]
        approved = client.post(f"/api/v1/writes/{proposal_id}/approve")
        assert approved.status_code == 200
        assert approved.json()["status"] == "applied"

        seen: list[str] = []
        scan_payload: dict[str, object] | None = None
        try:
            while scan_payload is None:
                message = subscriber.get(timeout=10)
                seen.append(message.event_type)
                if message.event_type == "scan":
                    scan_payload = message.payload
        except queue.Empty as exc:
            raise AssertionError(
                f"post-write scan never reached the application activity "
                f"service (saw {seen})"
            ) from exc

        assert "vault.mutation.requested" in seen
        assert scan_payload["graph_refs"] == [write_path]
        assert scan_payload["added_paths"] == [write_path]


def test_settings_lists_index_root_directories_without_symlinks(tmp_path: Path) -> None:
    (tmp_path / "Projects" / "Client").mkdir(parents=True)
    (tmp_path / "Projects" / "Private").mkdir()
    (tmp_path / "Projects" / ".obsidian" / "plugins").mkdir(parents=True)
    (tmp_path / "Projects" / "Client" / "node_modules" / "package").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (tmp_path / "Projects" / "escape").symlink_to(
        tmp_path / "outside", target_is_directory=True
    )
    settings = Settings(
        vault_path=tmp_path,
        index_root="Projects",
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )

    response = authed_client(settings).get("/api/v1/settings")

    assert response.status_code == 200
    assert response.json()["folders"] == [
        "Projects",
        "Projects/Client",
        "Projects/Private",
    ]
