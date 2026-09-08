"""Add opaque graph snapshot handle registry."""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0014_graph_snapshot_handles"
down_revision = "0013_graph_projection_facts"
branch_labels = None
depends_on = None

def _create() -> None:
    op.create_table(
        "graph_snapshot_handles",
        sa.Column("handle", sa.String(128), primary_key=True),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("policy_fingerprint", sa.String(128), nullable=False),
        sa.Column("scope_fingerprint", sa.String(128), nullable=False),
        sa.Column("offset", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.String(128), nullable=False),
    )
    for column in ("version_id", "expires_at"):
        op.create_index(f"ix_graph_snapshot_handles_{column}", "graph_snapshot_handles", [column])

def upgrade() -> None:
    if op.get_context().as_sql:
        _create()
        return
    bind = op.get_bind()
    inspector = inspect(bind)
    if "graph_snapshot_handles" not in inspector.get_table_names():
        _create()
        return
    columns = {column["name"] for column in inspector.get_columns("graph_snapshot_handles")}
    if "level" not in columns:
        op.add_column("graph_snapshot_handles", sa.Column("level", sa.Integer(), nullable=False, server_default="0"))
    indexes = {index["name"] for index in inspector.get_indexes("graph_snapshot_handles")}
    for column in ("version_id", "expires_at"):
        name = f"ix_graph_snapshot_handles_{column}"
        if name not in indexes:
            op.create_index(name, "graph_snapshot_handles", [column])

def downgrade() -> None:
    op.drop_table("graph_snapshot_handles")
