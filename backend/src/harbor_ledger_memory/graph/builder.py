"""Build the typed vault graph from explicit links and local folder structure."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, cast

import networkx as nx
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession
from harbor_ledger_memory.catalog.models import Note
from harbor_ledger_memory.services.scan import resolve_target
from harbor_ledger_memory.vault.links import parse_wikilinks


class NodeType(StrEnum):
    """Node types intentionally supported by the Phase 1 graph."""

    ROOT = "Root"
    INDEX = "Index"
    CATEGORY = "Category"
    PROJECT = "Project"
    KNOWLEDGE = "Knowledge"
    LESSON = "Lesson"
    CONTEXT = "Context"
    CURRENT = "Current"
    SOURCE = "Source"
    INGEST_ITEM = "IngestItem"
    FILE = "File"


_EXPLICIT_TYPES = {
    "root": NodeType.ROOT,
    "index": NodeType.INDEX,
    "category": NodeType.CATEGORY,
    "project": NodeType.PROJECT,
    "knowledge": NodeType.KNOWLEDGE,
    "lesson": NodeType.LESSON,
    "lessons": NodeType.LESSON,
    "context": NodeType.CONTEXT,
    "current": NodeType.CURRENT,
    "source": NodeType.SOURCE,
    "sources": NodeType.SOURCE,
    "ingest": NodeType.INGEST_ITEM,
    "ingestitem": NodeType.INGEST_ITEM,
    "ingest_item": NodeType.INGEST_ITEM,
    "file": NodeType.FILE,
}

_LOCATION_TYPES = {
    "category": NodeType.CATEGORY,
    "categories": NodeType.CATEGORY,
    "project": NodeType.PROJECT,
    "projects": NodeType.PROJECT,
    "knowledge": NodeType.KNOWLEDGE,
    "lessons": NodeType.LESSON,
    "lesson": NodeType.LESSON,
    "context": NodeType.CONTEXT,
    "current": NodeType.CURRENT,
    "source": NodeType.SOURCE,
    "sources": NodeType.SOURCE,
    "ingest": NodeType.INGEST_ITEM,
}


class GraphBuilder:
    """Project catalog notes and explicit relationships into a MultiDiGraph."""

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

    def build(self) -> nx.MultiDiGraph[str]:
        notes = [
            note
            for note in self._session.scalars(select(Note).order_by(Note.path)).all()
            if self._path_filter is None or self._path_filter(note.path)
        ]
        known_paths = tuple(note.path for note in notes)
        graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
        for note in notes:
            node_type = infer_node_type(note)
            graph.add_node(
                note.path,
                path=note.path,
                title=note.title,
                node_type=node_type.value,
                type=node_type.value,
                kind=node_kind(node_type),
                status=note.status,
                note_type=note.type,
                summary=note.summary,
            )

        self._add_explicit_links(graph, notes)
        self._add_parent_edges(graph, notes, known_paths)
        self._add_contains_edges(graph, notes)
        return graph

    def _add_explicit_links(
        self, graph: nx.MultiDiGraph[str], notes: Iterable[Note]
    ) -> None:
        for note in notes:
            for link in sorted(note.links, key=lambda value: value.id):
                if not link.explicit or link.resolution_status != "resolved":
                    continue
                target = link.normalized_target
                if target is None or target not in graph:
                    continue
                graph.add_edge(
                    note.path,
                    target,
                    edge_type="links_to",
                    explicit=True,
                    inferred=False,
                    confidence=1.0,
                    weight=1.0,
                    source="wikilink",
                    raw=link.raw,
                )

    def _add_parent_edges(
        self,
        graph: nx.MultiDiGraph[str],
        notes: Iterable[Note],
        known_paths: tuple[str, ...],
    ) -> None:
        for note in notes:
            parent = _parent_target(note)
            if parent is None:
                continue
            resolution = resolve_target(
                parent,
                note.path,
                known_paths,
                ai_prefix=_ai_prefix(known_paths),
            )
            parent_path = resolution.target
            if (
                resolution.status != "resolved"
                or parent_path is None
                or parent_path not in graph
            ):
                continue
            graph.add_edge(
                parent_path,
                note.path,
                edge_type="parent_of",
                explicit=True,
                inferred=False,
                confidence=1.0,
                weight=1.0,
                source="frontmatter.parent",
            )

    def _add_contains_edges(
        self, graph: nx.MultiDiGraph[str], notes: Iterable[Note]
    ) -> None:
        """Connect every note to its closest enclosing ``index.md``.

        A folder index is a useful structural hub even when authors have not
        manually linked every child.  We use only the *nearest* index so nested
        folders remain local clusters instead of producing an ancestor-wide
        mesh.  An index inside a child folder becomes a child of the next index
        above it.
        """
        paths = tuple(sorted(note.path for note in notes))
        index_for_directory = {
            PurePosixPath(path).parent: path
            for path in paths
            if PurePosixPath(path).name.lower() == "index.md"
        }
        for child_path in paths:
            child = PurePosixPath(child_path)
            for directory in (child.parent, *child.parent.parents):
                index_path = index_for_directory.get(directory)
                if index_path is None or index_path == child_path:
                    continue
                graph.add_edge(
                    index_path,
                    child_path,
                    edge_type="contains",
                    explicit=False,
                    inferred=True,
                    confidence=0.75,
                    weight=0.5,
                    source="filesystem.index",
                )
                break


def infer_node_type(note: Note) -> NodeType:
    """Infer a node type, preferring explicit frontmatter over location."""
    if note.type is not None:
        return _EXPLICIT_TYPES.get(_normalize_type(note.type), NodeType.FILE)

    path = PurePosixPath(note.path)
    if path.name.lower() == "index.md":
        return NodeType.ROOT if path.parent.name == "AI" else NodeType.INDEX
    if len(path.parts) > 1:
        return _LOCATION_TYPES.get(path.parts[1].lower(), NodeType.FILE)
    return NodeType.FILE


def node_kind(node_type: NodeType | str) -> str:
    """Return the stable public kind for a node type.

    Unknown values are deliberately mapped to ``file`` rather than leaking an
    implementation-specific value to clients.
    """
    try:
        value = node_type.value if isinstance(node_type, NodeType) else str(node_type)
        return NodeType(value).value.lower()
    except (ValueError, TypeError):
        return NodeType.FILE.value.lower()


def _parent_target(note: Note) -> str | None:
    try:
        loaded: object = json.loads(note.frontmatter_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    values = cast(dict[str, Any], loaded)
    parent = values.get("parent")
    if not isinstance(parent, str) or not parent.strip():
        return None
    parsed = parse_wikilinks(parent)
    return parsed[0].target if parsed else parent.strip()


def _normalize_type(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _ai_prefix(paths: tuple[str, ...]) -> str:
    return PurePosixPath(paths[0]).parts[0] if paths else "AI"


__all__ = ["GraphBuilder", "NodeType", "infer_node_type", "node_kind"]
