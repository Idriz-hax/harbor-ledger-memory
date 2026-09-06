"""Add memory-write proposals table for persistent write lifecycle."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_memory_write_proposals"
down_revision = "0006_activity_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_write_proposals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("rule_access", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.String(length=128), nullable=False),
        sa.Column("resolved_at", sa.String(length=128), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_write_proposals_status",
        "memory_write_proposals",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_memory_write_proposals_path",
        "memory_write_proposals",
        ["path"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_memory_write_proposals_path", table_name="memory_write_proposals")
    op.drop_index(
        "ix_memory_write_proposals_status", table_name="memory_write_proposals"
    )
    op.drop_table("memory_write_proposals")
