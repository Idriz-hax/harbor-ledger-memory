"""Deterministic, read-only full scans of the configured vault scope."""

from __future__ import annotations

import hashlib
import logging
import posixpath
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import (
    CatalogSession,
    clear_catalog,
    persist_parsed_note,
)
from harbor_ledger_memory.catalog.models import (
    Diagnostic,
    Link,
    Note,
    NoteEmbedding,
    ScanRun,
)
from harbor_ledger_memory.domain.models import ParseDiagnostic, ParsedNote
from harbor_ledger_memory.services.activity import ActivityService, graph_refs
from harbor_ledger_memory.services.embeddings import (
    EmbeddingService,
    effective_embedding_model,
    embeddable_text,
)
from harbor_ledger_memory.services.live_traversal import (
    NullLiveTraversalPublisher,
    TraversalEvent,
)
from harbor_ledger_memory.vault.boundary import VaultBoundary
from harbor_ledger_memory.vault.parser import parse_note_bytes

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanDiagnostic:
    """A scan diagnostic with its vault-relative source path."""

    code: str
    message: str
    path: str
    severity: str = "error"
    line: int | None = None


@dataclass(frozen=True)
class ScanResult:
    """The stable, caller-facing summary of a completed full scan."""

    files_indexed: int
    broken_links: int
    diagnostics: tuple[ScanDiagnostic, ...]
    indexed_paths: tuple[str, ...] = ()
    ambiguous_links: int = 0
    added_paths: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    topology_paths: tuple[str, ...] = ()
    content_hashes: dict[str, str] | None = None
    links: dict[str, tuple[str, ...]] | None = None


def visible_scan_paths(paths: tuple[str, ...], can_read: Any) -> tuple[str, ...]:
    """Keep scan path evidence within the caller's effective read scope."""
    return tuple(path for path in paths if can_read(path))


def projection_identity(result: ScanResult) -> dict[str, object]:
    """Return the deterministic identity of the vault-derived projection."""
    return {
        "paths": tuple(sorted(result.indexed_paths)),
        "content_hashes": tuple(sorted((result.content_hashes or {}).items())),
        "links": tuple(sorted((result.links or {}).items())),
        "diagnostics": tuple(
            sorted(
                (d.code, d.message, d.path, d.severity, d.line)
                for d in result.diagnostics
            )
        ),
    }


@dataclass(frozen=True)
class LinkResolution:
    """Resolution state for one parsed wikilink or parent reference."""

    status: str
    target: str | None
    candidates: tuple[str, ...] = ()


class FullScanRequired(RuntimeError):
    """Incremental input cannot safely reconcile the active projection."""


