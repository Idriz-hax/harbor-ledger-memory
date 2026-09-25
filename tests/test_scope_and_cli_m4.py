"""Milestone 4 scope isolation and CLI migration regressions."""

from pathlib import Path

from fastapi.testclient import TestClient

from harbor_ledger_memory import cli
from harbor_ledger_memory.api.app import _swept_sse_frame, create_app
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import Note, QueryTrace
from harbor_ledger_memory.config import ApiSettings, Settings
from harbor_ledger_memory.domain.retrieval import QueryRequest
from harbor_ledger_memory.services.access import AccessPolicy
from harbor_ledger_memory.services.activity import ActivityMessage, ActivityService
from harbor_ledger_memory.services.memory_marks import (
    MemoryMarkScope,
    MemoryMarkService,
)
from harbor_ledger_memory.services.query import QueryService


def test_rest_query_activity_and_trace_replay_are_scope_owned(tmp_path: Path) -> None:
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'm4.db'}",
        api=ApiSettings(enabled=True),
    )
    app = create_app(settings)
    first = app.state.token_service.create("first", rules=()).plaintext
    second = app.state.token_service.create("second", rules=()).plaintext
    with TestClient(app, headers={"Authorization": f"Bearer {first}"}) as client:
        query = client.post("/api/v1/queries", json={"query": "nothing"})
        trace_id = query.json()["trace_id"]
        own = client.get("/api/v1/activity/history").json()["events"]
        assert any(event["payload"].get("trace_id") == trace_id for event in own)
    with TestClient(app, headers={"Authorization": f"Bearer {second}"}) as client:
        assert not any(
            event["event_type"] == "query"
            for event in client.get("/api/v1/activity/history").json()["events"]
        )
        assert client.get(f"/api/v1/traces/{trace_id}").status_code == 404


def test_legacy_unscoped_query_activity_is_hidden_from_scoped_callers(
    tmp_path: Path,
) -> None:
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'm4.db'}",
        api=ApiSettings(enabled=True),
    )
    app = create_app(settings)
    token = app.state.token_service.create("scoped", rules=()).plaintext
    engine = create_database(settings.database_url)
    session = CatalogSession(bind=engine)
    session.add(
        QueryTrace(
            trace_uuid="legacy",
            query="legacy",
            retrieval_settings="{}",
            status="completed",
        )
    )
    session.commit()
    session.close()
    engine.dispose()
    app.state.activity_service.record(
        "query", {"trace_id": "legacy", "selected_paths": []}
    )
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        assert not any(
            event["event_type"] == "query"
            for event in client.get("/api/v1/activity/history").json()["events"]
        )
        assert client.get("/api/v1/traces/legacy").status_code == 404


def test_activity_sse_filters_query_events_by_scope() -> None:
    message = ActivityMessage(
        id=1,
        event_type="query",
        created_at="now",
        payload={"trace_id": "trace", "scope_kind": "token", "scope_id": "one"},
    )
    frame = ActivityService.sse_message(message)
    assert _swept_sse_frame(AccessPolicy(()), frame, ("token", "two")) is None
    assert _swept_sse_frame(AccessPolicy(()), frame, ("token", "one")) is not None


def test_cli_catalog_runs_pending_migrations(monkeypatch, tmp_path: Path) -> None:
    called: list[str] = []
    monkeypatch.setattr(cli, "upgrade_to_head", lambda url: called.append(url))
    settings = Settings(
        vault_path=tmp_path, database_url=f"sqlite:///{tmp_path / 'old.db'}"
    )
    with cli._catalog(settings):
        pass
    assert called == [settings.database_url]


def test_old_create_all_catalog_gets_m4_trace_columns_before_head_stamp(
    tmp_path: Path,
) -> None:
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'legacy.db'}",
    )
    engine = create_database(settings.database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_query_traces_scope_kind")
        connection.exec_driver_sql("DROP INDEX ix_query_traces_scope_id")
        connection.exec_driver_sql("ALTER TABLE query_traces DROP COLUMN scope_kind")
        connection.exec_driver_sql("ALTER TABLE query_traces DROP COLUMN scope_id")
        connection.exec_driver_sql("DROP TABLE memory_marks")
    engine.dispose()

    with cli._catalog(settings) as session:
        session.add(
            Note(
                path="Public/legacy.md",
                title="Legacy",
                content="legacy",
                content_hash="h",
            )
        )
        session.commit()
        result = QueryService(session).query(
            QueryRequest(query="legacy", scope_kind="cli", scope_id="local")
        )
        mark = MemoryMarkService(session).create_mark(
            MemoryMarkScope("cli", "local"),
            str(result.trace_id),
            "Public/legacy.md",
            "pin",
            lambda _: True,
        )
        assert mark.path == "Public/legacy.md"
