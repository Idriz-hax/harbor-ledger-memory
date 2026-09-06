import json
from pathlib import Path, PurePosixPath

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import (
    CatalogSession,
    create_database,
    persist_parsed_note,
    rebuild_catalog,
    search_fts,
)
from harbor_ledger_memory.catalog.models import Diagnostic, Link, Note, ScanRun
from harbor_ledger_memory.domain.models import (
    Frontmatter,
    ParseDiagnostic,
    ParsedNote,
    Wikilink,
)


def make_session(tmp_path: Path) -> tuple[Engine, Session]:
    database_path = tmp_path / "catalog.db"
    engine = create_database(f"sqlite:///{database_path}")
    return engine, CatalogSession(bind=engine)


def run_migration(tmp_path: Path, *, offline: bool = False) -> Config:
    database_path = tmp_path / "migration.db"
    config = Config(str(Path(__file__).parents[1] / "backend" / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "head", sql=offline)
    return config


def test_note_fts_returns_indexed_content(tmp_path: Path) -> None:
    engine, session = make_session(tmp_path)
    try:
        session.add(
            Note(
                path="AI/Knowledge/cache.md",
                title="Caching",
                content="cache invalidation",
                content_hash="a",
            )
        )
        session.commit()

        assert search_fts(session, "invalidation")[0].path == ("AI/Knowledge/cache.md")
    finally:
        session.close()
        engine.dispose()


def test_note_fts_update_and_delete_follow_note_transaction(tmp_path: Path) -> None:
    engine, session = make_session(tmp_path)
    try:
        note = Note(path="AI/old.md", title="Old", content="before", content_hash="a")
        session.add(note)
        session.commit()

        note.content = "after change"
        session.commit()
        assert search_fts(session, "before") == []
        assert search_fts(session, "after")[0].path == "AI/old.md"

        session.delete(note)
        session.commit()
        assert search_fts(session, "after") == []
    finally:
        session.close()
        engine.dispose()


def test_persist_parsed_note_serializes_identity_metadata_and_links(
    tmp_path: Path,
) -> None:
    engine, session = make_session(tmp_path)
    try:
        parsed = ParsedNote(
            path=PurePosixPath("AI/Knowledge/cache.md"),
            content="cache invalidation",
            frontmatter=Frontmatter(
                type="knowledge",
                status="active",
                created="2026-01-01",
                updated="2026-01-02",
                summary="Cache notes",
                extra={"custom": "value"},
            ),
            wikilinks=(
                Wikilink(
                    raw="[[AI/INDEX|Index]]",
                    target="AI/INDEX",
                    alias="Index",
                ),
            ),
            diagnostics=(ParseDiagnostic(code="note.warning", message="test warning"),),
        )

        note = persist_parsed_note(
            session,
            parsed,
            content_hash="hash",
            file_size=17,
            file_mtime_ns=123,
            scan_timestamp="2026-01-03T00:00:00+00:00",
        )
        session.commit()

        assert note.path == "AI/Knowledge/cache.md"
        assert note.title == "cache"
        assert note.note_type == "knowledge"
        assert note.file_size == 17
        assert note.file_mtime_ns == 123
        assert json.loads(note.frontmatter_json)["extra"]["custom"] == "value"
        assert session.scalars(select(Link)).one().normalized_target == "AI/INDEX"
        assert session.scalars(select(Diagnostic)).one().path == note.path
    finally:
        session.close()
        engine.dispose()


def test_rebuild_catalog_only_clears_catalog_tables(tmp_path: Path) -> None:
    engine, session = make_session(tmp_path)
    try:
        session.add(
            Note(path="AI/note.md", title="Note", content="body", content_hash="a")
        )
        session.add(
            Link(
                source_path="AI/note.md",
                raw="[[missing]]",
                normalized_target="missing",
                explicit=True,
                resolution_status="broken",
            )
        )
        session.commit()
        rebuild_catalog(session)

        assert session.scalars(select(Note)).all() == []
        assert session.scalars(select(Link)).all() == []
        assert session.execute(text("SELECT count(*) FROM note_fts")).scalar_one() == 0
    finally:
        session.close()
        engine.dispose()


def test_catalog_schema_contains_application_tables(tmp_path: Path) -> None:
    engine, session = make_session(tmp_path)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"notes", "links", "scan_runs", "diagnostics", "note_fts"} <= tables
    finally:
        session.close()
        engine.dispose()


