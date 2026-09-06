"""Persistence and HTTP coverage for live activity telemetry."""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import networkx as nx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import String, Text

from conftest import authed_client
from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import ActivityEvent, Link, Note
from harbor_ledger_memory.config import ApiSettings, Settings
from harbor_ledger_memory.domain.retrieval import (
    ActivatedNode,
    QueryRequest,
    QuerySettings,
    SeedCandidate,
)
from harbor_ledger_memory.graph.activation import spread_activation
from harbor_ledger_memory.services.activity import (
    ActivityMessage,
    ActivityService,
    graph_refs,
)
from harbor_ledger_memory.services.query import (
    QueryService,
    _activation_segments,
)
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.vault.boundary import VaultBoundary


def test_activity_service_persists_events_and_cleans_up_after_thirty_days(
    tmp_path: Path,
) -> None:
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    service = ActivityService(engine)
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()

    with CatalogSession(bind=engine) as session:
        session.add(
            ActivityEvent(
                event_type="old",
                created_at=old,
                payload_json=json.dumps({"stale": True}),
            )
        )
        session.commit()

    service.record("query", {"query": "memory"})

    with CatalogSession(bind=engine) as session:
        events = list(session.query(ActivityEvent).order_by(ActivityEvent.id))
        assert len(events) == 1
        assert events[0].event_type == "query"
        assert json.loads(events[0].payload_json) == {"query": "memory"}

    service.close()
    engine.dispose()


def test_activity_endpoints_emit_history_for_scan_query_and_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "AI").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        api=ApiSettings(enabled=True),
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    with authed_client(settings) as client:
        query = client.post("/api/v1/queries", json={"query": "memory"})
        assert query.status_code == 200

        config = client.put("/api/v1/settings", json={"index_root": "AI"})
        assert config.status_code == 200

        history = client.get("/api/v1/activity/history")
        assert history.status_code == 200
        events = history.json()["events"]
        assert {event["event_type"] for event in events} >= {
            "scan",
            "query",
            "config",
        }
        assert all("payload" in event for event in events)


def test_activity_sse_serializes_event_payload(tmp_path: Path) -> None:
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    service = ActivityService(engine)
    event = service.record("query", {"query": "memory"})

    message = service.sse_message(event)

    assert f"id: {event.id}" in message
    assert "event: activity" in message
    assert "data: " in message
    assert json.loads(message.split("data: ", 1)[1].strip())["event_type"] == "query"

    service.close()
    engine.dispose()


def test_stage_defers_commit_and_publish_until_transaction_succeeds(
    tmp_path: Path,
) -> None:
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    with CatalogSession(bind=engine) as session:
        service = ActivityService(session)
        subscriber = service.subscribe()

        message = service.stage("vault.mutation.applied", {"proposal_id": 7})

        # Staged but uncommitted: invisible to other sessions ...
        with CatalogSession(bind=engine) as fresh:
            staged = list(fresh.query(ActivityEvent))
            assert len(staged) == 0
        # ... and not fanned out to live subscribers.
        assert subscriber.empty()

        session.commit()
        service.publish(message)

    with CatalogSession(bind=engine) as fresh:
        rows = list(fresh.query(ActivityEvent))
        assert len(rows) == 1
        assert rows[0].event_type == "vault.mutation.applied"
    assert subscriber.get_nowait().id == message.id
    service.close()
    engine.dispose()


def test_rollback_discards_staged_event(tmp_path: Path) -> None:
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    with CatalogSession(bind=engine) as session:
        service = ActivityService(session)
        service.stage("vault.mutation.failed", {"proposal_id": 9})
        session.rollback()

    with CatalogSession(bind=engine) as fresh:
        rows = list(fresh.query(ActivityEvent))
        assert len(rows) == 0
    engine.dispose()


def test_activity_event_persists_graph_metadata_columns(tmp_path: Path) -> None:
    """Graph correlation metadata round-trips through the ORM."""
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    with CatalogSession(bind=engine) as session:
        session.add(
            ActivityEvent(
                event_type="vault.mutation.applied",
                created_at="2026-08-19T00:00:00+00:00",
                payload_json='{"proposal_id": 3}',
                operation_id="op-3",
                run_id="run-3",
                agent_id="agent-3",
                parent_id="evt-1",
                graph_refs_json='["AI/notes/n.md"]',
            )
        )
        session.add(
            ActivityEvent(
                event_type="scan",
                created_at="2026-08-19T00:01:00+00:00",
            )
        )
        session.commit()

    with CatalogSession(bind=engine) as fresh:
        rows = fresh.query(ActivityEvent).order_by(ActivityEvent.id).all()

    assert (
        rows[0].operation_id,
        rows[0].run_id,
        rows[0].agent_id,
        rows[0].parent_id,
        rows[0].graph_refs_json,
    ) == ("op-3", "run-3", "agent-3", "evt-1", '["AI/notes/n.md"]')
    # Events without correlation metadata keep the columns NULL.
    assert (
        rows[1].operation_id,
        rows[1].run_id,
        rows[1].agent_id,
        rows[1].parent_id,
        rows[1].graph_refs_json,
    ) == (None, None, None, None, None)
    engine.dispose()


def test_activity_event_graph_metadata_columns_match_historical_schema() -> None:
    """The graph metadata columns use the immutable historical types."""
    table = ActivityEvent.__table__
    for name in ("operation_id", "run_id", "agent_id", "parent_id"):
        column = table.c[name]
        assert isinstance(column.type, String)
        assert column.type.length == 256
        assert column.nullable is True
    assert isinstance(table.c["graph_refs_json"].type, Text)
    assert table.c["graph_refs_json"].nullable is True


