"""Tests for short-term memory persistence layer."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import (
    Base,
    Note,
    ShortTermEntry,
    ShortTermEvent,
)
from harbor_ledger_memory.config import MemorySettings
from harbor_ledger_memory.services.memory import MemoryService


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_database(f"sqlite:///{tmp_path / 'memory.db'}")
    Base.metadata.create_all(engine)
    session = CatalogSession(bind=engine)
    yield session
    session.close()


@pytest.fixture
def memory_settings() -> MemorySettings:
    return MemorySettings()


@pytest.fixture
def memory_service(session: Session, memory_settings: MemorySettings) -> MemoryService:
    return MemoryService(session, memory_settings)


def test_short_term_event_round_trip(session: Session) -> None:
    event = ShortTermEvent(
        trace_uuid="test-trace-uuid",
        query_text="test query",
        selected_paths='["AI/test.md"]',
        created_at="2026-08-03T12:00:00Z",
        expires_at="2026-08-04T12:00:00Z",
    )
    session.add(event)
    session.flush()
    session.refresh(event)

    assert event.id is not None
    assert event.trace_uuid == "test-trace-uuid"
    assert event.query_text == "test query"
    assert event.selected_paths == '["AI/test.md"]'
    assert event.created_at == "2026-08-03T12:00:00Z"
    assert event.expires_at == "2026-08-04T12:00:00Z"
    session.rollback()


def test_short_term_entry_round_trip(session: Session) -> None:
    session.add(Note(path="AI/current.md"))
    session.flush()
    entry = ShortTermEntry(
        path="AI/current.md",
        last_selected_at="2026-08-06T12:00:00+00:00",
        selection_count=1,
    )
    session.add(entry)
    session.flush()
    session.refresh(entry)

    assert entry.path == "AI/current.md"
    assert entry.last_selected_at == "2026-08-06T12:00:00+00:00"
    assert entry.selection_count == 1
    session.rollback()


def test_refreshes_selected_paths_only(memory_service: MemoryService) -> None:
    for path in ("AI/one.md", "AI/two.md"):
        memory_service._session.add(Note(path=path))
    memory_service._session.flush()

    refresh = memory_service.refresh_selected(["AI/one.md", "AI/two.md", "AI/one.md"])

    assert set(memory_service.cache_candidates()) == {"AI/one.md", "AI/two.md"}
    assert refresh.refreshed_paths == ("AI/one.md", "AI/two.md")
    assert memory_service._session.get(ShortTermEntry, "AI/one.md").selection_count == 1


def test_lru_evicts_oldest_entry(memory_service: MemoryService) -> None:
    memory_service._settings = MemorySettings(short_term_capacity=2)
    for path in ("AI/one.md", "AI/two.md", "AI/three.md"):
        memory_service._session.add(Note(path=path))
    memory_service._session.flush()

    for path in ("AI/one.md", "AI/two.md", "AI/three.md"):
        refresh = memory_service.refresh_selected([path])

    assert set(memory_service.cache_candidates()) == {"AI/two.md", "AI/three.md"}
    assert refresh.evicted_paths == ("AI/one.md",)


def test_cleanup_cache_removes_expired_entries(memory_service: MemoryService) -> None:
    from datetime import UTC, datetime, timedelta

    memory_service._settings = MemorySettings(short_term_ttl_days=1)
    memory_service._session.add(Note(path="AI/expired.md"))
    memory_service._session.add(
        ShortTermEntry(
            path="AI/expired.md",
            last_selected_at=(
                datetime.now(UTC) - timedelta(days=1, seconds=1)
            ).isoformat(),
            selection_count=1,
        )
    )
    memory_service._session.flush()

    assert memory_service.cleanup_cache() == (1, 0)
    assert memory_service.cache_candidates() == {}


def test_cleanup_cache_removes_entries_without_notes(
    memory_service: MemoryService,
) -> None:
    from datetime import UTC, datetime

    memory_service._session.commit()
    memory_service._session.connection().exec_driver_sql("PRAGMA foreign_keys = OFF")
    memory_service._session.add(
        ShortTermEntry(
            path="AI/missing.md",
            last_selected_at=datetime.now(UTC).isoformat(),
            selection_count=1,
        )
    )
    memory_service._session.commit()
    memory_service._session.connection().exec_driver_sql("PRAGMA foreign_keys = ON")

    assert memory_service.cleanup_cache() == (0, 1)
    assert memory_service.cache_candidates() == {}


def test_cache_candidates_decay_boost_within_bounds(
    memory_service: MemoryService,
) -> None:
    from datetime import UTC, datetime, timedelta

    memory_service._session.add(Note(path="AI/recent.md"))
    memory_service._session.add(
        ShortTermEntry(
            path="AI/recent.md",
            last_selected_at=(datetime.now(UTC) - timedelta(days=7)).isoformat(),
            selection_count=1,
        )
    )
    memory_service._session.flush()

    boost = memory_service.cache_candidates()["AI/recent.md"]

    assert 0.0 <= boost <= memory_service._settings.short_term_max_boost
    assert boost == pytest.approx(0.125, abs=0.001)


def test_cache_candidates_clamp_future_timestamp_boost(
    memory_service: MemoryService,
) -> None:
    from datetime import UTC, datetime, timedelta

    memory_service._session.add(Note(path="AI/future.md"))
    memory_service._session.add(
        ShortTermEntry(
            path="AI/future.md",
            last_selected_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            selection_count=1,
        )
    )
    memory_service._session.flush()

    boost = memory_service.cache_candidates()["AI/future.md"]

    assert boost == memory_service._settings.short_term_max_boost


def test_lru_uses_path_to_break_timestamp_ties(memory_service: MemoryService) -> None:
    from datetime import UTC, datetime, timedelta

    memory_service._settings = MemorySettings(short_term_capacity=2)
    for path in ("AI/a.md", "AI/b.md", "AI/c.md"):
        memory_service._session.add(Note(path=path))
    tied_timestamp = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    memory_service._session.add_all(
        [
            ShortTermEntry(
                path="AI/a.md",
                last_selected_at=tied_timestamp,
                selection_count=1,
            ),
            ShortTermEntry(
                path="AI/b.md",
                last_selected_at=tied_timestamp,
                selection_count=1,
            ),
        ]
    )
    memory_service._session.flush()

    refresh = memory_service.refresh_selected(["AI/c.md"])

    assert refresh.evicted_paths == ("AI/a.md",)
    assert set(memory_service.cache_candidates()) == {"AI/b.md", "AI/c.md"}


def test_query_history_does_not_feed_cache_candidates(
    memory_service: MemoryService,
) -> None:
    memory_service.record_query("test-trace", "test query", ["AI/history.md"])

    assert memory_service.cache_candidates() == {}


def test_adaptive_edge_round_trip(session: Session) -> None:
    from harbor_ledger_memory.catalog.models import AdaptiveEdge

    edge = AdaptiveEdge(
        source_path="AI/source.md",
        target_path="AI/target.md",
        edge_type="wikilink",
        weight_delta=0.05,
        last_updated="2026-08-03T12:00:00Z",
    )
    session.add(edge)
    session.flush()
    session.refresh(edge)

    assert edge.id is not None
    assert edge.source_path == "AI/source.md"
    assert edge.target_path == "AI/target.md"
    assert edge.edge_type == "wikilink"
    assert edge.weight_delta == 0.05
    assert edge.last_updated == "2026-08-03T12:00:00Z"
    session.rollback()


def test_record_and_query_recent_paths(memory_service: MemoryService) -> None:
    """Records a query event, verifies storage, then queries recent paths for boost."""
    memory_service.record_query("test-trace", "test query", ["AI/test.md"])
    memory_service._session.commit()

    recent = memory_service.recent_paths()
    assert "AI/test.md" in recent
    assert recent["AI/test.md"] == 0.20  # adaptive_boost * min(1, 3)


def test_recent_paths_aggregates_counts(memory_service: MemoryService) -> None:
    """Multiple queries for the same path increase the boost score."""
    memory_service.record_query("trace-1", "query 1", ["AI/test.md"])
    memory_service.record_query("trace-2", "query 2", ["AI/test.md"])
    memory_service.record_query("trace-3", "query 3", ["AI/test.md"])
    memory_service.record_query("trace-4", "query 4", ["AI/test.md"])
    memory_service._session.commit()

    recent = memory_service.recent_paths()
    assert abs(recent["AI/test.md"] - 0.60) < 0.001  # adaptive_boost * min(4, 3)


def test_cleanup_expired_events(memory_service: MemoryService) -> None:
    """Create an expired event, run cleanup, and prove it is deleted."""
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    memory_service.record_query("past-trace", "old query", ["AI/old.md"])
    # Manually expire the event
    event = (
        memory_service._session.query(ShortTermEvent)
        .filter_by(trace_uuid="past-trace")
        .first()
    )
    event.expires_at = past
    memory_service._session.commit()

    deleted = memory_service.cleanup_expired()
    assert deleted == 1

    remaining = (
        memory_service._session.query(ShortTermEvent)
        .filter_by(trace_uuid="past-trace")
        .first()
    )
    assert remaining is None
