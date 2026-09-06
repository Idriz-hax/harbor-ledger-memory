"""Project the catalog graph as a deterministic snapshot for API consumption."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession
from harbor_ledger_memory.graph.builder import GraphBuilder


@dataclass(frozen=True)
class GraphNode:
    """One node in the graph projection."""

    path: str
    title: str
    isolated: bool


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


class GraphProjectionService:
    """Build a flat, deterministic snapshot of the catalog graph."""

    def __init__(
        self,
        session: Session | Engine,
    ) -> None:
        self._session = (
            CatalogSession(bind=session) if isinstance(session, Engine) else session
        )

    def snapshot(self) -> GraphSnapshot:
        """Return every catalog note and all real structural edges."""
        graph = GraphBuilder(self._session).build()

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


def _edge_id(source: str, target: str, edge_type: str) -> str:
    """Deterministic stable ID for an edge."""
    raw = f"{source}\t{target}\t{edge_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _generation_id(nodes: list[GraphNode], edges: list[GraphEdge]) -> str:
    """Deterministic identifier over all serialized snapshot fields."""
    # Serialize every field of every node and edge in deterministic order
    payload = json.dumps(
        [{"path": n.path, "title": n.title, "isolated": n.isolated} for n in nodes]
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
