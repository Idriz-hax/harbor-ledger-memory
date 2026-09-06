"""Read-only graph query service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import networkx as nx
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession
from harbor_ledger_memory.catalog.models import Diagnostic, Link, Note, ScanRun
from harbor_ledger_memory.config import Settings
from harbor_ledger_memory.graph.builder import GraphBuilder
from harbor_ledger_memory.services.access import AccessPolicy


@dataclass(frozen=True)
class GraphNeighbour:
    """One adjacent graph node and the edge connecting it."""

    path: str
    edge_type: str
    direction: str
    node_type: str
    explicit: bool
    inferred: bool
    confidence: float
    weight: float
    source: str


class GraphService:
    """Expose deterministic incoming and outgoing graph neighbours."""

    def __init__(
        self, source: GraphBuilder | nx.MultiDiGraph[str] | Session | Engine
    ) -> None:
        if isinstance(source, GraphBuilder):
            self._builder = source
            self._graph: nx.MultiDiGraph[str] | None = None
        elif isinstance(source, nx.MultiDiGraph):
            self._builder = None
            self._graph = source
        else:
            self._builder = GraphBuilder(source)
            self._graph = None

    def neighbours(self, path: str) -> list[GraphNeighbour]:
        if self._graph is None:
            assert self._builder is not None
            graph = self._builder.build()
        else:
            graph = self._graph
        if path not in graph:
            return []

        result: list[GraphNeighbour] = []
        for _, target, _, data in graph.out_edges(path, keys=True, data=True):
            result.append(_neighbour(graph, target, "outgoing", data))
        for source, _, _, data in graph.in_edges(path, keys=True, data=True):
            result.append(_neighbour(graph, source, "incoming", data))
        return sorted(
            result,
            key=lambda item: (
                item.path,
                item.direction,
                item.edge_type,
                item.source,
            ),
        )


@dataclass(frozen=True)
class CatalogStatus:
    """Read-only operational status for the disposable catalog."""

    indexed_notes: int
    scan_runs: int
    diagnostics: int
    broken_links: int
    ambiguous_links: int
    last_scan_status: str | None
    last_scan_completed_at: str | None


class CatalogStatusService:
    """Read catalog status without opening or changing vault files."""

    def __init__(
        self,
        session: Session | Engine,
        *,
        path_filter: Callable[[str], bool] | None = None,
    ) -> None:
        self._session = (
            CatalogSession(bind=session) if isinstance(session, Engine) else session
        )
        self._path_filter = path_filter

    def read(self) -> CatalogStatus:
        """Return counts and the latest scan metadata."""
        paths = [
            path
            for path in self._session.scalars(select(Note.path))
            if self._path_filter is None or self._path_filter(path)
        ]
        indexed_notes = len(paths)
        scan_runs = int(self._session.scalar(select(func.count(ScanRun.id))) or 0)
        diagnostics = int(
            sum(
                1
                for path in self._session.scalars(select(Diagnostic.path))
                if path is None or self._path_filter is None or self._path_filter(path)
            )
        )
        broken_links = int(
            sum(
                1
                for link in self._session.scalars(
                    select(Link).where(Link.resolution_status == "broken")
                )
                if self._path_filter is None or self._path_filter(link.source_path)
            )
        )
        ambiguous_links = int(
            sum(
                1
                for link in self._session.scalars(
                    select(Link).where(Link.resolution_status == "ambiguous")
                )
                if self._path_filter is None or self._path_filter(link.source_path)
            )
        )
        latest = self._session.scalar(
            select(ScanRun).order_by(ScanRun.id.desc()).limit(1)
        )
        return CatalogStatus(
            indexed_notes=indexed_notes,
            scan_runs=scan_runs,
            diagnostics=diagnostics,
            broken_links=broken_links,
            ambiguous_links=ambiguous_links,
            last_scan_status=None if latest is None else latest.status,
            last_scan_completed_at=(None if latest is None else latest.completed_at),
        )


def vault_is_read_only(settings: Settings) -> bool:
    """Whether no configured rule grants write access."""
    return not any(rule.access.is_writable for rule in settings.folder_rules)


def write_policy_payload(settings: Settings) -> dict[str, object]:
    """Return the complete effective write policy representation.

    Unmatched paths are read-only.  Keeping that default explicit prevents a
    mixed rule set from being represented as globally writable or read-only.
    """
    return {
        "default_access": "read",
        "rules": [
            {"path": rule.path.as_posix(), "access": rule.access.value}
            for rule in settings.folder_rules
        ],
    }


def canonical_status_payload(
    settings: Settings, catalog: CatalogStatus
) -> dict[str, object]:
    """Build the canonical status payload shared by all transports."""
    return {
        "status": "ok",
        "read_only": vault_is_read_only(settings),
        "write_policy": write_policy_payload(settings),
        "vault_scope": settings.index_root.as_posix(),
        "effective_read_scope": settings.effective_read_scope,
        **asdict(catalog),
    }


def effective_read_scope_for_policy(policy: AccessPolicy, root: str) -> str:
    """Describe this token's own read scope for per-token status surfaces.

    Only the calling token's own unreadable (deny) rules are named, so the
    scope string never leaks a path another token configured.
    """
    denied = sorted(
        rule.path.as_posix() for rule in policy.rules if not rule.access.is_readable
    )
    if not denied:
        return root
    return f"{root} (deny: {', '.join(denied)})"


def token_status_payload(
    policy: AccessPolicy, settings: Settings, catalog: CatalogStatus
) -> dict[str, object]:
    """Report the CALLING token's own policy, not vault-global facts.

    ``read_only`` reflects whether this token grants any write access, and
    ``write_policy.rules`` lists only this token's own rules, so two tokens
    with different grants see different status.  ``effective_read_scope`` is
    recomputed from the token's own rules so it never names a path this token
    did not configure.  ``vault_scope`` stays the shared index root.
    """
    root = settings.index_root.as_posix()
    return {
        "status": "ok",
        "read_only": not policy.has_any_write(),
        "write_policy": {
            "default_access": "read",
            "rules": [
                {"path": rule.path.as_posix(), "access": rule.access.value}
                for rule in policy.rules
            ],
        },
        "vault_scope": root,
        "effective_read_scope": effective_read_scope_for_policy(policy, root),
        **asdict(catalog),
    }


def _neighbour(
    graph: nx.MultiDiGraph[str],
    path: str,
    direction: str,
    data: dict[str, Any],
) -> GraphNeighbour:
    return GraphNeighbour(
        path=path,
        edge_type=str(data.get("edge_type", "")),
        direction=direction,
        node_type=str(graph.nodes[path].get("node_type", "File")),
        explicit=bool(data.get("explicit", False)),
        inferred=bool(data.get("inferred", False)),
        confidence=float(data.get("confidence", 0.0)),
        weight=float(data.get("weight", 0.0)),
        source=str(data.get("source", "")),
    )


__all__ = [
    "CatalogStatus",
    "CatalogStatusService",
    "GraphNeighbour",
    "GraphService",
    "canonical_status_payload",
    "effective_read_scope_for_policy",
    "token_status_payload",
    "write_policy_payload",
    "vault_is_read_only",
]
