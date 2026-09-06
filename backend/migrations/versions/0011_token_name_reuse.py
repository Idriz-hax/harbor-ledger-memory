"""Allow api_tokens name reuse after revocation (active names stay unique).

The inline ``UNIQUE (name)`` constraint is an auto-index that SQLite refuses
to ``DROP`` (and that the SQLAlchemy inspector does not report), so the table
is rebuilt without it. A partial unique index keeps active token names
unique, matching the service-level duplicate check.
"""

from __future__ import annotations

from alembic import op

revision = "0011_token_name_reuse"
down_revision = "0010_api_tokens"
branch_labels = None
depends_on = None

_ACTIVE_TABLE = (
    "CREATE TABLE api_tokens_new ("
    "id INTEGER NOT NULL, "
    "name VARCHAR(80) NOT NULL, "
    "token_hash VARCHAR(64) NOT NULL, "
    "scopes TEXT NOT NULL, "
    "created_at VARCHAR(128) NOT NULL, "
    "last_used_at VARCHAR(128), "
    "revoked_at VARCHAR(128), "
    "PRIMARY KEY (id))"
)

_REUSED_TABLE = (
    "CREATE TABLE api_tokens_new ("
    "id INTEGER NOT NULL, "
    "name VARCHAR(80) NOT NULL UNIQUE, "
    "token_hash VARCHAR(64) NOT NULL, "
    "scopes TEXT NOT NULL, "
    "created_at VARCHAR(128) NOT NULL, "
    "last_used_at VARCHAR(128), "
    "revoked_at VARCHAR(128), "
    "PRIMARY KEY (id))"
)


def _rebuild(table_ddl: str) -> None:
    # The rebuild is unconditional and idempotent: on a database already at
    # the target schema it only copies rows into an identical table. Column
    # order matches between both DDL variants. Offline (static SQL) mode has
    # no live connection to introspect, so the same DDL renders there
    # (mirrors 0010_api_tokens).
    op.execute(table_ddl)
    op.execute("INSERT INTO api_tokens_new SELECT * FROM api_tokens")
    op.execute("DROP TABLE api_tokens")
    op.execute("ALTER TABLE api_tokens_new RENAME TO api_tokens")


def upgrade() -> None:
    _rebuild(_ACTIVE_TABLE)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_api_tokens_active_name "
        "ON api_tokens (name) WHERE revoked_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_api_tokens_active_name")
    _rebuild(_REUSED_TABLE)
