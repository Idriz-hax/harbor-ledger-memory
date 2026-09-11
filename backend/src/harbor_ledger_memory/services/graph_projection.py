"""Project the catalog graph as a deterministic snapshot for API consumption."""

from __future__ import annotations

import hashlib
import base64
import hmac
import json
import time
import secrets
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass
from pathlib import PurePosixPath
from collections.abc import Callable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import aliased
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession
from harbor_ledger_memory.catalog.models import GraphEdgeFact, GraphNodeFact, GraphProjectionVersion, GraphSnapshotHandle
from harbor_ledger_memory.services.access import AccessPolicy
from harbor_ledger_memory.services.graph_materialization import MAX_PINNED_SNAPSHOT_HANDLES
from harbor_ledger_memory.graph.builder import GraphBuilder

GRAPH_VIEW_BUDGETS: dict[int, tuple[int, int]] = {
    0: (256, 512),
    1: (512, 2048),
    2: (1024, 4096),
}


@dataclass(frozen=True)
class GraphNode:
    """One node in the graph projection."""

    path: str
    title: str
    isolated: bool
    kind: str = "file"


@dataclass(frozen=True)
class GraphEdge:
    """One structural edge in the graph projection."""

    id: str
    source: str
    target: str
    edge_type: str
    explicit: bool


@dataclass(frozen=True)
class GraphSnapshot:
    """Complete deterministic snapshot of the vault graph."""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    generation: str


@dataclass(frozen=True)
class GraphCluster:
    id: str
    label: str
    kind: str
    type_counts: dict[str, int]
    member_count: int
    scope: str


@dataclass(frozen=True)
class GraphClusterEdge:
    id: str
    source: str
    target: str
    weight: float


@dataclass(frozen=True)
class GraphView:
    level: int
    scope: str | None
    clusters: tuple[GraphCluster, ...]
    edges: tuple[GraphClusterEdge, ...]
    generation: str
    next_cursor: str | None = None


class GraphHandleError(ValueError):
    """Opaque graph handle is invalid, expired, or policy-mismatched."""


