"""Add the api_tokens table for token-based access control."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0010_api_tokens"
down_revision = "0009_merge_graph_metadata_and_writes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``create_database`` runs ``Base.metadata.create_all`` before Alembic
    # applies this migration, so a database upgrading across it may already
    # contain the table. Skip the DDL when present; the schema is identical
    # either way. Offline (static SQL) mode has no live connection to
    # inspect, so emit the DDL unconditionally there.
    if not op.get_context().as_sql and inspect(op.get_bind()).has_table(
        "api_tokens"
    ):
        return
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=80), nullable=False, unique=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=128), nullable=False),
        sa.Column("last_used_at", sa.String(length=128), nullable=True),
        sa.Column("revoked_at", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("api_tokens")
