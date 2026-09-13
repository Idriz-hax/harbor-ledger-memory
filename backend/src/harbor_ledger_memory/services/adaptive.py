"""Adaptive weight adjustment service for link learning."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.models import AdaptiveEdge
from harbor_ledger_memory.config import MemorySettings


class FeedbackValidationError(ValueError):
    """Raised when feedback does not match an admitted query trace."""


class AdaptiveService:
    """Manages adaptive edge weight adjustments."""

    def __init__(self, session: Session, settings: MemorySettings) -> None:
        self._session = session
        self._settings = settings

    def apply_implicit(self, trace_uuid: str, selected_paths: list[str]) -> int:
        """Apply implicit weight adjustments for co-selected path pairs.

        For each pair of selected paths, upsert an adaptive edge with +implicit_step.
        """
        adjustments = 0
        path_set = sorted(set(selected_paths))
        for i, source in enumerate(path_set):
            for target in path_set[i + 1 :]:
                adjustments += self._upsert_edge(
                    source, target, "co_selected", self._settings.implicit_step
                )
        return adjustments

    def apply_explicit(
        self,
        relevant_paths: list[str] | None,
        irrelevant_paths: list[str] | None,
    ) -> int:
        """Apply explicit feedback weight adjustments.

        For relevant paths, add +explicit_positive; for irrelevant,
        add -explicit_negative.
        """
        adjustments = 0
        if relevant_paths:
            for path in relevant_paths:
                adjustments += self._upsert_edge(
                    "__query__",
                    path,
                    "explicit_positive",
                    self._settings.explicit_positive,
                )
        if irrelevant_paths:
            for path in irrelevant_paths:
                adjustments += self._upsert_edge(
                    "__query__",
                    path,
                    "explicit_negative",
                    -self._settings.explicit_negative,
                )
        return adjustments

    def apply_trace_feedback(
        self,
        trace_uuid: str,
        relevant_paths: list[str] | None,
        irrelevant_paths: list[str] | None,
        path_filter: Callable[[str], bool],
    ) -> int:
        """Validate feedback against a trace, then apply it atomically."""
        from harbor_ledger_memory.catalog.models import QueryTrace

        trace = self._session.execute(
            select(QueryTrace).where(QueryTrace.trace_uuid == trace_uuid)
        ).scalar_one_or_none()
        if trace is None:
            raise FeedbackValidationError(f"Trace {trace_uuid} not found")

        relevant = relevant_paths or []
        irrelevant = irrelevant_paths or []
        supplied = relevant + irrelevant
        if not supplied:
            raise FeedbackValidationError("feedback must include at least one path")
        if any(not path for path in supplied):
            raise FeedbackValidationError("feedback paths must not be empty")
        if len(supplied) != len(set(supplied)):
            raise FeedbackValidationError("feedback paths must not be duplicated")
        if set(relevant) & set(irrelevant):
            raise FeedbackValidationError(
                "a path cannot be both relevant and irrelevant"
            )

        selected = {selection.path for selection in trace.selections}
        for path in supplied:
            if path not in selected:
                raise FeedbackValidationError(f"path was not selected by trace: {path}")
            if not path_filter(path):
                raise FeedbackValidationError(f"path is not admitted: {path}")

        return self.apply_explicit(relevant, irrelevant)

    def _upsert_edge(
        self, source: str, target: str, edge_type: str, delta: float
    ) -> int:
        """Upsert an adaptive edge with the given delta, clamping to bounds."""
        existing = self._session.execute(
            select(AdaptiveEdge).where(
                AdaptiveEdge.source_path == source,
                AdaptiveEdge.target_path == target,
                AdaptiveEdge.edge_type == edge_type,
            )
        ).scalar_one_or_none()

        if existing:
            new_delta = max(-0.9, min(1.0, existing.weight_delta + delta))
            existing.weight_delta = new_delta
            existing.last_updated = datetime.now(UTC).isoformat()
        else:
            new_delta = max(-0.9, min(1.0, delta))
            edge = AdaptiveEdge(
                source_path=source,
                target_path=target,
                edge_type=edge_type,
                weight_delta=new_delta,
                last_updated=datetime.now(UTC).isoformat(),
            )
            self._session.add(edge)
        return 1

    def get_deltas(self, source_path: str) -> dict[str, float]:
        """Get adaptive weight deltas for a source path.

        Returns: dict mapping target_path -> weight_delta
        """
        edges = (
            self._session.execute(
                select(AdaptiveEdge)
                .where(AdaptiveEdge.source_path == source_path)
                .order_by(
                    AdaptiveEdge.target_path, AdaptiveEdge.edge_type, AdaptiveEdge.id
                )
            )
            .scalars()
            .all()
        )

        if source_path == "__query__":
            # Explicit feedback is stored by kind so its event history is
            # retained.  Query activation needs one deterministic net delta.
            totals: dict[str, float] = {}
            for edge in edges:
                if edge.edge_type in {"explicit_positive", "explicit_negative"}:
                    totals[edge.target_path] = (
                        totals.get(edge.target_path, 0.0) + edge.weight_delta
                    )
            return {
                target: max(-0.9, min(1.0, totals[target])) for target in sorted(totals)
            }

        return {edge.target_path: edge.weight_delta for edge in edges}