def test_record_persists_graph_metadata_and_preserves_payload(
    tmp_path: Path,
) -> None:
    """record persists correlation metadata into the dedicated columns.

    The structured fields populate the ``ActivityEvent`` columns while the
    payload is stored verbatim, so the historical payload/SSE envelope and
    the history roundtrip are unchanged.
    """
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    service = ActivityService(engine)
    payload = {
        "query": "memory",
        "graph_refs": ["AI/notes/b.md", "AI/notes/a.md"],
    }
    metadata = {
        "operation_id": "op-42",
        "run_id": "run-42",
        "agent_id": "agent-42",
        "parent_id": "parent-42",
    }

    message = service.record("query", payload, metadata=metadata)

    # The message keeps the historical four-field envelope and payload.
    assert message.event_type == "query"
    assert message.payload == payload
    assert set(message.as_dict()) == {
        "id",
        "event_type",
        "created_at",
        "payload",
    }

    # The dedicated columns are populated from the metadata.
    with CatalogSession(bind=engine) as fresh:
        row = fresh.get(ActivityEvent, message.id)
    assert row is not None
    assert row.operation_id == "op-42"
    assert row.run_id == "run-42"
    assert row.agent_id == "agent-42"
    assert row.parent_id == "parent-42"
    assert row.graph_refs_json is not None
    assert json.loads(row.graph_refs_json) == payload["graph_refs"]
    assert json.loads(row.payload_json) == payload

    # The history roundtrip preserves the envelope and the payload.
    roundtripped = next(event for event in service.history() if event.id == message.id)
    assert roundtripped.payload == payload
    service.close()
    engine.dispose()


def test_stage_persists_graph_metadata_and_preserves_payload(
    tmp_path: Path,
) -> None:
    """stage persists correlation metadata inside the caller transaction.

    The staged row carries the correlation metadata and the verbatim payload
    once the caller commits; the message keeps the historical envelope.
    """
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    with CatalogSession(bind=engine) as session:
        service = ActivityService(session)
        payload = {
            "proposal_id": 7,
            "graph_refs": ["AI/notes/a.md"],
        }
        metadata = {
            "operation_id": "op-stage",
            "run_id": "run-stage",
            "agent_id": "agent-stage",
            "parent_id": "parent-stage",
        }

        message = service.stage("vault.mutation.applied", payload, metadata=metadata)
        assert message.payload == payload
        session.commit()

    with CatalogSession(bind=engine) as fresh:
        row = fresh.get(ActivityEvent, message.id)
    assert row is not None
    assert row.event_type == "vault.mutation.applied"
    assert row.operation_id == "op-stage"
    assert row.run_id == "run-stage"
    assert row.agent_id == "agent-stage"
    assert row.parent_id == "parent-stage"
    assert row.graph_refs_json is not None
    assert json.loads(row.graph_refs_json) == ["AI/notes/a.md"]
    assert json.loads(row.payload_json) == payload
    engine.dispose()


def test_graph_refs_from_payload_populates_column_without_metadata(
    tmp_path: Path,
) -> None:
    """graph_refs already in the payload populates graph_refs_json.

    The historical scan/query convention carries ``graph_refs`` inside the
    payload; that value must populate the structured column while the other
    correlation columns stay NULL and the payload is preserved verbatim.
    """
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    service = ActivityService(engine)
    payload = {"graph_refs": ["AI/notes/z.md", "AI/notes/a.md"]}

    message = service.record("scan", payload)

    with CatalogSession(bind=engine) as fresh:
        row = fresh.get(ActivityEvent, message.id)
    assert row is not None
    assert row.graph_refs_json is not None
    assert json.loads(row.graph_refs_json) == payload["graph_refs"]
    assert (
        row.operation_id,
        row.run_id,
        row.agent_id,
        row.parent_id,
    ) == (None, None, None, None)
    assert json.loads(row.payload_json) == payload
    assert message.payload == payload
    service.close()
    engine.dispose()


def test_record_without_metadata_keeps_columns_null_and_payload_intact(
    tmp_path: Path,
) -> None:
    """A legacy record without metadata keeps every correlation column NULL."""
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    service = ActivityService(engine)
    payload = {"query": "legacy", "value": 1}

    message = service.record("query", payload)

    with CatalogSession(bind=engine) as fresh:
        row = fresh.get(ActivityEvent, message.id)
    assert row is not None
    assert (
        row.operation_id,
        row.run_id,
        row.agent_id,
        row.parent_id,
        row.graph_refs_json,
    ) == (None, None, None, None, None)
    assert json.loads(row.payload_json) == payload
    assert message.payload == payload
    service.close()
    engine.dispose()


def test_metadata_graph_refs_take_priority_over_payload(
    tmp_path: Path,
) -> None:
    """An explicit metadata value wins over the same key carried in payload."""
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    service = ActivityService(engine)
    payload = {
        "graph_refs": ["AI/notes/payload.md"],
        "operation_id": "op-payload",
    }
    metadata = {
        "graph_refs": ["AI/notes/meta.md"],
        "operation_id": "op-meta",
    }

    message = service.record("query", payload, metadata=metadata)

    with CatalogSession(bind=engine) as fresh:
        row = fresh.get(ActivityEvent, message.id)
    assert row is not None
    assert row.graph_refs_json is not None
    assert json.loads(row.graph_refs_json) == ["AI/notes/meta.md"]
    assert row.operation_id == "op-meta"
    # The payload is still stored verbatim (legacy envelope preserved).
    assert json.loads(row.payload_json) == payload
    assert message.payload == payload
    service.close()
    engine.dispose()


