"""Upgrade the catalog database with packaged Alembic migrations.

Handles two scenarios:
1. **Alembic-tracked databases**: ``upgrade head`` runs outstanding migrations.
2. **SQLAlchemy create_all() databases** (no alembic_version table): the
   ``activity_events`` table may lack the metadata columns added by migration
   0007.  We add the missing columns directly and then stamp the version so
   future upgrades work via Alembic normally.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text

from harbor_ledger_memory.catalog.database import resolve_database_url
from harbor_ledger_memory.catalog.models import Base

# Columns added by migration 0007_activity_graph_metadata
_ACTIVITY_METADATA_COLUMNS = {
    "operation_id": "VARCHAR(256)",
    "run_id": "VARCHAR(256)",
    "agent_id": "VARCHAR(256)",
    "parent_id": "VARCHAR(256)",
    "graph_refs_json": "TEXT",
}


def _alembic_ini_path() -> Path | None:
    """Return the path to the packaged alembic.ini."""
    # 1. Installed wheel — packaged inside the module
    _res = resources.files("harbor_ledger_memory").joinpath(
        "migrations",
        "alembic.ini",
    )
    _pkg_ini = Path(str(_res))
    if _pkg_ini.is_file():
        return _pkg_ini

    # 2. Source checkout — backend/alembic.ini
    _src = Path(__file__).resolve()
    for candidate in _src.parents:
        ini = candidate / "alembic.ini"
        if ini.is_file():
            return ini

    return Path("alembic.ini") if Path("alembic.ini").is_file() else None


def _column_names(engine: Engine, table: str) -> set[str]:
    """Return the set of column names for *table*."""
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return {row[1] for row in rows}


def _add_missing_activity_columns(engine: Engine) -> None:
    """Add the 0007 metadata columns to activity_events if missing."""
    if not inspect(engine).has_table("activity_events"):
        return
    existing = _column_names(engine, "activity_events")
    missing = {
        col: dtype
        for col, dtype in _ACTIVITY_METADATA_COLUMNS.items()
        if col not in existing
    }
    if not missing:
        return
    with engine.begin() as conn:
        for col, dtype in missing.items():
            conn.execute(text(f"ALTER TABLE activity_events ADD COLUMN {col} {dtype}"))


def _add_missing_query_trace_scope_columns(engine: Engine) -> None:
    """Bring an untracked pre-M4 create_all catalog to the current trace shape."""
    if not inspect(engine).has_table("query_traces"):
        return
    existing = _column_names(engine, "query_traces")
    missing = {
        "scope_kind": "VARCHAR(32)",
        "scope_id": "VARCHAR(128)",
    }
    with engine.begin() as conn:
        for column, dtype in missing.items():
            if column not in existing:
                conn.execute(
                    text(f"ALTER TABLE query_traces ADD COLUMN {column} {dtype}")
                )


def _add_missing_proposal_columns(engine: Engine) -> None:
    """Bring an untracked proposal table to the current additive shape."""
    if not inspect(engine).has_table("memory_write_proposals"):
        return
    if "base_diff" in _column_names(engine, "memory_write_proposals"):
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE memory_write_proposals ADD COLUMN base_diff TEXT")
        )


def _has_alembic_version(engine: Engine) -> bool:
    """Check whether the alembic_version table exists."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='alembic_version'"
            )
        )
        return result.fetchone() is not None


def upgrade_to_head(database_url: str) -> None:
    """Apply all outstanding Alembic migrations to *database_url*.

    Safe to call repeatedly; Alembic skips migrations that are already applied.
    No-op if no packaged migrations are found (e.g. development without build).
    """
    database_url = resolve_database_url(database_url)
    parsed = database_url.rsplit("/", 1)[-1]
    if parsed not in (":memory:", "") and database_url.startswith("sqlite:"):
        from sqlalchemy.engine import make_url

        database = make_url(database_url).database
        if database:
            Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    ini = _alembic_ini_path()
    if ini is None or not ini.is_file():
        return

    cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", database_url)
    # The alembic.ini lives inside the migrations directory itself,
    # so %(here)s already points to the correct script location.
    # For source checkout (ini at backend/alembic.ini), we override.
    if "migrations" in str(ini.parent).lower():
        cfg.set_main_option("script_location", str(ini.parent))
    else:
        cfg.set_main_option("script_location", str(ini.parent / "migrations"))
    _src_dir = ini.parent / "src"
    if _src_dir.is_dir():
        cfg.set_main_option("prepend_sys_path", str(_src_dir))

    engine = create_engine(database_url)
    try:
        # If the database was created by SQLAlchemy create_all() (no Alembic
        # tracking), the activity_events table may lack the 0007 metadata
        # columns.  Add them directly, then stamp the version to head so that
        # Alembic upgrade skips the table-creating migrations.
        if not _has_alembic_version(engine):
            _add_missing_activity_columns(engine)
            _add_missing_query_trace_scope_columns(engine)
            _add_missing_proposal_columns(engine)
            Base.metadata.create_all(engine)
            command.stamp(cfg, "head")
            return
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")


__all__ = ["upgrade_to_head"]