class ScanService:
    """Build the disposable catalog from admitted Markdown files only."""

    _scan_lock = threading.RLock()

    def __init__(
        self,
        boundary: VaultBoundary,
        session: Session | Engine,
        *,
        embedding_model: str | None = None,
        activity_service: ActivityService | None = None,
        live_traversal: Any | None = None,
    ) -> None:
        self.boundary = boundary
        self._embedding_model = embedding_model
        self._session = (
            CatalogSession(bind=session) if isinstance(session, Engine) else session
        )
        self._activity_service = activity_service or ActivityService(self._session)
        self._live_traversal = live_traversal or NullLiveTraversalPublisher()

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        live_traversal: Any | None = None,
    ) -> ScanService:
        """Construct a service from settings without touching the vault yet."""
        from harbor_ledger_memory.catalog.database import create_database

        boundary = VaultBoundary(settings)
        return cls(
            boundary,
            create_database(settings.database_url),
            embedding_model=settings.memory.embedding_model,
            live_traversal=live_traversal,
        )

    def full_scan(
        self,
        trace_id: str | None = None,
        sequence_start: int = 0,
        *,
        defer_embeddings: bool = False,
    ) -> ScanResult:
        """Replace the catalog with a deterministic snapshot of admitted files."""
        with self._scan_lock:
            return self._full_scan_locked(
                trace_id, sequence_start, defer_embeddings=defer_embeddings
            )

    def _full_scan_locked(
        self, trace_id: str | None, sequence_start: int, *, defer_embeddings: bool
    ) -> ScanResult:
        snapshots = tuple(self.boundary.iter_admitted_snapshots())
        known_paths = tuple(snapshot.path.as_posix() for snapshot in snapshots)
        traversal_trace = trace_id or str(uuid4())

        parsed_files: list[tuple[str, ParsedNote, str, int, int | None]] = []
        for snapshot in snapshots:
            identity = snapshot.path.as_posix()
            raw = snapshot.content
            parsed = parse_note_bytes(raw, snapshot.path)
            parsed_files.append(
                (
                    identity,
                    parsed,
                    hashlib.sha256(raw).hexdigest(),
                    snapshot.file_size,
                    snapshot.file_mtime_ns,
                )
            )

        diagnostics: list[ScanDiagnostic] = []
        link_rows: dict[str, list[dict[str, Any]]] = {}
        broken_links = 0
        ambiguous_links = 0
        embedding_inputs: list[tuple[str, str]] = []
        for identity, parsed, _, _, _ in parsed_files:
            rows: list[dict[str, Any]] = []
            for wikilink in parsed.wikilinks:
                resolution = resolve_target(
                    wikilink.target,
                    identity,
                    known_paths,
                    ai_prefix=_link_prefix(self.boundary.index_relative),
                )
                rows.append(
                    {
                        "raw": wikilink.raw,
                        "normalized_target": resolution.target,
                        "alias": wikilink.alias,
                        "heading": wikilink.heading,
                        "block_id": wikilink.block_id,
                        "explicit": True,
                        "resolution_status": resolution.status,
                    }
                )
                if resolution.status == "broken":
                    broken_links += 1
                    diagnostics.append(
                        ScanDiagnostic(
                            code="link.broken",
                            message=f"link target does not resolve: {wikilink.target}",
                            path=identity,
                        )
                    )
                elif resolution.status == "ambiguous":
                    ambiguous_links += 1
                    diagnostics.append(
                        ScanDiagnostic(
                            code="link.ambiguous",
                            message=(
                                f"link target resolves to multiple notes: "
                                f"{wikilink.target}"
                            ),
                            path=identity,
                        )
                    )
            link_rows[identity] = rows
            diagnostics.extend(_parse_diagnostics(identity, parsed.diagnostics))

        session = self._session
        previous_hashes = {
            path: content_hash
            for path, content_hash in session.execute(
                select(Note.path, Note.content_hash)
            )
        }
        vault_hashes = {
            identity: content_hash for identity, _, content_hash, _, _ in parsed_files
        }
        # Deferred startup needs the configured identity to retain valid vectors,
        # but must not probe or initialize the optional runtime.
        emb_model = (
            self._embedding_model
            if defer_embeddings
            else effective_embedding_model(self._embedding_model)
        )
        preserved_embeddings = {
            path: (blob, created_at)
            for path, blob, created_at in session.execute(
                select(
                    NoteEmbedding.note_path,
                    NoteEmbedding.embedding_blob,
                    NoteEmbedding.created_at,
                ).where(NoteEmbedding.model_name == emb_model)
            )
            if emb_model
            and path in vault_hashes
            and previous_hashes.get(path) == vault_hashes[path]
        }
        added_paths = tuple(
            sorted(path for path in vault_hashes if path not in previous_hashes)
        )
        changed_paths = tuple(
            sorted(
                path
                for path, content_hash in vault_hashes.items()
                if path in previous_hashes and previous_hashes[path] != content_hash
            )
        )
        deleted_paths = tuple(
            sorted(path for path in previous_hashes if path not in vault_hashes)
        )
        # Snapshot the projection topology before the catalog is cleared so
        # the structural delta (links/containment) can be computed explicitly
        # after the new catalog is committed.
        from harbor_ledger_memory.graph.builder import GraphBuilder

        old_topology = _edge_set(GraphBuilder(session).build())
        try:
            clear_catalog(session)
            for instance in list(session.identity_map.values()):
                if isinstance(instance, NoteEmbedding):
                    session.expunge(instance)
            started_at = _timestamp()
            scan_run = ScanRun(
                started_at=started_at,
                status="running",
                notes_indexed=0,
                diagnostics_count=0,
            )
            session.add(scan_run)
            session.flush()

            for (
                identity,
                parsed,
                content_hash,
                file_size,
                file_mtime_ns,
            ) in parsed_files:
                note = persist_parsed_note(
                    session,
                    parsed,
                    content_hash=content_hash,
                    file_size=file_size,
                    file_mtime_ns=file_mtime_ns,
                    scan_timestamp=started_at,
                    resolved_links=link_rows[identity],
                )
                if identity in preserved_embeddings:
                    blob, created_at = preserved_embeddings[identity]
                    session.add(
                        NoteEmbedding(
                            note_path=identity,
                            embedding_blob=blob,
                            model_name=emb_model,
                            created_at=created_at,
                        )
                    )
                else:
                    text = embeddable_text(note.title, note.summary, note.content)
                    if text is not None:
                        embedding_inputs.append((identity, text))
                for diagnostic in note.diagnostics:
                    diagnostic.scan_run_id = scan_run.id
                for diagnostic in diagnostics:
                    if diagnostic.path == identity and diagnostic.code.startswith(
                        "link."
                    ):
                        session.add(
                            Diagnostic(
                                path=identity,
                                scan_run_id=scan_run.id,
                                code=diagnostic.code,
                                message=diagnostic.message,
                                severity=diagnostic.severity,
                                line=diagnostic.line,
                            )
                        )
            if emb_model and embedding_inputs and not defer_embeddings:
                embedding_service = EmbeddingService(emb_model)
                vectors = embedding_service.encode_batch(
                    [text for _, text in embedding_inputs]
                )
                if len(vectors) != len(embedding_inputs):
                    raise RuntimeError(
                        "embedding encoder returned "
                        f"{len(vectors)} vectors for {len(embedding_inputs)} inputs; "
                        "expected exactly one vector per input "
                        f"(model={self._embedding_model!r})"
                    )
                for index, (path, _) in enumerate(embedding_inputs):
                    vector = vectors[index]
                    session.add(
                        NoteEmbedding(
                            note_path=path,
                            embedding_blob=embedding_service.to_blob(vector),
                            model_name=self._embedding_model,
                            created_at=started_at,
                        )
                    )
            scan_run.status = "complete"
            scan_run.completed_at = _timestamp()
            scan_run.notes_indexed = len(parsed_files)
            scan_run.diagnostics_count = len(diagnostics)
            from harbor_ledger_memory.services.graph_materialization import (
                materialize_graph,
            )

            materialize_graph(session, created_at=scan_run.completed_at)
            session.commit()
        except Exception:
            session.rollback()
            raise

        # Topology delta: structural edges that were added to or removed from
        # the projection.  Endpoints of changed edges — even content-unchanged
        # sources whose adjacency changed (dangling links, new containment) —
        # must stay visible in the scan's graph refs.
        new_topology = _edge_set(GraphBuilder(self._session).build())
        changed_edges = old_topology ^ new_topology
        changed_endpoints = {
            endpoint
            for source, target, _ in changed_edges
            for endpoint in (source, target)
        }
        topology_paths = tuple(sorted(changed_endpoints))

        result = ScanResult(
            files_indexed=len(parsed_files),
            broken_links=broken_links,
            ambiguous_links=ambiguous_links,
            diagnostics=tuple(diagnostics),
            indexed_paths=tuple(identity for identity, *_ in parsed_files),
            added_paths=added_paths,
            changed_paths=changed_paths,
            deleted_paths=deleted_paths,
            topology_paths=topology_paths,
            content_hashes={
                path: content_hash for path, _, content_hash, _, _ in parsed_files
            },
            links={
                path: tuple(
                    f"{row['normalized_target']}:{row['resolution_status']}"
                    for row in rows
                )
                for path, rows in link_rows.items()
            },
        )
        for sequence, path in enumerate(
            sorted(result.indexed_paths), sequence_start + 1
        ):
            self._live_traversal.publish(
                TraversalEvent(traversal_trace, sequence, "read", path)
            )
        self._activity_service.record(
            "scan",
            {
                "files_indexed": result.files_indexed,
                "broken_links": result.broken_links,
                "ambiguous_links": result.ambiguous_links,
                "diagnostics": len(result.diagnostics),
                "graph_refs": graph_refs(
                    [
                        *result.added_paths,
                        *result.changed_paths,
                        *result.deleted_paths,
                        *result.topology_paths,
                    ]
                ),
                "added_paths": list(result.added_paths),
                "changed_paths": list(result.changed_paths),
                "deleted_paths": list(result.deleted_paths),
                "topology_paths": list(result.topology_paths),
            },
        )
        return result

    def incremental_scan(self, paths: list[str] | tuple[str, ...]) -> ScanResult:
        """Update only already-indexed Markdown identities with changed bytes."""
        with self._scan_lock:
            session = self._session
            known = {path for (path,) in session.execute(select(Note.path))}
            if not paths or any(path not in known for path in paths):
                raise FullScanRequired("full scan required: unknown or empty path set")
            snapshots = []
            for value in dict.fromkeys(paths):
                try:
                    identity = self.boundary.validate_identity(value)
                    if identity.as_posix() != value or identity.suffix.lower() != ".md":
                        raise ValueError("not a canonical Markdown identity")
                    snapshots.append(self.boundary.read_file(identity))
                except Exception as exc:
                    raise FullScanRequired(f"full scan required for {value}") from exc
            hashes = {
                s.path.as_posix(): hashlib.sha256(s.content).hexdigest()
                for s in snapshots
            }
            old_hashes = {
                path: digest
                for path, digest in session.execute(
                    select(Note.path, Note.content_hash)
                )
            }
            changed = [
                s
                for s in snapshots
                if hashes[s.path.as_posix()] != old_hashes[s.path.as_posix()]
            ]
            if not changed:
                result = ScanResult(
                    0,
                    0,
                    (),
                    indexed_paths=tuple(sorted(known)),
                    content_hashes=old_hashes,
                )
                now = _timestamp()
                scan = ScanRun(
                    started_at=now,
                    status="complete",
                    completed_at=now,
                    notes_indexed=0,
                    diagnostics_count=0,
                )
                session.add(scan)
                session.commit()
                self._publish_incremental_evidence(
                    result, (s.path.as_posix() for s in snapshots)
                )
                return result
            known_paths = tuple(sorted(known))
            diagnostics: list[ScanDiagnostic] = []
            link_rows: dict[str, list[dict[str, Any]]] = {}
            broken = ambiguous = 0
            parsed_rows = []
            for snapshot in changed:
                path = snapshot.path.as_posix()
                parsed = parse_note_bytes(snapshot.content, snapshot.path)
                rows = []
                for link in parsed.wikilinks:
                    resolution = resolve_target(
                        link.target,
                        path,
                        known_paths,
                        ai_prefix=_link_prefix(self.boundary.index_relative),
                    )
                    rows.append(
                        {
                            "raw": link.raw,
                            "normalized_target": resolution.target,
                            "alias": link.alias,
                            "heading": link.heading,
                            "block_id": link.block_id,
                            "explicit": True,
                            "resolution_status": resolution.status,
                        }
                    )
                    if resolution.status in {"broken", "ambiguous"}:
                        code = f"link.{resolution.status}"
                        diagnostics.append(
                            ScanDiagnostic(
                                code,
                                f"link target does not resolve: {link.target}"
                                if resolution.status == "broken"
                                else f"link target resolves to multiple notes: {link.target}",
                                path,
                            )
                        )
                        if resolution.status == "broken":
                            broken += 1
                        else:
                            ambiguous += 1
                diagnostics.extend(_parse_diagnostics(path, parsed.diagnostics))
                link_rows[path] = rows
                parsed_rows.append((snapshot, parsed, path))
            from harbor_ledger_memory.graph.builder import GraphBuilder
            from harbor_ledger_memory.services.graph_materialization import (
                materialize_graph,
            )

            old_topology = _edge_set(GraphBuilder(session).build())
            now = _timestamp()
            scan = ScanRun(
                started_at=now,
                status="running",
                notes_indexed=0,
                diagnostics_count=len(diagnostics),
            )
            try:
                session.add(scan)
                session.flush()
                for snapshot, parsed, path in parsed_rows:
                    session.query(Diagnostic).filter(Diagnostic.path == path).delete(
                        synchronize_session=False
                    )
                    session.query(Link).filter(Link.source_path == path).delete(
                        synchronize_session=False
                    )
                    session.query(NoteEmbedding).filter(
                        NoteEmbedding.note_path == path
                    ).delete(synchronize_session=False)
                    note = persist_parsed_note(
                        session,
                        parsed,
                        content_hash=hashes[path],
                        file_size=snapshot.file_size,
                        file_mtime_ns=snapshot.file_mtime_ns,
                        scan_timestamp=now,
                        resolved_links=link_rows[path],
                    )
                    for d in diagnostics:
                        if d.path == path and d.code.startswith("link."):
                            session.add(
                                Diagnostic(
                                    path=path,
                                    scan_run_id=scan.id,
                                    code=d.code,
                                    message=d.message,
                                    severity=d.severity,
                                    line=d.line,
                                )
                            )
                    for d in note.diagnostics:
                        d.scan_run_id = scan.id
                scan.status = "complete"
                scan.completed_at = _timestamp()
                scan.notes_indexed = len(changed)
                materialize_graph(session, created_at=scan.completed_at)
                session.commit()
            except Exception:
                session.rollback()
                raise
            topology = tuple(
                sorted(
                    {
                        endpoint
                        for edge in old_topology
                        ^ _edge_set(GraphBuilder(session).build())
                        for endpoint in edge[:2]
                    }
                )
            )
            result = ScanResult(
                len(changed),
                broken,
                tuple(diagnostics),
                indexed_paths=tuple(sorted(known)),
                changed_paths=tuple(sorted(s.path.as_posix() for s in changed)),
                topology_paths=topology,
                content_hashes={**old_hashes, **{p: hashes[p] for p in hashes}},
                links={
                    p: tuple(
                        f"{r['normalized_target']}:{r['resolution_status']}"
                        for r in rows
                    )
                    for p, rows in link_rows.items()
                },
            )
            self._publish_incremental_evidence(result, result.changed_paths)
            return result

    def _publish_incremental_evidence(
        self, result: ScanResult, read_paths: Any
    ) -> None:
        """Publish post-commit evidence best-effort; projection is already committed."""
        trace = str(uuid4())
        try:
            for sequence, path in enumerate(sorted(read_paths), 1):
                self._live_traversal.publish(
                    TraversalEvent(trace, sequence, "read", path)
                )
        except Exception:
            logger.exception("incremental scan traversal notification failed")
        try:
            self._activity_service.record(
                "scan",
                {
                    "files_indexed": result.files_indexed,
                    "broken_links": result.broken_links,
                    "ambiguous_links": result.ambiguous_links,
                    "diagnostics": len(result.diagnostics),
                    "graph_refs": graph_refs(
                        [*result.changed_paths, *result.topology_paths]
                    ),
                    "added_paths": [],
                    "changed_paths": list(result.changed_paths),
                    "deleted_paths": [],
                    "topology_paths": list(result.topology_paths),
                },
            )
        except Exception:
            logger.exception("incremental scan activity notification failed")

    def reconcile_embeddings(self, *, batch_size: int = 32) -> int:
        """Fill missing vectors in bounded batches, validating snapshots before write."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        model = effective_embedding_model(self._embedding_model)
        if not model:
            return 0
        session = self._session
        inserted = 0
        try:
            pending = session.execute(
                select(
                    Note.path, Note.title, Note.summary, Note.content, Note.content_hash
                )
                .outerjoin(
                    NoteEmbedding,
                    (NoteEmbedding.note_path == Note.path)
                    & (NoteEmbedding.model_name == model),
                )
                .where(NoteEmbedding.note_path.is_(None))
                .order_by(Note.path)
            ).all()
            for offset in range(0, len(pending), batch_size):
                batch = pending[offset : offset + batch_size]
                inputs = [
                    (
                        row.path,
                        row.content_hash,
                        embeddable_text(row.title, row.summary, row.content),
                    )
                    for row in batch
                ]
                inputs = [(path, digest, text) for path, digest, text in inputs if text]
                if not inputs:
                    continue
                session.rollback()
                encoder = EmbeddingService(model)
                vectors = encoder.encode_batch([text for _, _, text in inputs])
                if len(vectors) != len(inputs):
                    raise RuntimeError(
                        "embedding encoder returned invalid batch cardinality"
                    )
                for (path, digest, _), vector in zip(inputs, vectors):
                    current = session.execute(
                        select(Note.content_hash).where(Note.path == path)
                    ).scalar_one_or_none()
                    exists = session.scalar(
                        select(NoteEmbedding.note_path).where(
                            NoteEmbedding.note_path == path,
                            NoteEmbedding.model_name == model,
                        )
                    )
                    if current == digest and exists is None:
                        session.add(
                            NoteEmbedding(
                                note_path=path,
                                embedding_blob=encoder.to_blob(vector),
                                model_name=model,
                                created_at=_timestamp(),
                            )
                        )
                        inserted += 1
                session.commit()
        except Exception:
            session.rollback()
            raise
        return inserted

    def set_activity_service(self, activity_service: ActivityService) -> None:
        """Use the application's shared activity fan-out for future scans."""
        self._activity_service = activity_service

    def set_live_traversal(self, publisher: Any) -> None:
        """Use the application's shared traversal publisher."""
        self._live_traversal = publisher


