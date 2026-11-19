"""Regression: starting on an old schema applies migrations automatically."""

import sqlite3
from pathlib import Path

import pytest
from alembic.util.exc import CommandError
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


def test_unknown_numeric_revision_is_not_assumed_compatible(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog.db"
    database_url = f"sqlite:///{db_path}"
    upgrade_to_head(database_url)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE alembic_version SET version_num = '9999_unrelated'")
        conn.execute("ALTER TABLE api_tokens DROP COLUMN agent_principal")

    with pytest.raises(CommandError, match="9999_unrelated"):
        upgrade_to_head(database_url)
    assert "agent_principal" not in _column_names(db_path, "api_tokens")
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [
            ("9999_unrelated",)
        ]


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


def test_unknown_newer_revision_preserves_revision_and_repairs_token_schema(
    tmp_path: Path,
) -> None:
    """Unknown newer revisions stay put while required additive columns are ensured."""
    db_path = tmp_path / "catalog.db"
    database_url = f"sqlite:///{db_path}"
    upgrade_to_head(database_url)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE alembic_version SET version_num = '0022_agent_fact_relations'"
    )
    conn.execute(
        """INSERT INTO api_tokens
           (name, token_hash, scopes, rules, admin, approve_own_proposals, created_at)
           VALUES ('legacy', ?, '[]', NULL, 0, 0, '2026-11-19T00:00:00Z')""",
        ("a" * 64,),
    )
    conn.execute("ALTER TABLE api_tokens DROP COLUMN agent_principal")
    conn.commit()
    conn.close()

    upgrade_to_head(database_url)
    assert "agent_principal" in _column_names(db_path, "api_tokens")
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT name, token_hash, agent_principal FROM api_tokens"
        ).fetchall() == [("legacy", "a" * 64, None)]

    settings = Settings(vault_path=tmp_path, database_url=f"sqlite:///{db_path}")
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200

    conn = sqlite3.connect(str(db_path))
    try:
        revision = conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        assert revision == "0022_agent_fact_relations"
    finally:
        conn.close()
