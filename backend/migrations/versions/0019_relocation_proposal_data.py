"""Store structured move and delete proposal previews."""

import sqlalchemy as sa
from alembic import op

revision = "0019_relocation_proposal_data"
down_revision = "0018_proposal_base_diff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_write_proposals",
        sa.Column("proposal_data_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("memory_write_proposals", "proposal_data_json")
