"""API tests for the query endpoint."""

from pathlib import Path, PurePosixPath

from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import authed_client
from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import AdaptiveEdge, Link, Note
from harbor_ledger_memory.config import ApiSettings, FolderAccess, FolderRule, Settings


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
    query_response = client.post("/api/v1/queries", json={"query": "memory systems"})
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
    assert response.json() == {
        "applied": True,
        "adjustments_count": 0,
        "recorded_count": 1,
    }


def test_feedback_api_rejects_invalid_feedback_with_400(tmp_path: Path) -> None:
    settings = _seed_catalog_for_query(tmp_path)
    client = authed_client(settings)
    response = client.post(
        "/api/v1/feedback",
        json={"trace_id": "missing-trace"},
    )
    assert response.status_code == 400


def test_restricted_rest_feedback_rejects_unreadable_trace_path(
    tmp_path: Path,
) -> None:
    settings = _seed_catalog_for_query(tmp_path)
    (tmp_path / "Private").mkdir()
    (tmp_path / "Private" / "secret.md").write_text(
        "# Secret\n\nunreadable feedback phrase", encoding="utf-8"
    )
    engine = create_database(settings.database_url)
    session = CatalogSession(bind=engine)
    try:
        session.add(
            Note(
                path="Private/secret.md",
                title="Secret",
                content="unreadable feedback phrase",
                summary="unreadable feedback phrase",
                frontmatter_json="{}",
                content_hash="secret",
            )
        )
        session.commit()
    finally:
        session.close()
        engine.dispose()

    settings = settings.model_copy(update={"api": ApiSettings(enabled=True)})
    app = create_app(settings)
    admin_token = app.state.token_service.create(
        "feedback-admin",
        rules=[FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE)],
        admin=True,
    ).plaintext
    restricted_token = app.state.token_service.create(
        "feedback-restricted",
        rules=[
            FolderRule(path=PurePosixPath("Public"), access=FolderAccess.PROPOSE_WRITE),
            FolderRule(path=PurePosixPath("Private"), access=FolderAccess.NONE),
        ],
    ).plaintext
    with TestClient(
        app,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        queried = client.post(
            "/api/v1/queries", json={"query": "unreadable feedback phrase"}
        )
        trace_id = queried.json()["trace_id"]
        secret_path = "Private/secret.md"
        client.headers["Authorization"] = f"Bearer {restricted_token}"
        response = client.post(
            "/api/v1/feedback",
            json={"trace_id": trace_id, "relevant_paths": [secret_path]},
        )

    assert response.status_code == 400
    engine = create_database(settings.database_url)
    session = CatalogSession(bind=engine)
    try:
        assert session.scalars(select(AdaptiveEdge)).all() == []
    finally:
        session.close()
        engine.dispose()


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
    assert after_score == before_score


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
