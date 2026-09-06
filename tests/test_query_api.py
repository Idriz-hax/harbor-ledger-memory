"""API tests for the query endpoint."""

from pathlib import Path

from conftest import authed_client
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import Link, Note
from harbor_ledger_memory.config import Settings


def _seed_catalog_for_query(tmp_path: Path) -> Settings:
    """Create a settings object with seeded catalog for API tests."""
    (tmp_path / "AI").mkdir()
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
                frontmatter_json='{"type":"knowledge","tags":["memory"],"status":"active"}',
                content_hash="aaa",
            )
        )
        session.add(
            Note(
                path="AI/Knowledge/context.md",
                title="Context Windows",
                content="Context windows in LLMs determine how much prior text.",
                summary="Context window size affects LLM reasoning capability",
                frontmatter_json='{"type":"knowledge","tags":["llm","context"],"status":"active"}',
                content_hash="bbb",
            )
        )
        session.add(
            Link(
                source_path="AI/Knowledge/memory-systems.md",
                raw="[[AI/Knowledge/context]]",
                normalized_target="AI/Knowledge/context.md",
                resolution_status="resolved",
            )
        )
        session.commit()
    finally:
        session.close()
        engine.dispose()

    return Settings(
        HLM_VAULT_PATH=tmp_path,
        database_url=f"sqlite:///{db_path}",
    )


def test_query_api_returns_result(tmp_path: Path) -> None:
    """POST /api/v1/queries returns a QueryResult with selected memories."""
    settings = _seed_catalog_for_query(tmp_path)
    client = authed_client(settings)

    response = client.post(
        "/api/v1/queries",
        json={"query": "memory systems"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "trace_id" in data
    assert data["query"] == "memory systems"
    assert isinstance(data["selected_memories"], list)
    assert isinstance(data["total_estimated_tokens"], int)
    assert data["short_term_evidence"] == {
        "hit_paths": [],
        "refreshed_paths": [memory["path"] for memory in data["selected_memories"]],
        "evicted_paths": [],
        "removed_expired": 0,
        "removed_missing": 0,
    }


def test_query_api_with_active_project(tmp_path: Path) -> None:
    """POST /api/v1/queries accepts active_project parameter."""
    settings = _seed_catalog_for_query(tmp_path)
    client = authed_client(settings)

    response = client.post(
        "/api/v1/queries",
        json={"query": "memory", "active_project": "AI/Knowledge"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "memory"


def test_feedback_api_binds_json_body_and_returns_adjustments(tmp_path: Path) -> None:
    """POST /api/v1/feedback binds JSON to the request model, not query params."""
    settings = _seed_catalog_for_query(tmp_path)
    client = authed_client(settings)
    query_response = client.post(
        "/api/v1/queries", json={"query": "memory systems"}
    )
    trace_id = query_response.json()["trace_id"]
    selected_path = query_response.json()["selected_memories"][0]["path"]

    response = client.post(
        "/api/v1/feedback",
        json={
            "trace_id": trace_id,
            "relevant_paths": [selected_path],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"applied": True, "adjustments_count": 1}


def test_feedback_api_rejects_invalid_feedback_with_400(tmp_path: Path) -> None:
    settings = _seed_catalog_for_query(tmp_path)
    client = authed_client(settings)
    response = client.post(
        "/api/v1/feedback",
        json={"trace_id": "missing-trace"},
    )
    assert response.status_code == 400


def test_feedback_changes_later_query_activation(tmp_path: Path) -> None:
    """Feedback on a trace changes activation in a subsequent graph query."""
    settings = _seed_catalog_for_query(tmp_path)
    client = authed_client(settings)

    first = client.post("/api/v1/queries", json={"query": "memory systems"})
    assert first.status_code == 200
    selected = {memory["path"] for memory in first.json()["selected_memories"]}
    target = "AI/Knowledge/context.md"
    assert target in selected

    before = client.post("/api/v1/queries", json={"query": "human working"})
    before_score = next(
        memory["activation_score"]
        for memory in before.json()["selected_memories"]
        if memory["path"] == target
    )
    feedback = client.post(
        "/api/v1/feedback",
        json={"trace_id": first.json()["trace_id"], "relevant_paths": [target]},
    )
    assert feedback.status_code == 200

    after = client.post("/api/v1/queries", json={"query": "human working"})
    after_score = next(
        memory["activation_score"]
        for memory in after.json()["selected_memories"]
        if memory["path"] == target
    )
    assert after_score > before_score


def test_query_api_empty_catalog(tmp_path: Path) -> None:
    """POST /api/v1/queries returns empty result for empty catalog."""
    (tmp_path / "AI").mkdir()
    db_path = tmp_path / "catalog.db"
    engine = create_database(f"sqlite:///{db_path}")
    engine.dispose()

    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        database_url=f"sqlite:///{db_path}",
    )
    client = authed_client(settings)

    response = client.post(
        "/api/v1/queries",
        json={"query": "anything"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "anything"
    assert len(data["selected_memories"]) == 0
    assert data["total_estimated_tokens"] == 0


def test_query_api_validates_request(tmp_path: Path) -> None:
    """POST /api/v1/queries returns 422 for invalid request body."""
    (tmp_path / "AI").mkdir()
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    client = authed_client(settings)

    # Empty query string
    response = client.post(
        "/api/v1/queries",
        json={"query": ""},
    )
    assert response.status_code == 422

    # Missing query field
    response = client.post(
        "/api/v1/queries",
        json={},
    )
    assert response.status_code == 422
