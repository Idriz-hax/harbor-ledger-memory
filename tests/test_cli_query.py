"""CLI tests for the query command."""

import json
from pathlib import Path

from typer.testing import CliRunner

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import Note
from harbor_ledger_memory.cli import app

runner = CliRunner()


def _seed_vault_for_query(tmp_path: Path) -> None:
    """Create vault files and seed the catalog for query CLI tests."""
    ai = tmp_path / "AI"
    knowledge = ai / "Knowledge"
    knowledge.mkdir(parents=True)

    (knowledge / "memory-systems.md").write_text(
        (
            "---\n"
            "type: knowledge\n"
            "status: active\n"
            "tags:\n"
            "  - memory\n"
            "  - cognitive\n"
            "---\n"
            "# Memory Systems\n\n"
            "Human memory systems include working memory and long-term memory.\n"
            "Working memory has limited capacity.\n\n"
        ),
        encoding="utf-8",
    )
    (knowledge / "context.md").write_text(
        (
            "---\n"
            "type: knowledge\n"
            "status: active\n"
            "tags:\n"
            "  - llm\n"
            "  - context\n"
            "---\n"
            "# Context Windows\n\n"
            "Context windows in LLMs determine how much prior text\n"
            "the model can attend to.\n\n"
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "catalog.db"
    engine = create_database(f"sqlite:///{db_path}")
    session = CatalogSession(bind=engine)
    try:
        session.add(
            Note(
                path="AI/Knowledge/memory-systems.md",
                title="Memory Systems",
                content=(
                    "Human memory systems include working memory and long-term memory."
                ),
                summary="Overview of memory systems and their limitations",
                frontmatter_json=(
                    '{"type":"knowledge","tags":["memory"],"status":"active"}'
                ),
                content_hash="aaa",
            )
        )
        session.add(
            Note(
                path="AI/Knowledge/context.md",
                title="Context Windows",
                content="Context windows in LLMs determine how much prior text.",
                summary="Context window size affects LLM reasoning capability",
                frontmatter_json=(
                    '{"type":"knowledge","tags":["llm","context"],"status":"active"}'
                ),
                content_hash="bbb",
            )
        )
        session.commit()
    finally:
        session.close()
        engine.dispose()


def test_query_cli_json_output(tmp_path: Path, monkeypatch) -> None:
    """CLI query command returns JSON with trace_id and selected_memories."""
    _seed_vault_for_query(tmp_path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")

    result = runner.invoke(app, ["query", "memory", "--format", "json"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    assert "trace_id" in data
    assert "query" in data
    assert data["query"] == "memory"
    assert isinstance(data["selected_memories"], list)
    assert isinstance(data["total_estimated_tokens"], int)
    assert data["short_term_evidence"] == {
        "hit_paths": [],
        "refreshed_paths": [memory["path"] for memory in data["selected_memories"]],
        "evicted_paths": [],
        "removed_expired": 0,
        "removed_missing": 0,
    }


def test_query_cli_human_readable(tmp_path: Path, monkeypatch) -> None:
    """CLI query command produces human-readable output without --json."""
    _seed_vault_for_query(tmp_path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")

    result = runner.invoke(app, ["query", "memory", "--format", "text"])
    assert result.exit_code == 0, result.output
    assert (
        "trace_id" in result.output
        or "Trace" in result.output
        or "query" in result.output.lower()
    )


def test_query_cli_with_project(tmp_path: Path, monkeypatch) -> None:
    """CLI query command accepts --project option."""
    _seed_vault_for_query(tmp_path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")

    result = runner.invoke(
        app,
        ["query", "memory", "--project", "AI/Knowledge", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["query"] == "memory"


def test_query_cli_empty_catalog(tmp_path: Path, monkeypatch) -> None:
    """CLI query command handles empty catalog gracefully."""
    ai = tmp_path / "AI"
    ai.mkdir()
    db_path = tmp_path / "catalog.db"
    engine = create_database(f"sqlite:///{db_path}")
    engine.dispose()

    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    result = runner.invoke(app, ["query", "anything", "--format", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data["selected_memories"]) == 0
    assert data["total_estimated_tokens"] == 0
