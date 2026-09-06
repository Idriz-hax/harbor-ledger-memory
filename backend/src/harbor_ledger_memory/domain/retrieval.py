"""Public retrieval request/result models and query settings."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class QueryRequest(BaseModel):
    """Immutable request model for a retrieval query."""

    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1, max_length=2_000)
    active_project: str | None = None
    include_excluded: bool = False


class SeedCandidate(BaseModel):
    """A candidate seed node identified by the retrieval stage."""

    model_config = ConfigDict(frozen=True)

    path: str
    retrieval_score: float
    reasons: tuple[str, ...] = ()


class ActivatedNode(BaseModel):
    """One node activated during graph traversal.

    ``via_path``/``edge_type`` describe the traversal step that reached this
    node.  ``edge_source``/``edge_target`` carry the *structural* orientation
    of the graph-projection edge the traversal actually used (``edge_source``
    → ``edge_target``), which may be the reverse of the traversal direction;
    ``traversal_direction`` records which way the activation actually moved
    ("forward" or "reverse"), and ``edge_key`` is the multigraph key of that
    exact projection edge.

    The provenance fields are selected by ``spread_activation`` at enqueue
    time — the edge that produced the node's winning score, after any
    adaptive weight deltas — and are intrinsic to the result.  The traversal
    step (``via_path``/``edge_type``) and the structural endpoints
    (``edge_source``/``edge_target``) are persisted in ``ActivationVisit``
    rows; ``traversal_direction`` and ``edge_key`` are not.  None of the
    provenance is re-derived from the projection afterwards.  Seeds (hop 0)
    leave the provenance fields as ``None``.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    activation_score: float
    hop: int
    via_path: str | None
    edge_type: str | None
    edge_source: str | None = None
    edge_target: str | None = None
    traversal_direction: str | None = None
    edge_key: int | str | None = None


class ContextMemory(BaseModel):
    """One node selected as context output."""

    model_config = ConfigDict(frozen=True)

    path: str
    title: str
    summary: str | None
    excerpt: str
    retrieval_score: float
    activation_score: float
    reasons: tuple[str, ...]
    estimated_tokens: int


class ShortTermEvidence(BaseModel):
    """Immutable cache evidence captured while executing a query."""

    model_config = ConfigDict(frozen=True)

    hit_paths: tuple[str, ...]
    refreshed_paths: tuple[str, ...]
    evicted_paths: tuple[str, ...]
    removed_expired: int
    removed_missing: int


class QueryResult(BaseModel):
    """Immutable result of a retrieval query, including activation trace."""

    model_config = ConfigDict(frozen=True)

    trace_id: UUID
    query: str
    selected_memories: tuple[ContextMemory, ...]
    excluded_nodes: tuple[ActivatedNode, ...]
    total_estimated_tokens: int
    short_term_evidence: ShortTermEvidence


class QuerySettings(BaseModel):
    """Configurable bounds and thresholds for graph-based retrieval."""

    model_config = ConfigDict(frozen=True)

    max_seed_nodes: int = Field(default=10, gt=0)
    max_activation_nodes: int = Field(default=50, gt=0)
    max_hops: int = Field(default=3, gt=0)
    decay: float = Field(default=0.65, gt=0, le=1.0)
    minimum_activation: float = Field(default=0.05, gt=0, lt=1.0)
    context_token_budget: int = Field(default=12_000, gt=0)
