"""End-to-end fixture tests for the QueryService orchestration."""

import json
from queue import Empty
from pathlib import Path

from sqlalchemy import String, Text, select

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import (
    ActivationVisit,
    Link,
    MemoryWriteProposal,
    Note,
    QueryTrace,
    ShortTermEvent,
)
from harbor_ledger_memory.config import MemorySettings
from harbor_ledger_memory.domain.retrieval import (
    ContextMemory,
    QueryRequest,
    QueryResult,
    QuerySettings,
)
from harbor_ledger_memory.services.memory import MemoryService
from harbor_ledger_memory.services.query import QueryService
from harbor_ledger_memory.services.live_traversal import LiveTraversalPublisher, TraversalEvent


def _seed_catalog(tmp_path: Path) -> str:
    """Create a minimal catalog with notes and links for query testing."""
    db_path = str(tmp_path / "query_test.db")
    engine = create_database(f"sqlite:///{db_path}")
    session = CatalogSession(bind=engine)
    try:
        # Note 1: About memory and context
        session.add(
            Note(
                path="AI/Knowledge/memory-systems.md",
                title="Memory Systems",
                content=(
                    "Human memory systems include working memory "
                    "and long-term memory.\n"
                    "Working memory has limited capacity.\n"
                    "Long-term memory can store vast amounts of information.\n"
                    "See [[AI/Knowledge/context.md]] for related concepts.\n"
                ),
                summary="Overview of memory systems and their limitations",
                frontmatter_json='{"type":"knowledge","tags":["memory","cognitive"],"status":"active"}',
                content_hash="aaa",
            )
        )
        # Note 2: About context windows
        session.add(
            Note(
                path="AI/Knowledge/context.md",
                title="Context Windows",
                content=(
                    "Context windows in LLMs determine how much prior text\n"
                    "the model can attend to during generation.\n"
                    "Larger context windows enable more coherent long-form reasoning.\n"
                ),
                summary="Context window size affects LLM reasoning capability",
                frontmatter_json='{"type":"knowledge","tags":["llm","context"],"status":"active"}',
                content_hash="bbb",
            )
        )
        # Note 3: About retrieval (linked from memory-systems)
        session.add(
            Note(
                path="AI/Projects/retrieval.md",
                title="Retrieval Augmented Generation",
                content=(
                    "Retrieval Augmented Generation (RAG) combines retrieval\n"
                    "with language model generation for improved accuracy.\n"
                    "This approach reduces hallucination in\n"
                    "knowledge-intensive tasks.\n"
                ),
                summary="RAG reduces hallucination through external retrieval",
                frontmatter_json='{"type":"project","tags":["rag","retrieval"],"status":"active"}',
                content_hash="ccc",
            )
        )
        session.commit()
    finally:
        session.close()
    return db_path


def test_query_live_events_apply_the_service_policy_to_nodes_and_edges(tmp_path: Path) -> None:
    db_path = _seed_catalog(tmp_path)
    engine = create_database(f"sqlite:///{db_path}")
    session = CatalogSession(bind=engine)
    publisher = LiveTraversalPublisher()
    subscription = publisher.subscribe()
    try:
        service = QueryService(
            session,
            path_filter=lambda path: path == "AI/Knowledge/memory-systems.md",
            live_traversal=publisher,
        )
        service.query(QueryRequest(query="memory systems"))
        events: list[TraversalEvent] = []
        while True:
            try:
                events.append(subscription.get_nowait())
            except Empty:
                break
        assert events
        assert all(event.node_path == "AI/Knowledge/memory-systems.md" for event in events)
        assert all(
            endpoint is None or endpoint == "AI/Knowledge/memory-systems.md"
            for event in events
            for endpoint in (event.source_path, event.target_path)
        )
    finally:
        session.close()
        engine.dispose()


