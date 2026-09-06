"""Add note_embeddings table for semantic retrieval vectors."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_note_embeddings"
down_revision = "0004_short_term_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "note_embeddings",
        sa.Column("note_path", sa.String(length=512), nullable=False, primary_key=True),
        sa.Column("embedding_blob", sa.Text, nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(["note_path"], ["notes.path"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("note_embeddings")