def test_migration_indexes_match_catalog_orm_indexes(tmp_path: Path) -> None:
    run_migration(tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        indexes = {
            (index["name"], tuple(index["column_names"]), index["unique"])
            for table in ("links", "diagnostics")
            for index in inspect(engine).get_indexes(table)
        }
        assert ("ix_links_source_path", ("source_path",), 0) in indexes
        assert ("ix_diagnostics_path", ("path",), 0) in indexes
    finally:
        engine.dispose()


def test_sqlite_foreign_keys_are_enforced_for_application_connections(
    tmp_path: Path,
) -> None:
    engine, session = make_session(tmp_path)
    try:
        assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1

        session.add(Note(path="AI/note.md", title="Note", content="body"))
        session.flush()
        scan_run = ScanRun(
            started_at="2026-01-01T00:00:00+00:00",
            status="complete",
            notes_indexed=1,
            diagnostics_count=1,
        )
        session.add(scan_run)
        session.flush()
        session.add(
            Link(
                source_path="AI/note.md",
                raw="[[target]]",
                explicit=True,
                resolution_status="unresolved",
            )
        )
        session.add(
            Diagnostic(
                path="AI/note.md",
                scan_run_id=scan_run.id,
                code="test",
                message="warning",
                severity="warning",
            )
        )
        session.commit()

        session.execute(text("DELETE FROM scan_runs"))
        assert (
            session.execute(text("SELECT scan_run_id FROM diagnostics")).scalar_one()
            is None
        )
        session.execute(text("DELETE FROM notes WHERE path = 'AI/note.md'"))
        assert session.execute(text("SELECT count(*) FROM links")).scalar_one() == 0
        assert (
            session.execute(text("SELECT count(*) FROM diagnostics")).scalar_one() == 0
        )
    finally:
        session.close()
        engine.dispose()


def test_online_migration_enables_foreign_keys_and_declared_actions(
    tmp_path: Path,
) -> None:
    run_migration(tmp_path)
    engine = create_database(f"sqlite:///{tmp_path / 'migration.db'}")
    session = CatalogSession(bind=engine)
    try:
        assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1

        session.add(Note(path="AI/migrated.md", title="Migrated", content="body"))
        session.add(
            ScanRun(
                started_at="2026-01-01T00:00:00+00:00",
                status="complete",
                notes_indexed=1,
                diagnostics_count=1,
            )
        )
        session.flush()
        scan_run = session.scalars(select(ScanRun)).one()
        session.add(
            Link(
                source_path="AI/migrated.md",
                raw="[[target]]",
                explicit=True,
                resolution_status="unresolved",
            )
        )
        session.add(
            Diagnostic(
                path="AI/migrated.md",
                scan_run_id=scan_run.id,
                code="test",
                message="warning",
                severity="warning",
            )
        )
        session.commit()

        session.execute(text("DELETE FROM scan_runs"))
        assert (
            session.execute(text("SELECT scan_run_id FROM diagnostics")).scalar_one()
            is None
        )
        session.execute(text("DELETE FROM notes WHERE path = 'AI/migrated.md'"))
        assert session.execute(text("SELECT count(*) FROM links")).scalar_one() == 0
        assert (
            session.execute(text("SELECT count(*) FROM diagnostics")).scalar_one() == 0
        )
    finally:
        session.close()
        engine.dispose()


def test_migration_offline_sql_does_not_require_a_runtime_connection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_migration(tmp_path, offline=True)
    output = capsys.readouterr().out
    assert "CREATE VIRTUAL TABLE note_fts" in output
    assert "CREATE INDEX ix_links_source_path" in output


def test_note_embedding_round_trip(tmp_path: Path) -> None:
    import struct

    from harbor_ledger_memory.catalog.models import NoteEmbedding

    engine, session = make_session(tmp_path)
    try:
        # Seed a parent note so the FK constraint is satisfied
        session.add(
            Note(
                path="AI/Knowledge/test.md",
                title="Test",
                content="placeholder",
                content_hash="x",
            )
        )
        session.commit()

        # Create a fake 384-dim embedding
        embedding = struct.pack("<384f", *[0.1] * 384)
        now = "2026-08-09T12:00:00"

        session.add(
            NoteEmbedding(
                note_path="AI/Knowledge/test.md",
                embedding_blob=embedding,
                model_name="all-MiniLM-L6-v2",
                created_at=now,
            )
        )
        session.commit()

        result = (
            session.query(NoteEmbedding)
            .filter_by(note_path="AI/Knowledge/test.md")
            .first()
        )
        assert result is not None
        assert result.embedding_blob == embedding
        assert result.model_name == "all-MiniLM-L6-v2"
    finally:
        session.close()
        engine.dispose()