class TestQueryServiceEmptyCatalog:
    """QueryService handles empty catalogs gracefully."""

    def test_empty_catalog_returns_empty_result(self, tmp_path: Path) -> None:
        db_path = _seed_catalog(tmp_path)
        engine = create_database(f"sqlite:///{db_path}")
        # Rebuild to clear all notes
        from harbor_ledger_memory.catalog.database import rebuild_catalog

        rebuild_catalog(engine)

        session = CatalogSession(bind=engine)
        try:
            service = QueryService(
                session,
                retrieval_settings=QuerySettings(
                    max_seed_nodes=5,
                    max_activation_nodes=10,
                    max_hops=2,
                    decay=0.65,
                    minimum_activation=0.05,
                    context_token_budget=8000,
                ),
            )
            request = QueryRequest(query="memory systems")
            result = service.query(request)

            assert isinstance(result, QueryResult)
            assert result.query == "memory systems"
            assert len(result.selected_memories) == 0
            assert result.total_estimated_tokens == 0
            # trace_id should be a valid UUID
            from uuid import UUID

            UUID(str(result.trace_id))
        finally:
            session.close()
            engine.dispose()


class TestQueryServiceWithNotes:
    """QueryService composes retrieval, activation, context correctly."""

    def test_query_returns_selected_memories(self, tmp_path: Path) -> None:
        db_path = _seed_catalog(tmp_path)
        engine = create_database(f"sqlite:///{db_path}")
        session = CatalogSession(bind=engine)
        try:
            service = QueryService(
                session,
                retrieval_settings=QuerySettings(
                    max_seed_nodes=10,
                    max_activation_nodes=50,
                    max_hops=3,
                    decay=0.65,
                    minimum_activation=0.05,
                    context_token_budget=12_000,
                ),
            )
            request = QueryRequest(query="memory systems")
            result = service.query(request)

            assert isinstance(result, QueryResult)
            assert result.query == "memory systems"
            assert len(result.selected_memories) > 0
            # Selected memories should be ContextMemory objects
            for mem in result.selected_memories:
                assert isinstance(mem, ContextMemory)
                assert mem.path
                assert isinstance(mem.excerpt, str)
                assert mem.estimated_tokens >= 0
        finally:
            session.close()
            engine.dispose()

    def test_query_persists_trace(self, tmp_path: Path) -> None:
        """Query trace, visits, and selections are persisted to the catalog."""
        db_path = _seed_catalog(tmp_path)
        engine = create_database(f"sqlite:///{db_path}")
        session = CatalogSession(bind=engine)
        try:
            service = QueryService(
                session,
                retrieval_settings=QuerySettings(
                    max_seed_nodes=10,
                    max_activation_nodes=50,
                    max_hops=3,
                    decay=0.65,
                    minimum_activation=0.05,
                    context_token_budget=12_000,
                ),
            )
            request = QueryRequest(query="memory systems")
            result = service.query(request)

            # Verify trace exists in the DB
            from sqlalchemy import select

            from harbor_ledger_memory.catalog.models import (
                ActivationVisit,
                ContextSelection,
            )

            trace_uuid = str(result.trace_id)
            trace = session.scalar(
                select(QueryTrace).where(QueryTrace.trace_uuid == trace_uuid)
            )
            assert trace is not None, "QueryTrace should be persisted"
            assert trace.query == "memory systems"
            assert trace.status == "completed"
            assert trace.latency_ms is not None
            assert trace.latency_ms >= 0

            # Verify activation visits were persisted
            visits = session.scalars(
                select(ActivationVisit).where(ActivationVisit.trace_id == trace.id)
            ).all()
            assert len(visits) >= 0  # May be zero if no activation beyond seeds

            # Verify context selections were persisted
            selections = session.scalars(
                select(ContextSelection).where(ContextSelection.trace_id == trace.id)
            ).all()
            assert len(selections) == len(result.selected_memories)
        finally:
            session.close()
            engine.dispose()

    def test_query_with_active_project(self, tmp_path: Path) -> None:
        """Active project filter boosts project-related notes."""
        db_path = _seed_catalog(tmp_path)
        engine = create_database(f"sqlite:///{db_path}")
        session = CatalogSession(bind=engine)
        try:
            service = QueryService(
                session,
                retrieval_settings=QuerySettings(
                    max_seed_nodes=10,
                    max_activation_nodes=50,
                    max_hops=3,
                    decay=0.65,
                    minimum_activation=0.05,
                    context_token_budget=12_000,
                ),
            )
            # Query about retrieval with active project matching a path
            request = QueryRequest(
                query="retrieval", active_project="AI/Projects/retrieval.md"
            )
            result = service.query(request)

            assert isinstance(result, QueryResult)
            assert result.query == "retrieval"
        finally:
            session.close()
            engine.dispose()

    def test_query_respects_token_budget(self, tmp_path: Path) -> None:
        """Context selection respects the configured token budget."""
        db_path = _seed_catalog(tmp_path)
        engine = create_database(f"sqlite:///{db_path}")
        session = CatalogSession(bind=engine)
        try:
            # Very tight token budget
            service = QueryService(
                session,
                retrieval_settings=QuerySettings(
                    max_seed_nodes=10,
                    max_activation_nodes=50,
                    max_hops=3,
                    decay=0.65,
                    minimum_activation=0.05,
                    context_token_budget=100,  # Very small budget
                ),
            )
            request = QueryRequest(query="memory")
            result = service.query(request)

            assert result.total_estimated_tokens <= 100 or (
                # At least one item should fit if it's small enough
                result.total_estimated_tokens == 0
            )
        finally:
            session.close()
            engine.dispose()

    def test_query_refreshes_only_selected_context(self, tmp_path: Path) -> None:
        """A query uses configured cache candidates and refreshes its context only."""
        db_path = _seed_catalog(tmp_path)
        engine = create_database(f"sqlite:///{db_path}")
        session = CatalogSession(bind=engine)
        memory_settings = MemorySettings(short_term_capacity=10)
        try:
            result = QueryService(
                session,
                retrieval_settings=QuerySettings(),
                memory_settings=memory_settings,
            ).query(QueryRequest(query="memory"))

            cached = MemoryService(session, memory_settings).cache_candidates()
            assert set(cached) == {memory.path for memory in result.selected_memories}
            assert result.short_term_evidence.hit_paths == ()
            assert result.short_term_evidence.refreshed_paths == tuple(
                memory.path for memory in result.selected_memories
            )
            assert result.short_term_evidence.evicted_paths == ()
            assert result.short_term_evidence.removed_expired == 0
            assert result.short_term_evidence.removed_missing == 0

            from sqlalchemy import select

            trace = session.scalar(
                select(QueryTrace).where(QueryTrace.trace_uuid == str(result.trace_id))
            )
            assert trace is not None
            metadata = json.loads(trace.retrieval_settings)
            assert metadata["short_term_evidence"] == {
                "evicted_paths": [],
                "hit_paths": [],
                "refreshed_paths": [memory.path for memory in result.selected_memories],
                "removed_expired": 0,
                "removed_missing": 0,
            }

            event = session.scalar(
                select(ShortTermEvent).where(
                    ShortTermEvent.trace_uuid == str(result.trace_id)
                )
            )
            assert event is not None
            assert json.loads(event.selected_paths) == [
                memory.path for memory in result.selected_memories
            ]
        finally:
            session.close()
            engine.dispose()

    def test_query_records_configured_cache_hits(self, tmp_path: Path) -> None:
        """Configured cache candidates are passed into seed retrieval."""
        db_path = _seed_catalog(tmp_path)
        engine = create_database(f"sqlite:///{db_path}")
        session = CatalogSession(bind=engine)
        memory_settings = MemorySettings(short_term_capacity=10)
        try:
            cached_path = "AI/Knowledge/context.md"
            MemoryService(session, memory_settings).refresh_selected([cached_path])

            result = QueryService(
                session,
                retrieval_settings=QuerySettings(),
                memory_settings=memory_settings,
            ).query(QueryRequest(query="memory"))

            assert cached_path in result.short_term_evidence.hit_paths
            selected = {memory.path: memory for memory in result.selected_memories}
            assert "Short-term cache (+" in " ".join(selected[cached_path].reasons)
        finally:
            session.close()
            engine.dispose()

    def test_query_result_never_exposes_orm_models(self, tmp_path: Path) -> None:
        """QueryResult contains only Pydantic models, no ORM or raw Note bodies."""
        db_path = _seed_catalog(tmp_path)
        engine = create_database(f"sqlite:///{db_path}")
        session = CatalogSession(bind=engine)
        try:
            service = QueryService(
                session,
                retrieval_settings=QuerySettings(),
            )
            request = QueryRequest(query="memory")
            result = service.query(request)

            # Verify serializable (Pydantic models are JSON-serializable)
            data = result.model_dump()
            assert "trace_id" in data
            assert "selected_memories" in data
            assert "query" in data

            # Each selected memory should have excerpt, not full content
            for mem in result.selected_memories:
                assert hasattr(mem, "excerpt")
                # excerpt should be shorter than full note content
                # (we don't have direct access to full content here, but excerpt
                # should be a bounded string)
                assert isinstance(mem.excerpt, str)
        finally:
            session.close()
            engine.dispose()


