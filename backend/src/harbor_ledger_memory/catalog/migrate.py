"""Migration entry point using the same database URL resolution as runtime."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import make_url

from harbor_ledger_memory.catalog.database import create_database, resolve_database_url
from harbor_ledger_memory.migrate import upgrade_to_head as _upgrade_to_head


def upgrade_to_head(database_url: str) -> None:
    """Upgrade the catalog at the stable resolved SQLite location."""
    resolved = resolve_database_url(database_url)
    parsed = make_url(resolved)
    if parsed.database not in (None, ":memory:"):
        Path(parsed.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    engine = create_database(resolved)
    engine.dispose()
    _upgrade_to_head(resolved)


__all__ = ["upgrade_to_head"]
