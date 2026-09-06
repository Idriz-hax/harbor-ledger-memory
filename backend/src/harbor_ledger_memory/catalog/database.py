"""Database setup and persistence helpers for the disposable catalog."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from sqlalchemy import Engine, create_engine, delete, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from harbor_ledger_memory.catalog.models import (
    Base,
    Diagnostic,
    Link,
    Note,
    ScanRun,
)
from harbor_ledger_memory.domain.models import ParsedNote, Wikilink

CatalogSession = sessionmaker[Session](class_=Session, expire_on_commit=False)

_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5(
    path UNINDEXED,
    title,
    content,
    summary
)
"""
_CREATE_FTS_INSERT_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS notes_fts_insert
AFTER INSERT ON notes
BEGIN
    INSERT INTO note_fts(rowid, path, title, content, summary)
    VALUES (new.id, new.path, new.title, new.content, coalesce(new.summary, ''));
END
"""
_CREATE_FTS_UPDATE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS notes_fts_update
AFTER UPDATE OF path, title, content, summary ON notes
BEGIN
    DELETE FROM note_fts WHERE rowid = old.id;
    INSERT INTO note_fts(rowid, path, title, content, summary)
    VALUES (new.id, new.path, new.title, new.content, coalesce(new.summary, ''));
END
"""
_CREATE_FTS_DELETE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS notes_fts_delete
AFTER DELETE ON notes
BEGIN
    DELETE FROM note_fts WHERE rowid = old.id;
END
"""


def create_database(url: str) -> Engine:
    """Create and initialize a SQLite catalog engine.

    Initialization owns only application tables and the FTS projection. It does
    not inspect, open, or modify a vault path.
    """

    url = resolve_database_url(url)
    parsed_url = make_url(url)
    if parsed_url.get_backend_name() != "sqlite":
        raise ValueError("the catalog requires a SQLite database URL")

    engine_kwargs: dict[str, Any] = {
        "connect_args": {"check_same_thread": False},
    }
    if parsed_url.database in (None, ":memory:"):
        engine_kwargs["poolclass"] = StaticPool
    else:
        Path(parsed_url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True, **engine_kwargs)
    event.listen(engine, "connect", enable_sqlite_foreign_keys)
    Base.metadata.create_all(engine)
    _ensure_fts(engine)
    return engine


def resolve_database_url(url: str, *, base_dir: Path | None = None) -> str:
    """Resolve relative SQLite file URLs from the stable application directory.

    Absolute database URLs and in-memory SQLite URLs are left unchanged.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or parsed.database in (None, ":memory:"):
        return url
    database = Path(parsed.database).expanduser()
    if database.is_absolute():
        return url
    root = base_dir or (Path.home() / ".config" / "harbor-ledger-memory")
    return parsed.set(database=str((root / database).resolve())).render_as_string(
        hide_password=False
    )


def enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
    """Enable SQLite foreign-key enforcement on every new connection."""

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _ensure_fts(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(_CREATE_FTS)
        connection.exec_driver_sql(_CREATE_FTS_INSERT_TRIGGER)
        connection.exec_driver_sql(_CREATE_FTS_UPDATE_TRIGGER)
        connection.exec_driver_sql(_CREATE_FTS_DELETE_TRIGGER)


def persist_parsed_note(
    session: Session,
    parsed: ParsedNote,
    *,
    content_hash: str | None = None,
    file_size: int | None = None,
    file_mtime_ns: int | None = None,
    scan_timestamp: str | date | datetime | None = None,
    resolved_links: Iterable[Link | Wikilink | Mapping[str, Any]] | None = None,
) -> Note:
    """Insert or replace one parsed note without reading from the filesystem."""

    path = _path_string(parsed.path)
    frontmatter = parsed.frontmatter
    note = session.scalar(select(Note).where(Note.path == path))
    if note is None:
        note = Note(path=path)
        session.add(note)

    note.title = (
        parsed.headings[0].text if parsed.headings else PurePosixPath(path).stem
    )
    note.type = frontmatter.type
    note.status = frontmatter.status
    note.summary = frontmatter.summary
    note.frontmatter_json = _serialize_frontmatter(frontmatter)
    note.created = frontmatter.created
    note.updated = frontmatter.updated
    note.content = parsed.content
    note.content_hash = content_hash or ""
    note.file_size = file_size
    note.file_mtime_ns = file_mtime_ns
    note.scan_timestamp = _timestamp_string(scan_timestamp)

    note.links.clear()
    identities: Iterable[Link | Wikilink | Mapping[str, Any]] = (
        parsed.wikilinks if resolved_links is None else resolved_links
    )
    for identity in identities:
        note.links.append(_link_from_identity(identity, path))

    note.diagnostics.clear()
    for diagnostic in parsed.diagnostics:
        note.diagnostics.append(
            Diagnostic(
                code=diagnostic.code,
                message=diagnostic.message,
                severity=diagnostic.severity,
                line=diagnostic.line,
            )
        )
    return note


def search_fts(session: Session, query: str, limit: int = 20) -> list[Note]:
    """Return notes whose indexed text matches a SQLite FTS5 expression."""

    if not query.strip() or limit <= 0:
        return []
    statement = select(Note).from_statement(
        text(
            """
            SELECT notes.*
            FROM notes
            JOIN note_fts ON note_fts.path = notes.path
            WHERE note_fts MATCH :query
            ORDER BY bm25(note_fts), notes.path
            LIMIT :limit
            """
        )
    )
    return list(session.scalars(statement, {"query": query, "limit": limit}))


def rebuild_catalog(bind: Session | Engine) -> None:
    """Clear only catalog-owned rows, leaving all vault files untouched."""

    if isinstance(bind, Engine):
        with CatalogSession(bind=bind) as session:
            clear_catalog(session)
            session.commit()
        return

    clear_catalog(bind)
    bind.commit()


def clear_catalog(session: Session) -> None:
    """Clear catalog-owned rows without committing the surrounding transaction."""
    # Clear FTS explicitly so this remains correct even if a database was
    # created from an older migration without the note delete trigger.
    session.execute(text("DELETE FROM note_fts"))
    session.execute(delete(Link))
    session.execute(delete(Diagnostic))
    session.execute(delete(Note))
    session.execute(delete(ScanRun))


def _path_string(value: PurePosixPath | str) -> str:
    path = PurePosixPath(value)
    return path.as_posix()


def _timestamp_string(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else value.isoformat()


def _serialize_frontmatter(frontmatter: Any) -> str:
    values = {
        "type": frontmatter.type,
        "status": frontmatter.status,
        "tags": list(frontmatter.tags),
        "created": frontmatter.created,
        "updated": frontmatter.updated,
        "summary": frontmatter.summary,
        "parent": frontmatter.parent,
        "graph_color": frontmatter.graph_color,
        "extra": _json_value(dict(frontmatter.extra)),
    }
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[Any, Any], value)
        return {str(key): _json_value(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        sequence = cast(list[Any] | tuple[Any, ...], value)
        return [_json_value(item) for item in sequence]
    if isinstance(value, (set, frozenset)):
        members = cast(set[Any] | frozenset[Any], value)
        return sorted(_json_value(item) for item in members)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _link_from_identity(
    identity: Link | Wikilink | Mapping[str, Any], source_path: str
) -> Link:
    if isinstance(identity, Link):
        return Link(
            source_path=source_path,
            raw=identity.raw,
            normalized_target=identity.normalized_target,
            alias=identity.alias,
            heading=identity.heading,
            block_id=identity.block_id,
            explicit=identity.explicit,
            resolution_status=identity.resolution_status,
        )
    if isinstance(identity, Wikilink):
        return Link(
            source_path=source_path,
            raw=identity.raw,
            normalized_target=identity.target,
            alias=identity.alias,
            heading=identity.heading,
            block_id=identity.block_id,
            explicit=True,
            resolution_status="unresolved",
        )

    values = dict(identity)
    raw = str(values.get("raw", values.get("raw_form", "")))
    target = values.get(
        "normalized_target",
        values.get("target_candidate", values.get("target")),
    )
    return Link(
        source_path=source_path,
        raw=raw,
        normalized_target=None if target is None else str(target),
        alias=_optional_string(values.get("alias")),
        heading=_optional_string(values.get("heading")),
        block_id=_optional_string(values.get("block_id")),
        explicit=bool(values.get("explicit", not bool(values.get("inferred", False)))),
        resolution_status=str(values.get("resolution_status", "unresolved")),
    )


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "CatalogSession",
    "create_database",
    "resolve_database_url",
    "enable_sqlite_foreign_keys",
    "persist_parsed_note",
    "rebuild_catalog",
    "search_fts",
]
