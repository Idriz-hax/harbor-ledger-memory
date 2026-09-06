"""Tests for adaptive weight adjustment service."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import (
    AdaptiveEdge,
    Base,
    ContextSelection,
    QueryTrace,
)
from harbor_ledger_memory.config import MemorySettings
from harbor_ledger_memory.services.adaptive import (
    AdaptiveService,
    FeedbackValidationError,
)


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_database(f"sqlite:///{tmp_path / 'memory.db'}")
    Base.metadata.create_all(engine)
    session = CatalogSession(bind=engine)
    yield session
    session.close()


@pytest.fixture
def adaptive_service(session: Session) -> AdaptiveService:
    return AdaptiveService(session, MemorySettings())


def test_implicit_adjustment_creates_edge(adaptive_service: AdaptiveService) -> None:
    """Apply implicit adjustment for co-selected paths, verify edge is stored."""
    paths = ["AI/source.md", "AI/target.md", "AI/other.md"]
    adjustments = adaptive_service.apply_implicit("test-trace", paths)
    adaptive_service._session.commit()

    assert adjustments == 3  # 3 pairs: (source,target), (source,other), (target,other)
    deltas = adaptive_service.get_deltas("AI/source.md")
    assert abs(deltas["AI/target.md"] - 0.05) < 0.001


def test_explicit_positive_feedback(adaptive_service: AdaptiveService) -> None:
    """Apply explicit positive feedback and see the delta increase."""
    adjustments = adaptive_service.apply_explicit(["AI/relevant.md"], None)
    adaptive_service._session.commit()

    assert adjustments == 1
    deltas = adaptive_service.get_deltas("__query__")
    assert abs(deltas["AI/relevant.md"] - 0.15) < 0.001


def test_explicit_negative_feedback(adaptive_service: AdaptiveService) -> None:
    """Apply explicit negative feedback and see the delta decrease."""
    adjustments = adaptive_service.apply_explicit(None, ["AI/irrelevant.md"])
    adaptive_service._session.commit()

    assert adjustments == 1
    deltas = adaptive_service.get_deltas("__query__")
    assert abs(deltas["AI/irrelevant.md"] - (-0.10)) < 0.001


def test_query_deltas_aggregate_mixed_explicit_feedback_deterministically(
    adaptive_service: AdaptiveService,
) -> None:
    """Positive and negative rows for one target are combined and clamped."""
    adaptive_service.apply_explicit(["AI/target.md"], None)
    adaptive_service.apply_explicit(None, ["AI/target.md"])
    adaptive_service._session.add(
        AdaptiveEdge(
            source_path="__query__",
            target_path="AI/other.md",
            edge_type="explicit_negative",
            weight_delta=-2.0,
            last_updated="2026-01-01T00:00:00+00:00",
        )
    )
    adaptive_service._session.commit()

    assert adaptive_service.get_deltas("__query__") == {
        "AI/other.md": -0.9,
        "AI/target.md": pytest.approx(0.05),
    }


@pytest.fixture
def selected_trace(session: Session) -> str:
    trace_id = "trace-selected"
    trace = QueryTrace(
        trace_uuid=trace_id,
        query="test",
        retrieval_settings="{}",
        status="completed",
    )
    session.add(trace)
    session.flush()
    session.add(
        ContextSelection(
            trace_id=trace.id,
            path="Public/a.md",
            rank=1,
            retrieval_score=1.0,
            activation_score=1.0,
            reasons="[]",
            excerpt="a",
            estimated_tokens=1,
        )
    )
    session.commit()
    return trace_id


def test_trace_feedback_rejects_unknown_trace(
    adaptive_service: AdaptiveService,
) -> None:
    with pytest.raises(FeedbackValidationError):
        adaptive_service.apply_trace_feedback(
            "missing", ["Public/a.md"], None, lambda _: True
        )


def test_trace_feedback_rejects_unselected_path(
    adaptive_service: AdaptiveService, selected_trace: str
) -> None:
    with pytest.raises(FeedbackValidationError):
        adaptive_service.apply_trace_feedback(
            selected_trace, ["Public/not-selected.md"], None, lambda _: True
        )


def test_trace_feedback_rejects_conflicting_path(
    adaptive_service: AdaptiveService, selected_trace: str
) -> None:
    with pytest.raises(FeedbackValidationError):
        adaptive_service.apply_trace_feedback(
            selected_trace, ["Public/a.md"], ["Public/a.md"], lambda _: True
        )


def test_trace_feedback_rejects_empty_input(
    adaptive_service: AdaptiveService, selected_trace: str
) -> None:
    with pytest.raises(FeedbackValidationError, match="at least one"):
        adaptive_service.apply_trace_feedback(
            selected_trace, None, None, lambda _: True
        )


def test_trace_feedback_rejects_duplicate_paths_in_one_list(
    adaptive_service: AdaptiveService, selected_trace: str
) -> None:
    with pytest.raises(FeedbackValidationError, match="duplicated"):
        adaptive_service.apply_trace_feedback(
            selected_trace, ["Public/a.md", "Public/a.md"], None, lambda _: True
        )


def test_trace_feedback_rejects_path_that_is_no_longer_admitted(
    adaptive_service: AdaptiveService, selected_trace: str
) -> None:
    with pytest.raises(FeedbackValidationError, match="admitted"):
        adaptive_service.apply_trace_feedback(
            selected_trace, ["Public/a.md"], None, lambda _: False
        )