def resolve_target(
    target: str,
    source_path: str,
    known_paths: tuple[str, ...] | list[str],
    *,
    ai_prefix: str = "AI",
) -> LinkResolution:
    """Resolve a target without reading the filesystem or guessing a winner."""
    clean_target = target.strip().replace("\\", "/")
    if not clean_target or clean_target.startswith("/"):
        return LinkResolution(
            "broken", _candidate_label(source_path, clean_target, ai_prefix)
        )

    known = set(known_paths)
    explicit = bool(ai_prefix) and (
        clean_target == ai_prefix or clean_target.startswith(f"{ai_prefix}/")
    )
    if explicit:
        explicit_candidates = _path_candidates(clean_target, ai_prefix)
        matches = _matches(explicit_candidates, known)
        return _resolution_from_matches(matches, explicit_candidates)

    folder = PurePosixPath(source_path).parent
    folder_target = _safe_join(folder, clean_target)
    folder_candidates = (
        _path_candidates(folder_target.as_posix(), ai_prefix)
        if folder_target is not None
        else ()
    )
    matches = _matches(folder_candidates, known)
    if matches:
        return _resolution_from_matches(matches, folder_candidates)

    # A full-vault scope has no configured prefix. A slash-containing target
    # may therefore be an explicit vault-relative identity after the local
    # folder interpretation has been tried.
    if not ai_prefix:
        root_candidates = _path_candidates(clean_target, "")
        root_matches = _matches(root_candidates, known)
        if root_matches:
            return _resolution_from_matches(root_matches, root_candidates)

    # Obsidian also permits a basename when the folder-relative interpretation
    # does not exist. Keeping this fallback explicit lets ambiguity remain
    # visible instead of selecting whichever file happened to be enumerated.
    stem = PurePosixPath(clean_target).name
    basename_matches = tuple(
        path for path in known_paths if PurePosixPath(path).name in {stem, f"{stem}.md"}
    )
    if len(basename_matches) == 1:
        return LinkResolution("resolved", basename_matches[0], basename_matches)
    if len(basename_matches) > 1:
        return LinkResolution("ambiguous", None, tuple(sorted(basename_matches)))
    return LinkResolution(
        "broken",
        folder_candidates[0] if folder_candidates else clean_target,
    )


