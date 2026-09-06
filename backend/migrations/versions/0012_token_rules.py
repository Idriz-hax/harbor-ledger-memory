"""Add per-token folder rules and admin flag to api_tokens."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision = "0012_token_rules"
down_revision = "0011_token_name_reuse"
branch_labels = None
depends_on = None


def _existing_columns() -> set[str]:
    return {
        column["name"]
        for column in inspect(op.get_bind()).get_columns("api_tokens")
    }


def _add_columns() -> None:
    op.add_column(
        "api_tokens",
        sa.Column("rules", sa.Text(), nullable=True),
    )
    op.add_column(
        "api_tokens",
        sa.Column(
            "admin",
            sa.Boolean(),
            nullable=False,
            server_default=text("0"),
        ),
    )


def upgrade() -> None:
    if op.get_context().as_sql:
        _add_columns()
        return
    columns = _existing_columns()
    if "rules" not in columns:
        op.add_column(
            "api_tokens",
            sa.Column("rules", sa.Text(), nullable=True),
        )
    if "admin" not in columns:
        op.add_column(
            "api_tokens",
            sa.Column(
                "admin",
                sa.Boolean(),
                nullable=False,
                server_default=text("0"),
            ),
        )


def downgrade() -> None:
    if op.get_context().as_sql:
        op.drop_column("api_tokens", "admin")
        op.drop_column("api_tokens", "rules")
        return
    columns = _existing_columns()
    if "admin" in columns:
        op.drop_column("api_tokens", "admin")
    if "rules" in columns:
        op.drop_column("api_tokens", "rules")
