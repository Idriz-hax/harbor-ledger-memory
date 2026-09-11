"""Store token approval scope and proposal creator token."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision = "0016_live_traversal_and_token_approval"
down_revision = "0008_memory_write_proposal_paths"
branch_labels = None
depends_on = None

_CREATOR_FK = "fk_memory_write_proposals_creator_token_id_api_tokens"


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def _has_creator_fk() -> bool:
    return any(
        foreign_key.get("name") == _CREATOR_FK
        for foreign_key in inspect(op.get_bind()).get_foreign_keys(
            "memory_write_proposals"
        )
    )


def upgrade() -> None:
    if op.get_context().as_sql:
        op.add_column(
            "api_tokens",
            sa.Column(
                "approve_own_proposals",
                sa.Boolean(),
                nullable=False,
                server_default=text("0"),
            ),
        )
        op.add_column(
            "memory_write_proposals",
            sa.Column("creator_token_id", sa.Integer(), nullable=True),
        )
        with op.batch_alter_table("memory_write_proposals") as batch_op:
            batch_op.create_foreign_key(
                _CREATOR_FK,
                "api_tokens",
                ["creator_token_id"],
                ["id"],
                ondelete="SET NULL",
            )
        return
    if "approve_own_proposals" not in _columns("api_tokens"):
        op.add_column(
            "api_tokens",
            sa.Column(
                "approve_own_proposals",
                sa.Boolean(),
                nullable=False,
                server_default=text("0"),
            ),
        )
    if "creator_token_id" not in _columns("memory_write_proposals"):
        op.add_column(
            "memory_write_proposals",
            sa.Column("creator_token_id", sa.Integer(), nullable=True),
        )
    if not _has_creator_fk():
        with op.batch_alter_table("memory_write_proposals") as batch_op:
            batch_op.create_foreign_key(
                _CREATOR_FK,
                "api_tokens",
                ["creator_token_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    if op.get_context().as_sql:
        op.drop_constraint(
            _CREATOR_FK,
            "memory_write_proposals",
            type_="foreignkey",
        )
        op.drop_column("memory_write_proposals", "creator_token_id")
        op.drop_column("api_tokens", "approve_own_proposals")
        return
    if _has_creator_fk():
        with op.batch_alter_table("memory_write_proposals") as batch_op:
            batch_op.drop_constraint(_CREATOR_FK, type_="foreignkey")
    if "creator_token_id" in _columns("memory_write_proposals"):
        op.drop_column("memory_write_proposals", "creator_token_id")
    if "approve_own_proposals" in _columns("api_tokens"):
        op.drop_column("api_tokens", "approve_own_proposals")