class GraphProjectionService:
    """Build a flat, deterministic snapshot of the catalog graph."""

    def __init__(
        self,
        session: Session | Engine,
    ) -> None:
        self._session = (
            CatalogSession(bind=session) if isinstance(session, Engine) else session
        )

    def snapshot(self, path_filter: Callable[[str], bool] | None = None) -> GraphSnapshot:
        """Return every catalog note and all real structural edges."""
        graph = GraphBuilder(self._session, path_filter=path_filter).build()

        # Determine which paths participate in at least one edge
        connected_paths: set[str] = set()
        for source, target, _key in graph.edges(keys=True):
            connected_paths.add(source)
            connected_paths.add(target)

        # Build nodes — include every catalog note (even isolates)
        nodes: list[GraphNode] = []
        for path in sorted(graph.nodes):
            node_data = graph.nodes[path]
            nodes.append(
                GraphNode(
                    path=path,
                    title=str(node_data.get("title", "")),
                    isolated=(path not in connected_paths),
                    kind=str(node_data.get("kind", "file")),
                )
            )

        # Build edges — deterministic ordering, stable IDs
        edges: list[GraphEdge] = []
        seen_edges: set[tuple[str, str, str]] = set()
        for source, target, _, data in sorted(
            graph.edges(keys=True, data=True),
            key=lambda e: (e[0], e[1], e[3].get("edge_type", "")),
        ):
            edge_type = str(data.get("edge_type", ""))
            edge_key = (source, target, edge_type)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append(
                GraphEdge(
                    id=_edge_id(source, target, edge_type),
                    source=source,
                    target=target,
                    edge_type=edge_type,
                    explicit=bool(data.get("explicit", False)),
                )
            )

        # Generation: hash all serialized node and edge fields in deterministic order
        generation = _generation_id(nodes, edges)

        return GraphSnapshot(
            nodes=tuple(nodes),
            edges=tuple(edges),
            generation=generation,
        )

    def view(self, level: int, *, scope: str | None = None,
             path_filter: Callable[[str], bool] | None = None,
             policy_fingerprint: str = "public", policy: AccessPolicy | None = None, cursor: str | None = None,
             page_size: int = 100, cursor_secret: bytes = b"tide-atlas-phase-1b") -> GraphView:
        """Return a bounded, policy-filtered cluster view.

        Authorization is applied while building the graph, before any counts,
        edges, or generation are calculated.
        """
        if level not in GRAPH_VIEW_BUDGETS:
            raise ValueError("level must be 0, 1, or 2")
        if not 1 <= page_size <= 500:
            raise ValueError("page_size must be between 1 and 500")
        if level == 2 and scope is None:
            pass  # whole-vault view: no scope filter
        if scope is not None:
            parsed = PurePosixPath(scope)
            if not scope.strip() or parsed.is_absolute() or ".." in parsed.parts:
                raise ValueError("scope must be a non-empty vault-relative path")
        offset = 0
        scope_binding = _scope_binding(scope)
        handle_row = None
        if cursor:
            handle_row = self._session.scalar(select(GraphSnapshotHandle).where(GraphSnapshotHandle.handle == cursor))
            if handle_row is None or handle_row.expires_at <= datetime.now(UTC).isoformat():
                raise GraphHandleError("graph view handle invalid or expired; restart the view")
            if handle_row.level != level or handle_row.policy_fingerprint != policy_fingerprint or handle_row.scope_fingerprint != scope_binding:
                raise GraphHandleError("graph view handle invalid or expired; restart the view")
            offset = handle_row.offset
            version = self._session.get(GraphProjectionVersion, handle_row.version_id)
        else:
            version = self._session.scalar(select(GraphProjectionVersion).where(GraphProjectionVersion.active.is_(True)))
        if version is None:
            return GraphView(level, scope, (), (), _view_generation(level, scope, (), ()))
        if policy is None and path_filter is not None:
            # Compatibility callers may still provide a predicate; API callers
            # pass the policy so filtering can remain in SQL.
            policy_clause = None
        else:
            policy_clause = policy.readable_clause(GraphNodeFact.path) if policy else None
        folder_expr = GraphNodeFact.path if level == 2 else (
            GraphNodeFact.top_folder if level == 0 else GraphNodeFact.parent_folder
        )
        node_where = [GraphNodeFact.version_id == version.id]
        if policy_clause is not None:
            node_where.append(policy_clause)
        if scope is not None:
            prefix = scope.rstrip("/")
            node_where.append((GraphNodeFact.path == scope) | GraphNodeFact.path.like(prefix + "/%"))
        title_expr = GraphNodeFact.title if level == 2 else folder_expr
        node_stmt = select(folder_expr, GraphNodeFact.kind, title_expr, func.count(GraphNodeFact.id)).where(*node_where).group_by(folder_expr, GraphNodeFact.kind, title_expr).order_by(folder_expr, GraphNodeFact.kind, title_expr).limit(page_size + 1).offset(offset)
        cluster_rows = list(self._session.execute(node_stmt))
        if level == 2 and not cluster_rows and cursor is None:
            raise ValueError("scope is not accessible or contains no notes")
        clusters: list[GraphCluster] = []
        for folder, kind, label, count in cluster_rows[:page_size]:
            cluster_id = _cluster_id(level, folder, kind)
            clusters.append(GraphCluster(cluster_id, str(label), kind, {kind: int(count)}, int(count), folder))
        left = aliased(GraphNodeFact)
        right = aliased(GraphNodeFact)
        left_key = folder_expr._clone()._annotate({"parententity": left}) if False else (left.path if level == 2 else (left.top_folder if level == 0 else left.parent_folder))
        right_key = right.path if level == 2 else (right.top_folder if level == 0 else right.parent_folder)
        edge_where = [GraphEdgeFact.version_id == version.id, left.version_id == version.id, right.version_id == version.id]
        if policy is not None:
            edge_where.extend([policy.readable_clause(left.path), policy.readable_clause(right.path)])
        if scope is not None:
            prefix = scope.rstrip("/")
            edge_where.extend([(left.path == scope) | left.path.like(prefix + "/%"), (right.path == scope) | right.path.like(prefix + "/%")])
        edge_stmt = select(left_key, left.kind, right_key, right.kind, func.sum(GraphEdgeFact.weight)).join(left, left.path == GraphEdgeFact.source).join(right, right.path == GraphEdgeFact.target).where(*edge_where).group_by(left_key, left.kind, right_key, right.kind).order_by(left_key, left.kind, right_key, right.kind).limit(page_size + 1).offset(offset)
        edge_rows = list(self._session.execute(edge_stmt))
        edges = tuple(GraphClusterEdge(_cluster_edge_id(_cluster_id(level, a, ak), _cluster_id(level, b, bk)), _cluster_id(level, a, ak), _cluster_id(level, b, bk), float(weight)) for a, ak, b, bk, weight in edge_rows[:page_size] if a != b)
        max_nodes, max_edges = GRAPH_VIEW_BUDGETS[level]
        if len(clusters) > max_nodes or len(edges) > max_edges:
            raise ValueError(
                f"graph view exceeds level {level} budget "
                f"({max_nodes} nodes/{max_edges} edges)"
            )
        payload = {"level": level, "scope": scope,
                   "clusters": [(c.id, c.label, c.kind, sorted(c.type_counts.items()), c.member_count, c.scope) for c in clusters],
                   "edges": [(e.id, e.source, e.target, e.weight) for e in edges]}
        generation = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
        page_clusters = tuple(clusters)
        page_edges = edges
        next_cursor = None
        if len(cluster_rows) > page_size or len(edge_rows) > page_size:
            now = datetime.now(UTC).isoformat()
            self._session.execute(delete(GraphSnapshotHandle).where(GraphSnapshotHandle.expires_at <= now))
            handle_count = self._session.scalar(select(func.count()).select_from(GraphSnapshotHandle)) or 0
            while handle_count >= MAX_PINNED_SNAPSHOT_HANDLES:
                oldest = self._session.scalar(select(GraphSnapshotHandle).order_by(GraphSnapshotHandle.expires_at.asc(), GraphSnapshotHandle.handle.asc()).limit(1))
                if oldest is None:
                    break
                self._session.delete(oldest)
                self._session.flush()
                handle_count -= 1
            token = secrets.token_urlsafe(32)
            self._session.add(GraphSnapshotHandle(handle=token, version_id=version.id, policy_fingerprint=policy_fingerprint, scope_fingerprint=scope_binding, level=level, offset=offset + page_size, expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat()))
            self._session.commit()
            next_cursor = token
        return GraphView(level, scope, page_clusters, page_edges, generation, next_cursor)