def _edge_set(graph: Any) -> set[tuple[str, str, str]]:
    """Unique structural edges ``(source, target, edge_type)`` of a projection.

    Accepts both ``DiGraph`` (3-tuple records) and ``MultiDiGraph``
    (4-tuple records with an edge key); parallel edges of the same type
    collapse into one entry.
    """
    edges: set[tuple[str, str, str]] = set()
    for record in graph.edges(data=True):
        if len(record) == 4:
            source, target, _key, data = record
        else:
            source, target, data = record
        edges.add((source, target, str(data.get("edge_type", ""))))
    return edges


def _parse_diagnostics(
    path: str, values: tuple[ParseDiagnostic, ...]
) -> list[ScanDiagnostic]:
    return [
        ScanDiagnostic(
            code=value.code,
            message=value.message,
            path=path,
            severity=value.severity,
            line=value.line,
        )
        for value in values
    ]


def _path_candidates(target: str, ai_prefix: str) -> tuple[str, ...]:
    normalized = posixpath.normpath(target)
    if normalized == "." or normalized.startswith("../") or normalized == "..":
        return ()
    if (
        ai_prefix
        and not normalized.startswith(f"{ai_prefix}/")
        and normalized != ai_prefix
    ):
        return ()
    path = PurePosixPath(normalized)
    if path.suffix.lower() == ".md":
        return (path.as_posix(),)
    return (path.as_posix(), f"{path.as_posix()}.md")