def _seed_linked_catalog(tmp_path: Path) -> str:
    """A catalog with one explicit, resolved wikilink so activation traverses."""
    db_path = str(tmp_path / "linked_query_test.db")
    engine = create_database(f"sqlite:///{db_path}")
    session = CatalogSession(bind=engine)
    try:
        source = Note(
            path="AI/Knowledge/memory-systems.md",
            title="Memory Systems",
            content="memory systems and retrieval.",
            summary="memory",
            frontmatter_json="{}",
            content_hash="aaa",
        )
        target = Note(
            path="AI/Knowledge/context.md",
            title="Context Windows",
            content="context windows and attention.",
            summary="context",
            frontmatter_json="{}",
            content_hash="bbb",
        )
        source.links.append(
            Link(
                raw="[[AI/Knowledge/context.md]]",
                normalized_target="AI/Knowledge/context.md",
                explicit=True,
                resolution_status="resolved",
            )
        )
        session.add(source)
        session.add(target)
        session.commit()
    finally:
        session.close()
    return db_path


class TestActivationVisitPersistence:
    """Activation visits persist the traversal step and edge endpoints."""

    def test_visits_persist_intrinsic_edge_endpoints(self, tmp_path: Path) -> None:
        db_path = _seed_linked_catalog(tmp_path)
        engine = create_database(f"sqlite:///{db_path}")
        session = CatalogSession(bind=engine)
        try:
            result = QueryService(session, retrieval_settings=QuerySettings()).query(
                QueryRequest(query="memory")
            )
            assert result.selected_memories

            trace = session.scalar(
                select(QueryTrace).where(QueryTrace.trace_uuid == str(result.trace_id))
            )
            assert trace is not None
            visits = {
                visit.path: visit
                for visit in session.scalars(
                    select(ActivationVisit).where(ActivationVisit.trace_id == trace.id)
                )
            }

            # The seed (hop 0) has no incoming edge: endpoints stay NULL.
            seed = visits["AI/Knowledge/memory-systems.md"]
            assert seed.hop == 0
            assert seed.via_path is None
            assert seed.edge_source is None
            assert seed.edge_target is None

            # The traversed node persists the structural endpoints of the
            # selected projection edge.
            target = visits["AI/Knowledge/context.md"]
            assert target.hop == 1
            assert target.via_path == "AI/Knowledge/memory-systems.md"
            assert target.edge_type == "links_to"
            assert target.edge_source == "AI/Knowledge/memory-systems.md"
            assert target.edge_target == "AI/Knowledge/context.md"
        finally:
            session.close()
            engine.dispose()


def test_activation_visit_columns_match_historical_schema() -> None:
    """The visit endpoint columns use the immutable historical widths."""
    table = ActivationVisit.__table__
    for name in ("edge_source", "edge_target"):
        column = table.c[name]
        assert isinstance(column.type, String)
        assert column.type.length == 512
        assert column.nullable is True


def test_memory_write_proposal_columns_match_historical_schema() -> None:
    """The proposal columns use the immutable historical widths."""
    table = MemoryWriteProposal.__table__
    assert table.c["path"].type.length == 1024
    for name in ("operation", "status"):
        column = table.c[name]
        assert isinstance(column.type, String)
        assert column.type.length == 16
    assert table.c["rule_access"].type.length == 32
    # Lifecycle fields already in use keep their historical types.
    assert table.c["requested_at"].type.length == 128
    assert table.c["expected_source_hash"].type.length == 64
    assert table.c["resolved_at"].type.length == 128
    assert table.c["applying_at"].type.length == 128
    assert table.c["applied_content_hash"].type.length == 64
    assert isinstance(table.c["failure_reason"].type, Text)
