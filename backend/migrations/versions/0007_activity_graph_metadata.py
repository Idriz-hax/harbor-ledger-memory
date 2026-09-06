"""Add structured metadata used by graph-referenced activity events."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_activity_graph_metadata"
down_revision = "0006_activity_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "activity_events",
        sa.Column("operation_id", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "activity_events",
        sa.Column("run_id", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "activity_events",
        sa.Column("agent_id", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "activity_events",
        sa.Column("parent_id", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "activity_events",
        sa.Column("graph_refs_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("activity_events", "graph_refs_json")
    op.drop_column("activity_events", "parent_id")
    op.drop_column("activity_events", "agent_id")
    op.drop_column("activity_events", "run_id")
    op.drop_column("activity_events", "operation_id")
