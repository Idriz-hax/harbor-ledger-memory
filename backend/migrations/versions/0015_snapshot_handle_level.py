"""Bind graph snapshot handles to aggregation level."""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0015_snapshot_handle_level"
down_revision = "0014_graph_snapshot_handles"
branch_labels = None
depends_on = None

def upgrade() -> None:
    if op.get_context().as_sql:
        op.add_column("graph_snapshot_handles", sa.Column("level", sa.Integer(), nullable=False, server_default="0"))
        return
    if "level" not in {column["name"] for column in inspect(op.get_bind()).get_columns("graph_snapshot_handles")}:
        op.add_column("graph_snapshot_handles", sa.Column("level", sa.Integer(), nullable=False, server_default="0"))

def downgrade() -> None:
    op.drop_column("graph_snapshot_handles", "level")