def _safe_join(base: PurePosixPath, target: str) -> PurePosixPath | None:
    joined = PurePosixPath(posixpath.normpath(posixpath.join(base.as_posix(), target)))
    if joined.is_absolute() or ".." in joined.parts:
        return None
    return joined


def _matches(candidates: tuple[str, ...], known: set[str]) -> tuple[str, ...]:
    return tuple(sorted({candidate for candidate in candidates if candidate in known}))


def _resolution_from_matches(
    matches: tuple[str, ...], candidates: tuple[str, ...]
) -> LinkResolution:
    if len(matches) == 1:
        return LinkResolution("resolved", matches[0], matches)
    if len(matches) > 1:
        return LinkResolution("ambiguous", None, matches)
    return LinkResolution("broken", candidates[0] if candidates else None)


def _candidate_label(source_path: str, target: str, ai_prefix: str) -> str:
    if ai_prefix and target.startswith(f"{ai_prefix}/"):
        return target
    source_dir = PurePosixPath(source_path).parent
    candidate = _safe_join(source_dir, target)
    return candidate.as_posix() if candidate is not None else target


def _link_prefix(index_root: PurePosixPath) -> str:
    """Return the explicit link prefix, or empty for a full-vault scope."""
    return "" if not index_root.parts else index_root.as_posix()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "LinkResolution",
    "ScanDiagnostic",
    "ScanResult",
    "ScanService",
    "projection_identity",
    "visible_scan_paths",
    "resolve_target",
]
