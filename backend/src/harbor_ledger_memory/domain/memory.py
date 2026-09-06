"""Immutable models for the memory persistence layer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _ImmutableModel(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class ShortTermMemory(_ImmutableModel):
    """A short-term memory event tied to a query trace."""

    trace_uuid: str
    query_text: str
    selected_paths: tuple[str, ...]
    created_at: str
    expires_at: str


class CacheRefresh(_ImmutableModel):
    """Paths refreshed in the short-term cache and evicted for capacity."""

    refreshed_paths: tuple[str, ...]
    evicted_paths: tuple[str, ...]


class AdaptiveEdgeInfo(_ImmutableModel):
    """An adaptive edge weight adjustment between two notes."""

    source_path: str
    target_path: str
    edge_type: str
    weight_delta: float
    last_updated: str
