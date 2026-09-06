"""Add query activation trace tables for retrieval observability."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_query_traces"
down_revision = "0001_initial_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "query_traces",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_uuid", sa.String(length=36), nullable=False, unique=True),
        sa.Column("created_at", sa.String(length=128), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("active_project", sa.String(length=256), nullable=True),
        sa.Column("retrieval_settings", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="pending"
        ),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "activation_visits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "trace_id",
            sa.Integer(),
            sa.ForeignKey("query_traces.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("activation_score", sa.Float(), nullable=False),
        sa.Column("hop", sa.Integer(), nullable=False),
        sa.Column("via_path", sa.String(length=512), nullable=True),
        sa.Column("edge_type", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "context_selections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "trace_id",
            sa.Integer(),
            sa.ForeignKey("query_traces.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("retrieval_score", sa.Float(), nullable=False),
        sa.Column("activation_score", sa.Float(), nullable=False),
        sa.Column("reasons", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index("ix_query_traces_trace_uuid", "query_traces", ["trace_uuid"])


def downgrade() -> None:
    op.drop_index("ix_query_traces_trace_uuid", table_name="query_traces")
    op.drop_table("context_selections")
    op.drop_table("activation_visits")
    op.drop_table("query_traces")
