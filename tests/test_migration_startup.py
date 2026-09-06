"""Regression: starting on an old schema applies migrations automatically."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.config import Settings
from harbor_ledger_memory.migrate import upgrade_to_head

_ACTIVITY_METADATA_COLUMNS = (
    "operation_id",
    "run_id",
    "agent_id",
    "parent_id",
    "graph_refs_json",
)


def _column_names(db_path: Path, table: str) -> set[str]:
    """Return the set of column names for *table* in *db_path*."""
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _make_old_schema(db_path: Path) -> None:
    """Create a catalog with the pre-0007 activity_events schema (no metadata)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE activity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type VARCHAR(64) NOT NULL,
            created_at VARCHAR(128) NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def test_upgrade_to_head_adds_activity_metadata_columns(tmp_path: Path) -> None:
    """upgrade_to_head adds the 0007 metadata columns to an old schema."""
    db_path = tmp_path / "catalog.db"
    _make_old_schema(db_path)
    assert "operation_id" not in _column_names(db_path, "activity_events")

    upgrade_to_head(f"sqlite:///{db_path}")

    cols = _column_names(db_path, "activity_events")
    for col in _ACTIVITY_METADATA_COLUMNS:
        assert col in cols, f"column {col} missing after migration"


def test_startup_applies_migrations_for_activity_metadata(tmp_path: Path) -> None:
    """Starting the app on an old schema runs the lifespan Alembic upgrade."""
    db_path = tmp_path / "catalog.db"
    _make_old_schema(db_path)

    (tmp_path / "AI").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{db_path}",
    )

    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200

    cols = _column_names(db_path, "activity_events")
    for col in _ACTIVITY_METADATA_COLUMNS:
        assert col in cols, f"column {col} missing after startup migration"
