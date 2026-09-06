"""Add a SQL-backed short-term selection cache."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_short_term_cache"
down_revision = "0003_memory_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "short_term_entries",
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("last_selected_at", sa.String(length=128), nullable=False),
        sa.Column("selection_count", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["path"], ["notes.path"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("path"),
    )
    op.create_index(
        "ix_short_term_entries_last_selected_at",
        "short_term_entries",
        ["last_selected_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_short_term_entries_last_selected_at", table_name="short_term_entries"
    )
    op.drop_table("short_term_entries")
