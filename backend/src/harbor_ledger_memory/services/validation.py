"""Deterministic validation findings for the derived AI catalog."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, cast

import networkx as nx
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession
from harbor_ledger_memory.catalog.models import Link, Note
from harbor_ledger_memory.graph.builder import GraphBuilder


@dataclass(frozen=True)
class ValidationFinding:
    """A proposed diagnostic; validation never edits the vault."""

    code: str
    severity: str
    path: str | None
    message: str
    evidence: Mapping[str, Any] = field(default_factory=lambda: dict[str, Any]())


@dataclass(frozen=True)
class ValidationReport:
    """Stable validation output sorted by finding identity."""

    findings: tuple[ValidationFinding, ...]

    @property
    def errors(self) -> tuple[ValidationFinding, ...]:
        return tuple(
            finding for finding in self.findings if finding.severity == "error"
        )

    @property
    def warnings(self) -> tuple[ValidationFinding, ...]:
        return tuple(
            finding for finding in self.findings if finding.severity == "warning"
        )


class ValidationService:
    """Check catalog consistency and routing conventions without vault writes."""

    def __init__(
        self, session: Session | Engine, graph_builder: GraphBuilder | None = None
    ) -> None:
        self._session = (
            CatalogSession(bind=session) if isinstance(session, Engine) else session
        )
        self._graph_builder = graph_builder

    def run(self) -> ValidationReport:
        notes = list(self._session.scalars(select(Note).order_by(Note.path)).all())
        links = list(
            self._session.scalars(
                select(Link).order_by(Link.source_path, Link.id)
            ).all()
        )
        graph = (self._graph_builder or GraphBuilder(self._session)).build()
        findings: list[ValidationFinding] = []
        findings.extend(_link_findings(links, notes))
        findings.extend(_index_findings(notes))
        findings.extend(_frontmatter_findings(notes))
        findings.extend(_parent_findings(notes, links, graph))
        findings.extend(_orphan_findings(notes, graph))
        findings.extend(_duplicate_path_findings(notes))
        findings.extend(_short_term_findings(notes, links))
        return ValidationReport(tuple(sorted(findings, key=_finding_key)))


def _link_findings(links: list[Link], notes: list[Note]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    note_types = {note.path: (note.type or "").strip().lower() for note in notes}
    for link in links:
        if link.resolution_status not in {"broken", "ambiguous"}:
            continue
        code = f"link.{link.resolution_status}"
        findings.append(
            ValidationFinding(
                code=code,
                severity="error",
                path=link.source_path,
                message=f"{link.resolution_status} link: {link.raw}",
                evidence={
                    "raw": link.raw,
                    "target": link.normalized_target,
                    "resolution_status": link.resolution_status,
                },
            )
        )
        if _is_index_path(link.source_path, note_types):
            findings.append(
                ValidationFinding(
                    code="index.link.missing",
                    severity="error",
                    path=link.source_path,
                    message=f"index route does not resolve: {link.raw}",
                    evidence={"raw": link.raw, "target": link.normalized_target},
                )
            )
    return findings


def _index_findings(notes: list[Note]) -> list[ValidationFinding]:
    by_directory: dict[str, list[Note]] = defaultdict(list)
    for note in notes:
        path = PurePosixPath(note.path)
        if path.name.lower() in {"index.md", "agents.md", "short-term.md"}:
            continue
        by_directory[path.parent.as_posix()].append(note)

    findings: list[ValidationFinding] = []
    note_paths = {note.path for note in notes}
    for directory, content in sorted(by_directory.items()):
        index_path = f"{directory}/INDEX.md"
        if index_path in note_paths or f"{directory}/index.md" in note_paths:
            continue
        findings.append(
            ValidationFinding(
                code="index.missing",
                severity="error",
                path=directory,
                message="content directory has no INDEX.md",
                evidence={
                    "directory": directory,
                    "content_paths": sorted(n.path for n in content),
                },
            )
        )
    return findings


def _frontmatter_findings(notes: list[Note]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    required = (
        "type",
        "status",
        "tags",
        "created",
        "updated",
        "summary",
        "parent",
        "graph_color",
    )
    for note in notes:
        path = PurePosixPath(note.path)
        if path.name.lower() in {"index.md", "agents.md", "short-term.md"}:
            continue
        if note.status != "active":
            continue
        values = _frontmatter(note)
        missing = [field for field in required if not _present(values, field)]
        if not missing:
            continue
        findings.append(
            ValidationFinding(
                code="frontmatter.missing",
                severity="error",
                path=note.path,
                message="active note is missing required frontmatter",
                evidence={"missing": missing},
            )
        )
    return findings


def _parent_findings(
    notes: list[Note], links: list[Link], graph: nx.MultiDiGraph[str]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    links_by_source: dict[str, list[Link]] = defaultdict(list)
    for link in links:
        links_by_source[link.source_path].append(link)
    for note in notes:
        if note.status != "active" or _is_special(note.path):
            continue
        values = _frontmatter(note)
        parent = values.get("parent")
        if not isinstance(parent, str) or not parent.strip():
            continue
        parent_edges = [
            source
            for source, _, data in graph.in_edges(note.path, data=True)
            if data.get("edge_type") == "parent_of"
        ]
        if not parent_edges:
            findings.append(
                ValidationFinding(
                    code="parent.unresolved",
                    severity="error",
                    path=note.path,
                    message="active note parent does not resolve",
                    evidence={"parent": parent},
                )
            )
            continue
        parent_path = sorted(parent_edges)[0]
        linked_children = {
            link.normalized_target
            for link in links_by_source[parent_path]
            if link.resolution_status == "resolved"
        }
        if note.path not in linked_children:
            findings.append(
                ValidationFinding(
                    code="parent.index.missing",
                    severity="error",
                    path=note.path,
                    message="active child is missing from its parent index",
                    evidence={"parent": parent_path},
                )
            )
    return findings


def _orphan_findings(
    notes: list[Note], graph: nx.MultiDiGraph[str]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for note in notes:
        if note.status != "active" or _is_special(note.path):
            continue
        if PurePosixPath(note.path).name.lower() == "index.md":
            continue
        incoming = [
            data
            for _, _, data in graph.in_edges(note.path, data=True)
            if data.get("edge_type") in {"links_to", "parent_of"}
        ]
        if incoming:
            continue
        findings.append(
            ValidationFinding(
                code="note.orphan",
                severity="warning",
                path=note.path,
                message="active note has no explicit graph parent or incoming link",
                evidence={"path": note.path},
            )
        )
    return findings


def _duplicate_path_findings(notes: list[Note]) -> list[ValidationFinding]:
    groups: dict[str, list[str]] = defaultdict(list)
    for note in notes:
        groups[PurePosixPath(note.path).as_posix().casefold()].append(note.path)
    return [
        ValidationFinding(
            code="path.duplicate",
            severity="error",
            path=paths[0],
            message="multiple notes share a normalized path",
            evidence={"paths": sorted(paths)},
        )
        for paths in groups.values()
        if len(paths) > 1
    ]


def _short_term_findings(
    notes: list[Note], links: list[Link]
) -> list[ValidationFinding]:
    statuses = {note.path: note.status for note in notes}
    short_term = next(
        (
            note.path
            for note in notes
            if PurePosixPath(note.path).name == "SHORT-TERM.md"
        ),
        None,
    )
    if short_term is None:
        return []
    findings: list[ValidationFinding] = []
    for link in links:
        if link.source_path != short_term:
            continue
        target_is_active = (
            link.normalized_target is not None
            and statuses.get(link.normalized_target) == "active"
        )
        if link.resolution_status != "resolved" or not target_is_active:
            findings.append(
                ValidationFinding(
                    code="short-term.stale",
                    severity="warning",
                    path=short_term,
                    message="SHORT-TERM contains a stale route",
                    evidence={"raw": link.raw, "status": link.resolution_status},
                )
            )
    return findings


def _is_index_path(path: str, note_types: Mapping[str, str]) -> bool:
    return PurePosixPath(path).name.lower() == "index.md" or note_types.get(path) in {
        "index",
        "root",
    }


def _frontmatter(note: Note) -> dict[str, Any]:
    try:
        values: object = json.loads(note.frontmatter_json)
    except (TypeError, ValueError):
        return {}
    return cast(dict[str, Any], values) if isinstance(values, dict) else {}


def _present(values: Mapping[str, Any], key: str) -> bool:
    value = values.get(key)
    return value is not None and value != "" and value != []


def _is_special(path: str) -> bool:
    return PurePosixPath(path).name.lower() in {
        "index.md",
        "agents.md",
        "short-term.md",
    }


def _finding_key(finding: ValidationFinding) -> tuple[str, str, str, str]:
    return (
        finding.code,
        finding.path or "",
        finding.message,
        repr(sorted(finding.evidence.items())),
    )


__all__ = ["ValidationFinding", "ValidationReport", "ValidationService"]
