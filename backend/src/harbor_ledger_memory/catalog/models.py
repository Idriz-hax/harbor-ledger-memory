"""SQLAlchemy models for the disposable SQLite catalog."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, synonym
from sqlalchemy.types import TypeDecorator


class PosixPathType(TypeDecorator[str]):
    """Store vault identities as validated, vault-relative POSIX strings."""

    impl = String(512)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(str(value))
        if (
            not path.parts
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in path.as_posix()
        ):
            raise ValueError("catalog paths must be relative POSIX paths")
        return path.as_posix()

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        return None if value is None else str(value)


class MemoryPathType(PosixPathType):
    """Vault-relative POSIX paths at the historical 1024-character width."""

    impl = String(1024)


class Base(DeclarativeBase):
    """Declarative metadata for application-owned catalog tables."""


class Note(Base):
    """A parsed note and its source metadata snapshot."""

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(PosixPathType(), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note_type: Mapped[str | None] = synonym("type")
    status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    frontmatter_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    frontmatter: Mapped[str] = synonym("frontmatter_json")
    created: Mapped[str | None] = mapped_column(String(256), nullable=True)
    updated: Mapped[str | None] = mapped_column(String(256), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_mtime_ns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scan_timestamp: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scanned_at: Mapped[str | None] = synonym("scan_timestamp")

    links: Mapped[list[Link]] = relationship(
        back_populates="note",
        cascade="all, delete-orphan",
        order_by="Link.id",
    )
    diagnostics: Mapped[list[Diagnostic]] = relationship(
        back_populates="note",
        cascade="all, delete-orphan",
        order_by="Diagnostic.id",
    )


class Link(Base):
    """One outgoing link identity retained by the catalog."""

    __tablename__ = "links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_path: Mapped[str] = mapped_column(
        PosixPathType(),
        ForeignKey("notes.path", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    note_path: Mapped[str] = synonym("source_path")
    raw: Mapped[str] = mapped_column(Text, nullable=False)
    raw_form: Mapped[str] = synonym("raw")
    normalized_target: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_candidate: Mapped[str | None] = synonym("normalized_target")
    alias: Mapped[str | None] = mapped_column(Text, nullable=True)
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    block_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    explicit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    resolution_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unresolved"
    )

    note: Mapped[Note] = relationship(back_populates="links")


class ScanRun(Base):
    """A record of a catalog build, without any vault ownership."""

    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[str] = mapped_column(String(128), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    notes_indexed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    diagnostics_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class GraphProjectionVersion(Base):
    """Immutable full graph facts produced by one completed scan transaction."""

    __tablename__ = "graph_projection_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )


class GraphSnapshotHandle(Base):
    """Opaque short-lived handle retaining a historical graph version."""

    __tablename__ = "graph_snapshot_handles"

    handle: Mapped[str] = mapped_column(String(128), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    policy_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[str] = mapped_column(String(128), nullable=False, index=True)


class GraphNodeFact(Base):
    """A durable node fact belonging to one graph projection version."""

    __tablename__ = "graph_node_facts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("graph_projection_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        primary_key=True,
    )
    path: Mapped[str] = mapped_column(PosixPathType(), nullable=False, index=True)
    parent_folder: Mapped[str] = mapped_column(String(512), nullable=False)
    top_folder: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)


class GraphEdgeFact(Base):
    """A durable weighted edge fact belonging to one graph projection version."""

    __tablename__ = "graph_edge_facts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("graph_projection_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        primary_key=True,
    )
    source: Mapped[str] = mapped_column(PosixPathType(), nullable=False, index=True)
    target: Mapped[str] = mapped_column(PosixPathType(), nullable=False, index=True)
    edge_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    weight: Mapped[float] = mapped_column(Float, nullable=False)


class Diagnostic(Base):
    """A parser or catalog diagnostic associated with an optional note."""

    __tablename__ = "diagnostics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str | None] = mapped_column(
        PosixPathType(),
        ForeignKey("notes.path", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    note_path: Mapped[str | None] = synonym("path")
    scan_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True
    )
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="error")
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)

    note: Mapped[Note | None] = relationship(back_populates="diagnostics")


class QueryTrace(Base):
    """Top-level record of a retrieval query activation trace."""

    __tablename__ = "query_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_uuid: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True, default=lambda: uuid4().hex
    )
    created_at: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(),
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    active_project: Mapped[str | None] = mapped_column(String(256), nullable=True)
    retrieval_settings: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # JSON snapshot of QuerySettings
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", default=1
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    visits: Mapped[list[ActivationVisit]] = relationship(
        back_populates="trace",
        cascade="all, delete-orphan",
        order_by="ActivationVisit.id",
    )
    selections: Mapped[list[ContextSelection]] = relationship(
        back_populates="trace",
        cascade="all, delete-orphan",
        order_by="ContextSelection.rank",
    )


class ActivationVisit(Base):
    """One node activated during graph traversal for a query trace."""

    __tablename__ = "activation_visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[int] = mapped_column(
        ForeignKey("query_traces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    path: Mapped[str] = mapped_column(PosixPathType(), nullable=False)
    activation_score: Mapped[float] = mapped_column(Float, nullable=False)
    hop: Mapped[int] = mapped_column(Integer, nullable=False)
    via_path: Mapped[str | None] = mapped_column(PosixPathType(), nullable=True)
    edge_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    edge_source: Mapped[str | None] = mapped_column(String(512), nullable=True)
    edge_target: Mapped[str | None] = mapped_column(String(512), nullable=True)

    trace: Mapped[QueryTrace] = relationship(back_populates="visits")


class ContextSelection(Base):
    """One node selected as context output for a query trace."""

    __tablename__ = "context_selections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[int] = mapped_column(
        ForeignKey("query_traces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    path: Mapped[str] = mapped_column(PosixPathType(), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieval_score: Mapped[float] = mapped_column(Float, nullable=False)
    activation_score: Mapped[float] = mapped_column(Float, nullable=False)
    reasons: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False)

    trace: Mapped[QueryTrace] = relationship(back_populates="selections")


class ShortTermEvent(Base):
    """A short-term memory event tied to a query trace."""

    __tablename__ = "short_term_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_uuid: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True
    )
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    selected_paths: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(128), nullable=False)


class ShortTermEntry(Base):
    """A bounded, recency-ranked cache entry for a selected note."""

    __tablename__ = "short_term_entries"

    path: Mapped[str] = mapped_column(
        PosixPathType(),
        ForeignKey("notes.path", ondelete="CASCADE"),
        primary_key=True,
    )
    last_selected_at: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )
    selection_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class AdaptiveEdge(Base):
    """An adaptive edge weight adjustment between two notes."""

    __tablename__ = "adaptive_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False)
    target_path: Mapped[str] = mapped_column(String(512), nullable=False)
    edge_type: Mapped[str] = mapped_column(String(32), nullable=False)
    weight_delta: Mapped[float] = mapped_column(Float, nullable=False)
    last_updated: Mapped[str] = mapped_column(String(128), nullable=False)


class NoteEmbedding(Base):
    """Embedding vector for a note's text content."""

    __tablename__ = "note_embeddings"

    note_path: Mapped[str] = mapped_column(
        PosixPathType(),
        ForeignKey("notes.path", ondelete="CASCADE"),
        primary_key=True,
    )
    embedding_blob: Mapped[bytes] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[str] = mapped_column(String(128), nullable=False)


