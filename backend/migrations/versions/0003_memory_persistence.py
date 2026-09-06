"""Add short-term memory and adaptive edge tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_memory_persistence"
down_revision = "0002_query_traces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "short_term_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_uuid", sa.String(length=36), nullable=False, unique=True),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("selected_paths", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "adaptive_edges",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_path", sa.String(length=512), nullable=False),
        sa.Column("target_path", sa.String(length=512), nullable=False),
        sa.Column("edge_type", sa.String(length=32), nullable=False),
        sa.Column("weight_delta", sa.Float(), nullable=False),
        sa.Column("last_updated", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_path", "target_path", "edge_type"),
    )

    op.create_index(
        "ix_short_term_events_trace_uuid", "short_term_events", ["trace_uuid"]
    )


def downgrade() -> None:
    op.drop_index("ix_short_term_events_trace_uuid", table_name="short_term_events")
    op.drop_table("adaptive_edges")
    op.drop_table("short_term_events")
