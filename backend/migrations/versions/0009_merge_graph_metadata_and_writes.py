"""Merge graph-metadata and write-proposal branches, add write lifecycle columns."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_merge_graph_metadata_and_writes"
down_revision = (
    "0007_activity_graph_metadata",
    "0008_activation_visit_edges",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_write_proposals",
        sa.Column("expected_source_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "memory_write_proposals",
        sa.Column("applying_at", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "memory_write_proposals",
        sa.Column("applied_content_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("memory_write_proposals", "applied_content_hash")
    op.drop_column("memory_write_proposals", "applying_at")
    op.drop_column("memory_write_proposals", "expected_source_hash")
