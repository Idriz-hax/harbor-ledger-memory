"""Store the safe proposal-time source diff."""

import sqlalchemy as sa
from alembic import op

revision = "0018_proposal_base_diff"
down_revision = "0017_memory_marks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_write_proposals",
        sa.Column("base_diff", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("memory_write_proposals", "base_diff")