def test_activity_sse_endpoint_returns_event_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "AI").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        api=ApiSettings(enabled=True),
    )
    application = create_app(settings)
    _, plaintext = application.state.token_service.create("test-admin", admin=True)
    service = application.state.activity_service
    event = service.record("query", {"query": "stream"})

    async def one_event(*, after_id: int | None = None) -> AsyncGenerator[str, None]:
        del after_id
        yield service.sse_message(event)

    monkeypatch.setattr(service, "stream", one_event)
    with TestClient(
        application, headers={"Authorization": f"Bearer {plaintext}"}
    ) as client:
        response = client.get("/api/v1/activity/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert f"id: {event.id}" in response.text


def _sse_payload(message: ActivityMessage) -> dict[str, Any]:
    """Parse the ``data`` field of an SSE frame back into a dict."""
    frame = ActivityService.sse_message(message)
    return json.loads(frame.split("data: ", 1)[1].strip())


def _write_scan_vault(vault: Path) -> None:
    ai = vault / "AI"
    (ai / "Knowledge").mkdir(parents=True)
    (ai / "INDEX.md").write_text(
        "# Root\n\n[[AI/Knowledge/example]]\n", encoding="utf-8"
    )
    (ai / "Knowledge" / "example.md").write_text(
        "---\ntype: knowledge\nstatus: active\n---\n# Example\n", encoding="utf-8"
    )


def _build_query_catalog(tmp_path: Path):
    """Seed a catalog whose traversal is fully deterministic.

    ``root`` is the only lexical match for ``memory``; ``alpha`` and ``beta``
    are reached only via ``root``'s explicit wikilinks, so the activation
    traversal is exactly ``root -> alpha`` and ``root -> beta``.
    """
    engine = create_database(f"sqlite:///{tmp_path / 'query.db'}")
    with CatalogSession(bind=engine) as session:
        root = Note(
            path="AI/notes/root.md",
            title="Memory Root",
            content="memory systems and retrieval.",
            summary="memory",
            frontmatter_json="{}",
            content_hash="root-hash",
        )
        alpha = Note(
            path="AI/notes/alpha.md",
            title="Alpha",
            content="alpha details",
            frontmatter_json="{}",
            content_hash="alpha-hash",
        )
        beta = Note(
            path="AI/notes/beta.md",
            title="Beta",
            content="beta details",
            frontmatter_json="{}",
            content_hash="beta-hash",
        )
        root.links.append(
            Link(
                raw="[[alpha]]",
                normalized_target="AI/notes/alpha.md",
                explicit=True,
                resolution_status="resolved",
            )
        )
        root.links.append(
            Link(
                raw="[[beta]]",
                normalized_target="AI/notes/beta.md",
                explicit=True,
                resolution_status="resolved",
            )
        )
        session.add(root)
        session.add(alpha)
        session.add(beta)
        session.commit()
    return engine


def test_scan_activity_payload_includes_graph_refs(tmp_path: Path) -> None:
    """A scan records the indexed note paths as deterministic ``graph_refs``."""
    vault = tmp_path / "vault"
    _write_scan_vault(vault)
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    activity = ActivityService(engine)
    subscriber = activity.subscribe()
    service = ScanService(
        VaultBoundary(Settings(vault_path=vault, index_root=PurePosixPath("AI"))),
        engine,
        activity_service=activity,
    )
    try:
        result = service.full_scan()
        message = subscriber.get_nowait()
        expected_refs = sorted(result.indexed_paths)
        assert expected_refs

        # A first scan only adds paths: nothing was changed or deleted.
        assert result.added_paths == tuple(expected_refs)
        assert result.changed_paths == ()
        assert result.deleted_paths == ()

        # Streamed/message payload carries the graph refs and the delta.
        assert message.event_type == "scan"
        assert message.payload["graph_refs"] == expected_refs
        assert message.payload["added_paths"] == expected_refs
        assert message.payload["changed_paths"] == []
        assert message.payload["deleted_paths"] == []

        # The SSE frame keeps the existing envelope and the same graph refs.
        assert f"id: {message.id}" in ActivityService.sse_message(message)
        assert "event: activity" in ActivityService.sse_message(message)
        sse_payload = _sse_payload(message)
        assert sse_payload["event_type"] == "scan"
        assert sse_payload["payload"]["graph_refs"] == expected_refs
        assert sse_payload["payload"]["added_paths"] == expected_refs

        # The persisted history event carries the same fields.
        scan_events = [
            event for event in activity.history() if event.event_type == "scan"
        ]
        assert len(scan_events) == 1
        assert scan_events[0].payload["graph_refs"] == expected_refs
        assert scan_events[0].payload["added_paths"] == expected_refs
        assert scan_events[0].payload["changed_paths"] == []
        assert scan_events[0].payload["deleted_paths"] == []
    finally:
        activity.close()
        engine.dispose()


def test_query_activity_payload_includes_graph_refs_and_path(tmp_path: Path) -> None:
    """A query records graph refs plus the ordered activation traversal."""
    engine = _build_query_catalog(tmp_path)
    activity = ActivityService(engine)
    subscriber = activity.subscribe()
    service = QueryService(
        engine,
        activity_service=activity,
        retrieval_settings=QuerySettings(
            max_hops=3,
            max_activation_nodes=50,
            decay=0.65,
            minimum_activation=0.05,
            context_token_budget=12_000,
        ),
    )
    try:
        result = service.query(QueryRequest(query="memory"))
        message = subscriber.get_nowait()

        assert result.selected_memories
        expected_refs = [
            "AI/notes/alpha.md",
            "AI/notes/beta.md",
            "AI/notes/root.md",
        ]
        expected_path = [
            {
                "source": "AI/notes/root.md",
                "target": "AI/notes/alpha.md",
                "edge_type": "links_to",
                "traversal_direction": "forward",
            },
            {
                "source": "AI/notes/root.md",
                "target": "AI/notes/beta.md",
                "edge_type": "links_to",
                "traversal_direction": "forward",
            },
        ]

        # Streamed/message payload carries the graph refs and traversal.
        assert message.event_type == "query"
        assert message.payload["graph_refs"] == expected_refs
        assert message.payload["graph_path"] == expected_path

        # The SSE frame keeps the existing envelope and the same graph data.
        assert f"id: {message.id}" in ActivityService.sse_message(message)
        assert "event: activity" in ActivityService.sse_message(message)
        sse_payload = _sse_payload(message)
        assert sse_payload["event_type"] == "query"
        assert sse_payload["payload"]["graph_refs"] == expected_refs
        assert sse_payload["payload"]["graph_path"] == expected_path

        # The persisted history event carries the same fields and envelope.
        query_events = [
            event for event in activity.history() if event.event_type == "query"
        ]
        assert len(query_events) == 1
        persisted = query_events[0].payload
        assert persisted["graph_refs"] == expected_refs
        assert persisted["graph_path"] == expected_path
        assert persisted["selected_paths"]
        assert persisted["trace_id"]
    finally:
        activity.close()
        engine.dispose()


def test_graph_refs_deduplicates_and_sorts_paths() -> None:
    """``graph_refs`` is deterministic: sorted, de-duplicated, no empties."""
    assert graph_refs([]) == []
    assert graph_refs(["b.md", "a.md", "b.md", "", "c.md", "a.md"]) == [
        "a.md",
        "b.md",
        "c.md",
    ]


def _projection_graph() -> nx.MultiDiGraph:
    """A directed graph mirroring the structural edge projection.

    ``parent --parent_of--> root --links_to--> leaf --contains--> detail``
    """
    graph: nx.MultiDiGraph = nx.MultiDiGraph()
    for path in ("parent.md", "root.md", "leaf.md", "detail.md"):
        graph.add_node(path)
    graph.add_edge("parent.md", "root.md", edge_type="parent_of", weight=1.0)
    graph.add_edge("root.md", "leaf.md", edge_type="links_to", weight=1.0)
    graph.add_edge("leaf.md", "detail.md", edge_type="contains", weight=0.5)
    return graph


def _activate(graph: nx.MultiDiGraph, seed_path: str, **overrides: object):
    kwargs: dict[str, object] = {
        "max_hops": 1,
        "max_nodes": 100,
        "decay": 0.5,
        "minimum_activation": 0.0,
        "edge_types": frozenset({"links_to", "contains", "parent_of"}),
    }
    kwargs.update(overrides)
    return spread_activation(
        graph,
        [SeedCandidate(path=seed_path, retrieval_score=1.0)],
        **kwargs,  # type: ignore[arg-type]
    )


def test_reverse_links_to_segment_matches_directed_edge() -> None:
    """Reverse links_to activation keeps the edge's structural orientation."""
    graph = _projection_graph()
    # Seed the *target* of the root -> leaf edge: reaching root traverses
    # the structural edge in reverse.
    activated = _activate(graph, "leaf.md")
    root = next(node for node in activated if node.path == "root.md")
    assert root.via_path == "leaf.md"
    assert root.edge_type == "links_to"

    segments = _activation_segments(activated)

    assert segments == [
        {
            "source": "leaf.md",
            "target": "detail.md",
            "edge_type": "contains",
            "traversal_direction": "forward",
        },
        {
            "source": "root.md",
            "target": "leaf.md",
            "edge_type": "links_to",
            "traversal_direction": "reverse",
        },
    ]


def test_reverse_parent_of_segment_matches_directed_edge() -> None:
    """Reverse parent_of activation keeps the edge's structural orientation."""
    graph = _projection_graph()
    # Seed the child: reaching parent traverses parent -> child in reverse.
    activated = _activate(graph, "root.md")
    parent = next(node for node in activated if node.path == "parent.md")
    assert parent.via_path == "root.md"
    assert parent.edge_type == "parent_of"

    segments = _activation_segments(activated)

    assert segments == [
        {
            "source": "root.md",
            "target": "leaf.md",
            "edge_type": "links_to",
            "traversal_direction": "forward",
        },
        {
            "source": "parent.md",
            "target": "root.md",
            "edge_type": "parent_of",
            "traversal_direction": "reverse",
        },
    ]


def test_reverse_contains_segment_matches_directed_edge() -> None:
    """Reverse contains activation keeps the edge's structural orientation."""
    graph = _projection_graph()
    # Seed the contained note: reaching leaf traverses leaf -> detail
    # in reverse.
    activated = _activate(graph, "detail.md")
    leaf = next(node for node in activated if node.path == "leaf.md")
    assert leaf.via_path == "detail.md"
    assert leaf.edge_type == "contains"

    segments = _activation_segments(activated)

    assert segments == [
        {
            "source": "leaf.md",
            "target": "detail.md",
            "edge_type": "contains",
            "traversal_direction": "reverse",
        },
    ]


def test_forward_segments_keep_structural_orientation() -> None:
    """Forward traversal reports the directed edge unchanged."""
    graph = _projection_graph()
    activated = _activate(graph, "parent.md", max_hops=3)

    segments = _activation_segments(activated)

    assert segments == [
        {
            "source": "parent.md",
            "target": "root.md",
            "edge_type": "parent_of",
            "traversal_direction": "forward",
        },
        {
            "source": "root.md",
            "target": "leaf.md",
            "edge_type": "links_to",
            "traversal_direction": "forward",
        },
        {
            "source": "leaf.md",
            "target": "detail.md",
            "edge_type": "contains",
            "traversal_direction": "forward",
        },
    ]


def test_every_segment_is_a_directed_projection_edge() -> None:
    """Each segment corresponds to a directed edge of the graph projection."""
    graph = _projection_graph()
    for seed in ("parent.md", "root.md", "leaf.md", "detail.md"):
        activated = _activate(graph, seed)
        for segment in _activation_segments(activated):
            attrs = graph.get_edge_data(segment["source"], segment["target"])
            assert attrs is not None
            assert any(
                data.get("edge_type") == segment["edge_type"] for data in attrs.values()
            )


def test_segments_are_ordered_by_hop_then_path_regardless_of_input_order() -> None:
    graph = _projection_graph()
    activated = _activate(graph, "leaf.md")
    shuffled = tuple(reversed(activated))

    assert _activation_segments(shuffled) == _activation_segments(activated)


def test_segments_ignore_seeds_and_use_intrinsic_provenance() -> None:
    """Seeds contribute no segment; segments read provenance from the node.

    Nodes built the way ``spread_activation`` builds them carry their
    selected-edge provenance and it is reported verbatim.  A node without
    provenance (a bare fixture) falls back to the traversal order.
    """
    nodes = (
        ActivatedNode(
            path="root.md",
            activation_score=0.5,
            hop=1,
            via_path="leaf.md",
            edge_type="links_to",
            edge_source="root.md",
            edge_target="leaf.md",
            traversal_direction="reverse",
        ),
        ActivatedNode(
            path="detail.md",
            activation_score=0.25,
            hop=1,
            via_path="leaf.md",
            edge_type="contains",
            edge_source="leaf.md",
            edge_target="detail.md",
            traversal_direction="forward",
        ),
        ActivatedNode(
            path="leaf.md",
            activation_score=1.0,
            hop=0,
            via_path=None,
            edge_type=None,
        ),
    )

    segments = _activation_segments(nodes)

    assert segments == [
        {
            "source": "leaf.md",
            "target": "detail.md",
            "edge_type": "contains",
            "traversal_direction": "forward",
        },
        {
            "source": "root.md",
            "target": "leaf.md",
            "edge_type": "links_to",
            "traversal_direction": "reverse",
        },
    ]


def test_segments_fall_back_to_traversal_order_without_provenance() -> None:
    """Bare hand-built nodes without provenance report the traversal order."""
    nodes = (
        ActivatedNode(
            path="root.md",
            activation_score=0.5,
            hop=1,
            via_path="leaf.md",
            edge_type="links_to",
        ),
        ActivatedNode(
            path="leaf.md",
            activation_score=1.0,
            hop=0,
            via_path=None,
            edge_type=None,
        ),
    )

    segments = _activation_segments(nodes)

    assert segments == [
        {
            "source": "leaf.md",
            "target": "root.md",
            "edge_type": "links_to",
            "traversal_direction": "forward",
        },
    ]


def test_reciprocal_same_type_edge_heavier_reverse_route_wins() -> None:
    """A reciprocal links_to pair resolves to the route the traversal took.

    With ``a -> b`` weighted 0.5 and ``b -> a`` weighted 1.0, seeding ``a``
    reaches ``b`` through the heavier edge, i.e. the *reverse* traversal of
    ``b -> a``.  ``spread_activation`` must report that structural edge
    intrinsically, not the ambiguous via/target pair.
    """
    graph: nx.MultiDiGraph = nx.MultiDiGraph()
    graph.add_node("a.md")
    graph.add_node("b.md")
    graph.add_edge("a.md", "b.md", edge_type="links_to", weight=0.5)
    reverse_key = graph.add_edge("b.md", "a.md", edge_type="links_to", weight=1.0)

    activated = _activate(graph, "a.md")
    b = next(node for node in activated if node.path == "b.md")
    # Winning route: reverse traversal of b -> a (1.0 * 1.0 * decay 0.5).
    assert b.via_path == "a.md"
    assert b.edge_type == "links_to"
    assert b.activation_score == 0.5
    assert (b.edge_source, b.edge_target) == ("b.md", "a.md")
    assert b.traversal_direction == "reverse"
    assert b.edge_key == reverse_key

    segments = _activation_segments(activated)
    assert segments == [
        {
            "source": "b.md",
            "target": "a.md",
            "edge_type": "links_to",
            "traversal_direction": "reverse",
        },
    ]


def test_reciprocal_same_type_edge_tie_keeps_forward_orientation() -> None:
    """Equal reciprocal weights resolve forward, matching traversal order.

    ``spread_activation`` enumerates outgoing edges before incoming ones and
    keeps the first route on a score tie, so the forward edge ``a -> b`` is
    the traversal's winning route and the provenance must report it.
    """
    graph: nx.MultiDiGraph = nx.MultiDiGraph()
    graph.add_node("a.md")
    graph.add_node("b.md")
    graph.add_edge("a.md", "b.md", edge_type="links_to", weight=1.0)
    graph.add_edge("b.md", "a.md", edge_type="links_to", weight=1.0)

    activated = _activate(graph, "a.md")
    b = next(node for node in activated if node.path == "b.md")
    assert b.activation_score == 0.5
    assert (b.edge_source, b.edge_target) == ("a.md", "b.md")
    assert b.traversal_direction == "forward"

    segments = _activation_segments(activated)
    assert segments == [
        {
            "source": "a.md",
            "target": "b.md",
            "edge_type": "links_to",
            "traversal_direction": "forward",
        },
    ]


def _query_catalog_with_reverse_links(tmp_path: Path):
    """Seed the *target* side of a structural links_to edge.

    The only edge is ``root --links_to--> alpha`` and ``alpha`` is the only
    lexical match for ``memory``, so activation reaches ``root`` by
    traversing the edge in reverse.
    """
    engine = create_database(f"sqlite:///{tmp_path / 'reverse.db'}")
    with CatalogSession(bind=engine) as session:
        root = Note(
            path="AI/notes/root.md",
            title="Root",
            content="plain root details",
            frontmatter_json="{}",
            content_hash="root-hash",
        )
        alpha = Note(
            path="AI/notes/alpha.md",
            title="Alpha",
            content="memory systems and retrieval.",
            frontmatter_json="{}",
            content_hash="alpha-hash",
        )
        root.links.append(
            Link(
                raw="[[alpha]]",
                normalized_target="AI/notes/alpha.md",
                explicit=True,
                resolution_status="resolved",
            )
        )
        session.add(root)
        session.add(alpha)
        session.commit()
    return engine


def test_query_activity_records_reverse_links_to_traversal(tmp_path: Path) -> None:
    engine = _query_catalog_with_reverse_links(tmp_path)
    activity = ActivityService(engine)
    subscriber = activity.subscribe()
    service = QueryService(engine, activity_service=activity)
    try:
        result = service.query(QueryRequest(query="memory"))
        message = subscriber.get_nowait()

        assert result.selected_memories
        assert message.event_type == "query"
        assert message.payload["graph_refs"] == [
            "AI/notes/alpha.md",
            "AI/notes/root.md",
        ]
        assert message.payload["graph_path"] == [
            {
                "source": "AI/notes/root.md",
                "target": "AI/notes/alpha.md",
                "edge_type": "links_to",
                "traversal_direction": "reverse",
            },
        ]
    finally:
        activity.close()
        engine.dispose()


def test_query_activity_records_reverse_parent_of_traversal(tmp_path: Path) -> None:
    """A frontmatter parent reference is traversed in reverse from the child."""
    engine = create_database(f"sqlite:///{tmp_path / 'parent.db'}")
    with CatalogSession(bind=engine) as session:
        root = Note(
            path="AI/notes/root.md",
            title="Root",
            content="plain root details",
            frontmatter_json="{}",
            content_hash="root-hash",
        )
        child = Note(
            path="AI/notes/child.md",
            title="Child",
            content="memory systems and retrieval.",
            frontmatter_json=json.dumps({"parent": "root"}),
            content_hash="child-hash",
        )
        session.add(root)
        session.add(child)
        session.commit()
    activity = ActivityService(engine)
    subscriber = activity.subscribe()
    service = QueryService(engine, activity_service=activity)
    try:
        result = service.query(QueryRequest(query="memory"))
        message = subscriber.get_nowait()

        assert result.selected_memories
        assert message.event_type == "query"
        assert message.payload["graph_path"] == [
            {
                "source": "AI/notes/root.md",
                "target": "AI/notes/child.md",
                "edge_type": "parent_of",
                "traversal_direction": "reverse",
            },
        ]
    finally:
        activity.close()
        engine.dispose()


def test_query_activity_records_reverse_contains_traversal(tmp_path: Path) -> None:
    """An inferred contains edge is traversed in reverse from the child."""
    engine = create_database(f"sqlite:///{tmp_path / 'contains.db'}")
    with CatalogSession(bind=engine) as session:
        index = Note(
            path="AI/notes/INDEX.md",
            title="Index",
            content="plain index details",
            frontmatter_json="{}",
            content_hash="index-hash",
        )
        child = Note(
            path="AI/notes/child.md",
            title="Child",
            content="memory systems and retrieval.",
            frontmatter_json="{}",
            content_hash="child-hash",
        )
        session.add(index)
        session.add(child)
        session.commit()
    activity = ActivityService(engine)
    subscriber = activity.subscribe()
    service = QueryService(engine, activity_service=activity)
    try:
        result = service.query(QueryRequest(query="memory"))
        message = subscriber.get_nowait()

        assert result.selected_memories
        assert message.event_type == "query"
        assert message.payload["graph_path"] == [
            {
                "source": "AI/notes/INDEX.md",
                "target": "AI/notes/child.md",
                "edge_type": "contains",
                "traversal_direction": "reverse",
            },
        ]
    finally:
        activity.close()
        engine.dispose()


def _query_catalog_with_reciprocal_links(tmp_path: Path):
    """Seed the *source* side of a reciprocal links_to pair.

    Both notes link to each other, so the projection holds two same-type
    edges: ``alpha -> root`` and ``root -> alpha``.  Only ``alpha`` matches
    ``memory`` lexically, so activation reaches ``root`` through the pair.
    """
    engine = create_database(f"sqlite:///{tmp_path / 'reciprocal.db'}")
    with CatalogSession(bind=engine) as session:
        root = Note(
            path="AI/notes/root.md",
            title="Root",
            content="plain root details [[alpha]]",
            frontmatter_json="{}",
            content_hash="root-hash",
        )
        alpha = Note(
            path="AI/notes/alpha.md",
            title="Alpha",
            content="memory systems and retrieval. [[root]]",
            frontmatter_json="{}",
            content_hash="alpha-hash",
        )
        root.links.append(
            Link(
                raw="[[alpha]]",
                normalized_target="AI/notes/alpha.md",
                explicit=True,
                resolution_status="resolved",
            )
        )
        alpha.links.append(
            Link(
                raw="[[root]]",
                normalized_target="AI/notes/root.md",
                explicit=True,
                resolution_status="resolved",
            )
        )
        session.add(root)
        session.add(alpha)
        session.commit()
    return engine


def test_query_activity_uses_intrinsic_provenance_for_reciprocal_links(
    tmp_path: Path,
) -> None:
    """Query activity reports the edge activation actually selected.

    Both reciprocal edges carry equal weight, so the tie resolves to the
    *forward* traversal (``spread_activation`` enumerates outgoing edges
    first and only switches on a strictly higher score).  The activity
    payload and the excluded node must reflect that selected edge — the
    intrinsic provenance, not a projection re-lookup.
    """
    engine = _query_catalog_with_reciprocal_links(tmp_path)
    activity = ActivityService(engine)
    subscriber = activity.subscribe()
    service = QueryService(
        engine,
        activity_service=activity,
        retrieval_settings=QuerySettings(context_token_budget=12),
    )
    try:
        result = service.query(QueryRequest(query="memory"))

        # alpha fits the budget (10 estimated tokens); root does not.
        assert [memory.path for memory in result.selected_memories] == [
            "AI/notes/alpha.md"
        ]
        excluded = {node.path: node for node in result.excluded_nodes}
        root = excluded["AI/notes/root.md"]
        assert root.via_path == "AI/notes/alpha.md"
        assert root.edge_type == "links_to"
        # The selected route is the forward traversal of alpha -> root.
        assert root.edge_source == "AI/notes/alpha.md"
        assert root.edge_target == "AI/notes/root.md"
        assert root.traversal_direction == "forward"

        message = subscriber.get_nowait()
        assert message.payload["graph_path"] == [
            {
                "source": "AI/notes/alpha.md",
                "target": "AI/notes/root.md",
                "edge_type": "links_to",
                "traversal_direction": "forward",
            },
        ]
    finally:
        activity.close()
        engine.dispose()


def test_query_activity_preserves_edge_provenance_on_excluded_nodes(
    tmp_path: Path,
) -> None:
    """Structural edge provenance attached during activation survives in QueryResult.

    A tight context budget excludes the reverse-reached node from the
    selection, and the excluded ``ActivatedNode`` must still carry the
    structural source/target and traversal direction.
    """
    engine = _query_catalog_with_reverse_links(tmp_path)
    activity = ActivityService(engine)
    service = QueryService(
        engine,
        activity_service=activity,
        retrieval_settings=QuerySettings(context_token_budget=10),
    )
    try:
        result = service.query(QueryRequest(query="memory"))

        assert [memory.path for memory in result.selected_memories] == [
            "AI/notes/alpha.md"
        ]
        excluded = {node.path: node for node in result.excluded_nodes}
        root = excluded["AI/notes/root.md"]
        assert root.via_path == "AI/notes/alpha.md"
        assert root.edge_type == "links_to"
        assert root.edge_source == "AI/notes/root.md"
        assert root.edge_target == "AI/notes/alpha.md"
        assert root.traversal_direction == "reverse"
    finally:
        activity.close()
        engine.dispose()


def _write_delta_vault(vault: Path) -> None:
    ai = vault / "AI"
    ai.mkdir(parents=True)
    (ai / "a.md").write_text("# A\n\nalpha content\n", encoding="utf-8")
    (ai / "b.md").write_text("# B\n\nbeta content\n", encoding="utf-8")


def test_scan_unchanged_rescan_records_no_graph_changes(tmp_path: Path) -> None:
    """A rescan with no vault changes reports an empty graph delta."""
    vault = tmp_path / "vault"
    _write_scan_vault(vault)
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    activity = ActivityService(engine)
    subscriber = activity.subscribe()
    service = ScanService(
        VaultBoundary(Settings(vault_path=vault, index_root=PurePosixPath("AI"))),
        engine,
        activity_service=activity,
    )
    try:
        first = service.full_scan()
        first_message = subscriber.get_nowait()
        assert first_message.payload["graph_refs"] == sorted(first.indexed_paths)

        second = service.full_scan()
        second_message = subscriber.get_nowait()

        assert second.files_indexed == first.files_indexed
        assert second.indexed_paths == first.indexed_paths
        assert second.added_paths == ()
        assert second.changed_paths == ()
        assert second.deleted_paths == ()
        assert second_message.payload["added_paths"] == []
        assert second_message.payload["changed_paths"] == []
        assert second_message.payload["deleted_paths"] == []
        assert second_message.payload["topology_paths"] == []
        assert second_message.payload["graph_refs"] == []

        scan_events = [
            event for event in activity.history() if event.event_type == "scan"
        ]
        assert len(scan_events) == 2
        assert scan_events[1].payload["topology_paths"] == []
        assert scan_events[1].payload["graph_refs"] == []
    finally:
        activity.close()
        engine.dispose()


def test_scan_rescan_records_added_changed_deleted_paths(tmp_path: Path) -> None:
    """A rescan reports exactly the paths that were added, changed, deleted."""
    vault = tmp_path / "vault"
    _write_delta_vault(vault)
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    activity = ActivityService(engine)
    subscriber = activity.subscribe()
    service = ScanService(
        VaultBoundary(Settings(vault_path=vault, index_root=PurePosixPath("AI"))),
        engine,
        activity_service=activity,
    )
    try:
        first = service.full_scan()
        subscriber.get_nowait()
        a_path = next(path for path in first.indexed_paths if path.endswith("a.md"))
        b_path = next(path for path in first.indexed_paths if path.endswith("b.md"))

        (vault / "AI" / "a.md").write_text(
            "# A\n\nalpha content, revised\n", encoding="utf-8"
        )
        (vault / "AI" / "b.md").unlink()
        (vault / "AI" / "c.md").write_text("# C\n\ngamma content\n", encoding="utf-8")

        second = service.full_scan()
        message = subscriber.get_nowait()
        c_path = next(path for path in second.indexed_paths if path.endswith("c.md"))

        assert second.added_paths == (c_path,)
        assert second.changed_paths == (a_path,)
        assert second.deleted_paths == (b_path,)
        assert message.payload["added_paths"] == [c_path]
        assert message.payload["changed_paths"] == [a_path]
        assert message.payload["deleted_paths"] == [b_path]
        # No links or indexes exist in this vault: the topology is empty in
        # both scans, so the topology delta must contribute nothing.
        assert message.payload["topology_paths"] == []
        assert message.payload["graph_refs"] == sorted([a_path, b_path, c_path])
    finally:
        activity.close()
        engine.dispose()


def test_scan_rescan_includes_topology_affected_source_in_graph_refs(
    tmp_path: Path,
) -> None:
    """Deleting a link target re-exposes the unchanged source in graph_refs.

    ``source.md`` is content-unchanged, but its resolved-link adjacency
    changes (the wikilink now dangles), so the explicit topology delta must
    pull it back into the scan's graph refs.
    """
    vault = tmp_path / "vault"
    ai = vault / "AI"
    ai.mkdir(parents=True)
    (ai / "source.md").write_text("# Source\n\n[[target]]\n", encoding="utf-8")
    (ai / "target.md").write_text("# Target\n\nplain target\n", encoding="utf-8")
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    activity = ActivityService(engine)
    subscriber = activity.subscribe()
    service = ScanService(
        VaultBoundary(Settings(vault_path=vault, index_root=PurePosixPath("AI"))),
        engine,
        activity_service=activity,
    )
    try:
        first = service.full_scan()
        first_message = subscriber.get_nowait()
        source_path = next(
            path for path in first.indexed_paths if path.endswith("source.md")
        )
        target_path = next(
            path for path in first.indexed_paths if path.endswith("target.md")
        )
        assert first_message.payload["graph_refs"] == sorted([source_path, target_path])

        (vault / "AI" / "target.md").unlink()
        second = service.full_scan()
        message = subscriber.get_nowait()

        assert second.added_paths == ()
        assert second.changed_paths == ()
        assert second.deleted_paths == (target_path,)
        assert message.payload["added_paths"] == []
        assert message.payload["changed_paths"] == []
        assert message.payload["deleted_paths"] == [target_path]
        # The dangling edge (source -> target) was removed from the
        # projection: both endpoints are topology-touched.
        assert message.payload["topology_paths"] == sorted([source_path, target_path])
        # The unchanged source must still appear in the graph refs.
        assert message.payload["graph_refs"] == sorted([source_path, target_path])

        scan_events = [
            event for event in activity.history() if event.event_type == "scan"
        ]
        assert scan_events[1].payload["graph_refs"] == sorted(
            [source_path, target_path]
        )
    finally:
        activity.close()
        engine.dispose()


def test_scan_rescan_includes_topology_affected_index_in_graph_refs(
    tmp_path: Path,
) -> None:
    """Adding a sibling file re-exposes the unchanged containing index.

    ``AI/INDEX.md`` is content-unchanged, but the inferred ``contains``
    adjacency grows when a sibling file appears, so the index must appear in
    the scan's graph refs through the explicit topology delta.
    """
    vault = tmp_path / "vault"
    ai = vault / "AI"
    ai.mkdir(parents=True)
    (ai / "INDEX.md").write_text("# Index\n", encoding="utf-8")
    (ai / "child.md").write_text("# Child\n\nplain child\n", encoding="utf-8")
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    activity = ActivityService(engine)
    subscriber = activity.subscribe()
    service = ScanService(
        VaultBoundary(Settings(vault_path=vault, index_root=PurePosixPath("AI"))),
        engine,
        activity_service=activity,
    )
    try:
        first = service.full_scan()
        subscriber.get_nowait()
        index_path = next(
            path for path in first.indexed_paths if path.endswith("INDEX.md")
        )
        child_path = next(
            path for path in first.indexed_paths if path.endswith("child.md")
        )

        (vault / "AI" / "sibling.md").write_text(
            "# Sibling\n\nplain sibling\n", encoding="utf-8"
        )
        second = service.full_scan()
        message = subscriber.get_nowait()
        sibling_path = next(
            path for path in second.indexed_paths if path.endswith("sibling.md")
        )

        assert second.added_paths == (sibling_path,)
        assert second.changed_paths == ()
        assert second.deleted_paths == ()
        assert message.payload["added_paths"] == [sibling_path]
        assert message.payload["changed_paths"] == []
        assert message.payload["deleted_paths"] == []
        # The new contains edge (INDEX -> sibling) is the only topology
        # change: its endpoints are the index and the new sibling.
        assert message.payload["topology_paths"] == sorted([index_path, sibling_path])
        # The unchanged index must appear in the graph refs.
        assert message.payload["graph_refs"] == sorted([index_path, sibling_path])
        # The untouched child is not part of the topology delta.
        assert child_path not in message.payload["graph_refs"]
    finally:
        activity.close()
        engine.dispose()
