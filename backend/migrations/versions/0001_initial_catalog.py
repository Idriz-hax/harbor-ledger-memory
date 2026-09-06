"""Create the rebuildable catalog and transactional FTS5 projection."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0001_initial_catalog"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("type", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=128), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("frontmatter_json", sa.Text(), nullable=False),
        sa.Column("created", sa.String(length=256), nullable=True),
        sa.Column("updated", sa.String(length=256), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("file_mtime_ns", sa.Integer(), nullable=True),
        sa.Column("scan_timestamp", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path"),
    )
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("started_at", sa.String(length=128), nullable=False),
        sa.Column("completed_at", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("notes_indexed", sa.Integer(), nullable=False),
        sa.Column("diagnostics_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_path", sa.String(length=512), nullable=False),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.Column("normalized_target", sa.Text(), nullable=True),
        sa.Column("alias", sa.Text(), nullable=True),
        sa.Column("heading", sa.Text(), nullable=True),
        sa.Column("block_id", sa.String(length=256), nullable=True),
        sa.Column("explicit", sa.Boolean(), nullable=False),
        sa.Column("resolution_status", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["source_path"], ["notes.path"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "diagnostics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("path", sa.String(length=512), nullable=True),
        sa.Column("scan_run_id", sa.Integer(), nullable=True),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["path"], ["notes.path"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scan_run_id"], ["scan_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_links_source_path", "links", ["source_path"], unique=False)
    op.create_index("ix_diagnostics_path", "diagnostics", ["path"], unique=False)
    _execute_sql(
        """
        CREATE VIRTUAL TABLE note_fts USING fts5(
            path UNINDEXED,
            title,
            content,
            summary
        )
        """
    )
    _execute_sql(
        """
        CREATE TRIGGER notes_fts_insert
        AFTER INSERT ON notes
        BEGIN
            INSERT INTO note_fts(rowid, path, title, content, summary)
            VALUES (
                new.id, new.path, new.title, new.content,
                coalesce(new.summary, '')
            );
        END
        """
    )
    _execute_sql(
        """
        CREATE TRIGGER notes_fts_update
        AFTER UPDATE OF path, title, content, summary ON notes
        BEGIN
            DELETE FROM note_fts WHERE rowid = old.id;
            INSERT INTO note_fts(rowid, path, title, content, summary)
            VALUES (
                new.id, new.path, new.title, new.content,
                coalesce(new.summary, '')
            );
        END
        """
    )
    _execute_sql(
        """
        CREATE TRIGGER notes_fts_delete
        AFTER DELETE ON notes
        BEGIN
            DELETE FROM note_fts WHERE rowid = old.id;
        END
        """
    )


def downgrade() -> None:
    _execute_sql("DROP TRIGGER IF EXISTS notes_fts_insert")
    _execute_sql("DROP TRIGGER IF EXISTS notes_fts_update")
    _execute_sql("DROP TRIGGER IF EXISTS notes_fts_delete")
    _execute_sql("DROP TABLE IF EXISTS note_fts")
    op.drop_index("ix_diagnostics_path", table_name="diagnostics")
    op.drop_index("ix_links_source_path", table_name="links")
    op.drop_table("diagnostics")
    op.drop_table("links")
    op.drop_table("scan_runs")
    op.drop_table("notes")


def _execute_sql(statement: str) -> None:
    """Execute raw FTS DDL online or emit it during offline generation."""

    if context.is_offline_mode():
        op.execute(statement)
    else:
        op.get_bind().exec_driver_sql(statement)