class ActivityEvent(Base):
    """A durable operational event for the live activity projection."""

    __tablename__ = "activity_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    operation_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    graph_refs_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class MemoryWriteProposal(Base):
    """A pending or resolved write operation against a vault note."""

    __tablename__ = "memory_write_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(MemoryPathType(), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True,
        default="pending",
        server_default="pending",
    )
    rule_access: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_source_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    resolved_at: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    creator_token_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("api_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    applying_at: Mapped[str | None] = mapped_column(String(128), nullable=True)
    applied_content_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    affected_paths_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    created_paths_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )

    @property
    def affected_paths(self) -> list[str]:
        return _decode_path_list(self.affected_paths_json)

    @affected_paths.setter
    def affected_paths(self, paths: list[str]) -> None:
        self.affected_paths_json = json.dumps(paths, separators=(",", ":"))

    @property
    def created_paths(self) -> list[str]:
        return _decode_path_list(self.created_paths_json)

    @created_paths.setter
    def created_paths(self, paths: list[str]) -> None:
        self.created_paths_json = json.dumps(paths, separators=(",", ":"))


def _decode_path_list(value: str | None) -> list[str]:
    if not value:
        return []
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        return []
    values = cast(list[Any], decoded)
    return [item for item in values if isinstance(item, str)]


class ApiToken(Base):
    """An API token record; only the SHA-256 hash is stored."""

    __tablename__ = "api_tokens"
    # Token names are unique among ACTIVE rows only; a name may be reused
    # after its token is revoked (revoked rows remain for audit).
    __table_args__ = (
        Index(
            "uq_api_tokens_active_name",
            "name",
            unique=True,
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array of scopes
    rules: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0"), default=False
    )
    approve_own_proposals: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0"), default=False
    )
    created_at: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(),
    )
    last_used_at: Mapped[str | None] = mapped_column(String(128), nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(String(128), nullable=True)


__all__ = [
    "ActivationVisit",
    "ActivityEvent",
    "ApiToken",
    "AdaptiveEdge",
    "Base",
    "ContextSelection",
    "Diagnostic",
    "Link",
    "MemoryPathType",
    "MemoryWriteProposal",
    "Note",
    "NoteEmbedding",
    "PosixPathType",
    "QueryTrace",
    "ScanRun",
    "ShortTermEntry",
    "ShortTermEvent",
]
