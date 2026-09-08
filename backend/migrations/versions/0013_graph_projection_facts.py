"""Add durable scan-time graph projection facts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0013_graph_projection_facts"
down_revision = "0012_token_rules"
branch_labels = None
depends_on = None


def _create() -> None:
    op.create_table(
        "graph_projection_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.String(128), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_graph_projection_versions_created_at", "graph_projection_versions", ["created_at"])
    op.create_index("ix_graph_projection_versions_active", "graph_projection_versions", ["active"])
    op.create_table(
        "graph_node_facts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("version_id", sa.String(64), sa.ForeignKey("graph_projection_versions.id", ondelete="CASCADE"), nullable=False, primary_key=True),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("parent_folder", sa.String(512), nullable=False),
        sa.Column("top_folder", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
    )
    for column in ("version_id", "path", "top_folder", "kind"):
        op.create_index(f"ix_graph_node_facts_{column}", "graph_node_facts", [column])
    op.create_table(
        "graph_edge_facts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("version_id", sa.String(64), sa.ForeignKey("graph_projection_versions.id", ondelete="CASCADE"), nullable=False, primary_key=True),
        sa.Column("source", sa.String(512), nullable=False),
        sa.Column("target", sa.String(512), nullable=False),
        sa.Column("edge_type", sa.String(64), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
    )
    for column in ("version_id", "source", "target", "edge_type"):
        op.create_index(f"ix_graph_edge_facts_{column}", "graph_edge_facts", [column])


def upgrade() -> None:
    # ``create_database`` runs ``Base.metadata.create_all`` before Alembic,
    # so an online upgrade may already have all of this schema.
    if op.get_context().as_sql:
        _create()
        return
    inspector = inspect(op.get_bind())
    if "graph_projection_versions" in inspector.get_table_names():
        return
    _create()


def downgrade() -> None:
    op.drop_table("graph_edge_facts")
    op.drop_table("graph_node_facts")
    op.drop_table("graph_projection_versions")
