"""Persist filesystem identities affected by memory-write proposals."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0008_memory_write_proposal_paths"
# The repository already has the later graph/snapshot branch at this point;
# include its head so applications still have one migration head.  The
# proposal table itself originates at 0007_memory_write_proposals.
down_revision = ("0007_memory_write_proposals", "0015_snapshot_handle_level")
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().as_sql:
        _add_columns()
        return
    columns = {
        column["name"]
        for column in inspect(op.get_bind()).get_columns("memory_write_proposals")
    }
    if "affected_paths_json" not in columns:
        op.add_column(
            "memory_write_proposals",
            sa.Column("affected_paths_json", sa.Text(), nullable=False, server_default="[]"),
        )
    if "created_paths_json" not in columns:
        op.add_column(
            "memory_write_proposals",
            sa.Column("created_paths_json", sa.Text(), nullable=False, server_default="[]"),
        )


def _add_columns() -> None:
    op.add_column(
        "memory_write_proposals",
        sa.Column("affected_paths_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "memory_write_proposals",
        sa.Column("created_paths_json", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    if op.get_context().as_sql:
        op.drop_column("memory_write_proposals", "created_paths_json")
        op.drop_column("memory_write_proposals", "affected_paths_json")
        return
    columns = {
        column["name"]
        for column in inspect(op.get_bind()).get_columns("memory_write_proposals")
    }
    if "created_paths_json" in columns:
        op.drop_column("memory_write_proposals", "created_paths_json")
    if "affected_paths_json" in columns:
        op.drop_column("memory_write_proposals", "affected_paths_json")
