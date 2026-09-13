"""Orchestration service that composes retrieval, activation, context and trace."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from typing import Any
from uuid import uuid4

import networkx as nx
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession
from harbor_ledger_memory.catalog.models import (
    ActivationVisit,
    ContextSelection,
    QueryTrace,
)
from harbor_ledger_memory.config import MemorySettings
from harbor_ledger_memory.domain.retrieval import (
    ActivatedNode,
    ContextMemory,
    QueryRequest,
    QueryResult,
    QuerySettings,
    SeedCandidate,
    ShortTermEvidence,
)
from harbor_ledger_memory.graph.activation import spread_activation
from harbor_ledger_memory.graph.builder import GraphBuilder
from harbor_ledger_memory.services.activity import ActivityService, graph_refs
from harbor_ledger_memory.services.adaptive import AdaptiveService
from harbor_ledger_memory.services.context import ContextBuilder
from harbor_ledger_memory.services.live_traversal import (
    NullLiveTraversalPublisher,
    TraversalEvent,
)
from harbor_ledger_memory.services.memory import MemoryService
from harbor_ledger_memory.services.retrieval import HybridRetrievalService

_TRACE_SCHEMA_VERSION = 1


class QueryService:
    """Compose retrieval, activation, context and trace persistence.

    All stages run within a single transaction boundary.  On an empty catalog
    the service returns an empty selection and a completed trace — never an
    exception.
    """

    def __init__(
        self,
        session: Session | Engine,
        *,
        retrieval_settings: QuerySettings | None = None,
        memory_settings: MemorySettings | None = None,
        path_filter: Callable[[str], bool] | None = None,
        activity_service: ActivityService | None = None,
        live_traversal: Any | None = None,
    ) -> None:
        self._session = (
            CatalogSession(bind=session) if isinstance(session, Engine) else session
        )
        self._settings = retrieval_settings or QuerySettings()
        self._memory_settings = memory_settings or MemorySettings()
        self._path_filter = path_filter
        self._activity_service = activity_service or ActivityService(self._session)
        self._live_traversal = live_traversal or NullLiveTraversalPublisher()

    def query(self, request: QueryRequest) -> QueryResult:
        """Execute a retrieval query end-to-end.

        1. Seed scoring via HybridRetrievalService
        2. Bounded graph activation via spread_activation
        3. Context package via ContextBuilder
        4. Persist trace, visits, and selections
        5. Return typed QueryResult (Pydantic models only)

        Returns an empty selection with a completed trace on an empty catalog.
        """
        start = time.monotonic()
        trace_uuid = uuid4()

        # --- Stage 1: Seed retrieval ---
        memory_service = MemoryService(self._session, self._memory_settings)
        removed_expired, removed_missing = memory_service.cleanup_cache()
        cache_candidates = memory_service.cache_candidates()
        retrieval = HybridRetrievalService(
            self._session,
            settings=self._settings,
            memory_settings=self._memory_settings,
            path_filter=self._path_filter,
        )
        seeds: tuple[SeedCandidate, ...] = retrieval.seeds(
            request.query,
            active_project=request.active_project,
            recent_paths=cache_candidates,
        )
        hit_paths = tuple(seed.path for seed in seeds if seed.path in cache_candidates)

        # --- Stage 2: Graph activation ---
        activated: tuple[ActivatedNode, ...] = ()
        graph: nx.MultiDiGraph[str] | None = None
        if seeds:
            graph = GraphBuilder(self._session, path_filter=self._path_filter).build()
            activated = spread_activation(
                graph,
                seeds,
                max_hops=self._settings.max_hops,
                max_nodes=self._settings.max_activation_nodes,
                decay=self._settings.decay,
                minimum_activation=self._settings.minimum_activation,
                edge_types=frozenset({"links_to", "contains", "parent_of"}),
                adaptive_deltas=AdaptiveService(
                    self._session, self._memory_settings
                ).get_deltas("__query__"),
            )
            # The activated nodes already carry the selected-edge provenance
            # (structural source/target, direction, edge key) chosen inside
            # spread_activation; context, exclusions, and the activity
            # payload use it as-is.  Persisted ActivationVisit rows store
            # the traversal step (via_path/edge_type) plus the structural
            # endpoints (edge_source/edge_target) when present; seeds leave
            # the endpoints NULL.

        # --- Stage 3: Context building ---
        context_builder = ContextBuilder(self._session)
        selected: tuple[ContextMemory, ...] = context_builder.build(
            request.query,
            seeds,
            activated,
            self._settings.context_token_budget,
        )
        refresh = memory_service.refresh_selected([memory.path for memory in selected])
        short_term_evidence = ShortTermEvidence(
            hit_paths=hit_paths,
            refreshed_paths=refresh.refreshed_paths,
            evicted_paths=refresh.evicted_paths,
            removed_expired=removed_expired,
            removed_missing=removed_missing,
        )

        # --- Stage 4: Determine excluded nodes ---
        selected_paths = {mem.path for mem in selected}
        excluded = tuple(node for node in activated if node.path not in selected_paths)

        # --- Stage 5: Persist trace ---
        trace_metadata = self._settings.model_dump()
        trace_metadata["short_term_evidence"] = short_term_evidence.model_dump(
            mode="json"
        )
        settings_snapshot = json.dumps(
            trace_metadata, ensure_ascii=False, sort_keys=True
        )

        trace = QueryTrace(
            trace_uuid=str(trace_uuid),
            query=request.query,
            active_project=request.active_project,
            retrieval_settings=settings_snapshot,
            schema_version=_TRACE_SCHEMA_VERSION,
            status="completed",
        )
        self._session.add(trace)
        self._session.flush()  # Get trace.id for child records

        # Preserve event history used by legacy adaptive behavior.
        memory_service.record_query(
            str(trace_uuid),
            request.query,
            [mem.path for mem in selected],
        )

        # Persist activation visits
        for node in activated:
            self._session.add(
                ActivationVisit(
                    trace_id=trace.id,
                    path=node.path,
                    activation_score=node.activation_score,
                    hop=node.hop,
                    via_path=node.via_path,
                    edge_type=node.edge_type,
                    edge_source=node.edge_source,
                    edge_target=node.edge_target,
                )
            )

        # Persist context selections
        for rank, mem in enumerate(selected, start=1):
            self._session.add(
                ContextSelection(
                    trace_id=trace.id,
                    path=mem.path,
                    rank=rank,
                    retrieval_score=mem.retrieval_score,
                    activation_score=mem.activation_score,
                    reasons=json.dumps(list(mem.reasons), ensure_ascii=False),
                    excerpt=mem.excerpt,
                    estimated_tokens=mem.estimated_tokens,
                )
            )

        # Calculate elapsed time
        elapsed_ms = (time.monotonic() - start) * 1000.0
        trace.latency_ms = round(elapsed_ms, 2)

        self._session.commit()

        for sequence, node in enumerate(
            sorted(activated, key=lambda item: (item.hop, item.path)), 1
        ):
            self._live_traversal.publish(
                TraversalEvent(
                    str(trace_uuid),
                    sequence,
                    "read",
                    node.path,
                    node.edge_source,
                    node.edge_target,
                    node.edge_type,
                )
            )

        total_tokens = sum(mem.estimated_tokens for mem in selected)
        touched_paths = {memory.path for memory in selected} | {
            node.path for node in activated
        }
        self._activity_service.record(
            "query",
            {
                "query": request.query,
                "trace_id": str(trace_uuid),
                "selected_paths": [memory.path for memory in selected],
                "total_estimated_tokens": total_tokens,
                "graph_refs": graph_refs(touched_paths),
                "graph_path": _activation_segments(activated),
            },
        )

        # --- Stage 6: Build and return result ---
        return QueryResult(
            trace_id=trace_uuid,
            query=request.query,
            selected_memories=selected,
            excluded_nodes=excluded,
            total_estimated_tokens=total_tokens,
            short_term_evidence=short_term_evidence,
        )


def _activation_segments(
    activated: Sequence[ActivatedNode],
) -> list[dict[str, str]]:
    """Return deterministic traversal segments for the activation graph.

    Each non-seed node contributes the structural edge it was reached
    through — a ``{source, target, edge_type, traversal_direction}`` segment
    read from the provenance ``spread_activation`` selected for that node.
    Source and target preserve the directed orientation of the edge as it
    exists in the graph projection, even when activation traversed the edge
    in reverse; ``traversal_direction`` records which way the activation
    moved.  Seeds (hop 0) have no incoming edge and therefore contribute no
    segment.  Segments are ordered by hop then path so the traversal reads
    as a level-order walk and is fully deterministic regardless of
    activation score.

    Nodes without provenance (bare hand-built fixtures) fall back to the
    traversal order: ``via_path -> path`` reported as forward.
    """
    traversed = [
        node
        for node in activated
        if node.via_path is not None and node.edge_type is not None
    ]
    traversed.sort(key=lambda node: (node.hop, node.path))
    segments: list[dict[str, str]] = []
    for node in traversed:
        via_path = node.via_path
        edge_type = node.edge_type
        if via_path is None or edge_type is None:
            continue
        if node.edge_source is not None and node.edge_target is not None:
            source, target = node.edge_source, node.edge_target
            direction = (
                node.traversal_direction
                if node.traversal_direction in ("forward", "reverse")
                else "forward"
            )
        else:
            source, target, direction = via_path, node.path, "forward"
        segments.append(
            {
                "source": source,
                "target": target,
                "edge_type": edge_type,
                "traversal_direction": direction,
            }
        )
    return segments