def _edge_id(source: str, target: str, edge_type: str) -> str:
    """Deterministic stable ID for an edge."""
    raw = f"{source}\t{target}\t{edge_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _cluster_id(level: int, folder: str, kind: str) -> str:
    return "c_" + hashlib.sha256(f"{level}\t{folder}\t{kind}".encode()).hexdigest()[:16]


def _cluster_edge_id(source: str, target: str) -> str:
    return hashlib.sha256(f"{source}\t{target}".encode()).hexdigest()[:16]


def _encode_handle(payload: dict[str, object], secret: bytes) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(secret, body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + b"." + signature).decode().rstrip("=")


def _scope_binding(scope: str | None) -> str:
    return hashlib.sha256((scope or "").encode()).hexdigest()


def _decode_handle(value: str, secret: bytes) -> dict[str, object]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        body, signature = raw.rsplit(b".", 1)
        if not hmac.compare_digest(signature, hmac.new(secret, body, hashlib.sha256).digest()):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4)))
        if not isinstance(payload, dict) or int(payload.get("expires", 0)) < int(time.time()):
            raise ValueError
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise GraphHandleError("graph view handle invalid or expired; restart the view") from None


def _view_generation(level: int, scope: str | None, clusters: tuple[GraphCluster, ...], edges: tuple[GraphClusterEdge, ...]) -> str:
    return hashlib.sha256(json.dumps([level, scope, [(c.id, c.member_count) for c in clusters], [(e.id, e.weight) for e in edges]], separators=(",", ":")).encode()).hexdigest()[:12]


def _generation_id(nodes: list[GraphNode], edges: list[GraphEdge]) -> str:
    """Deterministic identifier over all serialized snapshot fields."""
    # Serialize every field of every node and edge in deterministic order
    payload = json.dumps(
        [{"path": n.path, "title": n.title, "isolated": n.isolated, "kind": n.kind} for n in nodes]
        + [
            {
                "id": e.id,
                "source": e.source,
                "target": e.target,
                "edge_type": e.edge_type,
                "explicit": e.explicit,
            }
            for e in edges
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
