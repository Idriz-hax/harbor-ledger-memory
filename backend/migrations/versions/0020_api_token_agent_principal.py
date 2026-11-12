"""Add stable principal metadata to API tokens."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0020_api_token_agent_principal"
down_revision = "0019_relocation_proposal_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # create_all runs before migrations and may already have installed this column.
    if op.get_context().as_sql or "agent_principal" not in {
        column["name"] for column in inspect(op.get_bind()).get_columns("api_tokens")
    }:
        op.add_column(
            "api_tokens", sa.Column("agent_principal", sa.String(128), nullable=True)
        )


def downgrade() -> None:
    if op.get_context().as_sql or "agent_principal" in {
        column["name"] for column in inspect(op.get_bind()).get_columns("api_tokens")
    }:
        op.drop_column("api_tokens", "agent_principal")
