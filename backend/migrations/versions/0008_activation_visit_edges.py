"""Add structural edge endpoints to activation visits."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_activation_visit_edges"
down_revision = "0007_memory_write_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "activation_visits",
        sa.Column("edge_source", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "activation_visits",
        sa.Column("edge_target", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("activation_visits", "edge_target")
    op.drop_column("activation_visits", "edge_source")
