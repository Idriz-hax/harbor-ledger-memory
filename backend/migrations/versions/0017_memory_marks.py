"""Add scoped explicit memory marks."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0017_memory_marks"
down_revision = "0016_live_traversal_and_token_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().as_sql:
        op.execute("ALTER TABLE query_traces ADD COLUMN scope_kind VARCHAR(32)")
        op.execute("ALTER TABLE query_traces ADD COLUMN scope_id VARCHAR(128)")
        op.execute(
            "CREATE INDEX ix_query_traces_scope_kind ON query_traces (scope_kind)"
        )
        op.execute("CREATE INDEX ix_query_traces_scope_id ON query_traces (scope_id)")
        op.execute(
            """CREATE TABLE memory_marks (
                id INTEGER NOT NULL PRIMARY KEY,
                scope_kind VARCHAR(32) NOT NULL,
                scope_id VARCHAR(128) NOT NULL,
                trace_uuid VARCHAR(36) NOT NULL,
                path VARCHAR(512) NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
                kind VARCHAR(16) NOT NULL,
                created_at VARCHAR(128) NOT NULL,
                expires_at VARCHAR(128),
                revoked_at VARCHAR(128)
            )"""
        )
        for column in (
            "scope_kind",
            "scope_id",
            "trace_uuid",
            "expires_at",
            "revoked_at",
        ):
            op.execute(
                f"CREATE INDEX ix_memory_marks_{column} ON memory_marks ({column})"
            )
        return
    inspector = inspect(op.get_bind())
    trace_columns = {column["name"] for column in inspector.get_columns("query_traces")}
    if "scope_kind" not in trace_columns:
        op.add_column(
            "query_traces", sa.Column("scope_kind", sa.String(length=32), nullable=True)
        )
    if "scope_id" not in trace_columns:
        op.add_column(
            "query_traces", sa.Column("scope_id", sa.String(length=128), nullable=True)
        )
    indexes = {index["name"] for index in inspector.get_indexes("query_traces")}
    if "ix_query_traces_scope_kind" not in indexes:
        op.create_index("ix_query_traces_scope_kind", "query_traces", ["scope_kind"])
    if "ix_query_traces_scope_id" not in indexes:
        op.create_index("ix_query_traces_scope_id", "query_traces", ["scope_id"])
    if inspect(op.get_bind()).has_table("memory_marks"):
        return
    op.create_table(
        "memory_marks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope_kind", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("trace_uuid", sa.String(length=36), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.String(length=128), nullable=True),
        sa.Column("revoked_at", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["path"], ["notes.path"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_marks_scope_kind", "memory_marks", ["scope_kind"])
    op.create_index("ix_memory_marks_scope_id", "memory_marks", ["scope_id"])
    op.create_index("ix_memory_marks_trace_uuid", "memory_marks", ["trace_uuid"])
    op.create_index("ix_memory_marks_expires_at", "memory_marks", ["expires_at"])
    op.create_index("ix_memory_marks_revoked_at", "memory_marks", ["revoked_at"])


def downgrade() -> None:
    op.drop_table("memory_marks")
    op.drop_index("ix_query_traces_scope_id", table_name="query_traces")
    op.drop_index("ix_query_traces_scope_kind", table_name="query_traces")
    op.drop_column("query_traces", "scope_id")
    op.drop_column("query_traces", "scope_kind")
