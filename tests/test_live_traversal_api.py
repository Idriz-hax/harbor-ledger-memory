"""Endpoint tests for the authenticated live traversal stream."""

import asyncio
from pathlib import Path, PurePosixPath

from fastapi.testclient import TestClient

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.api.auth import AuthContext
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import ActivityEvent
from harbor_ledger_memory.config import ApiSettings, FolderAccess, FolderRule, Settings
from harbor_ledger_memory.services.access import AccessPolicy
from harbor_ledger_memory.services.live_traversal import TraversalEvent


def test_traversal_stream_requires_auth_and_filters_paths(tmp_path: Path) -> None:
    (tmp_path / "AI").mkdir()
    (tmp_path / "Secret").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        api=ApiSettings(enabled=True),
    )
    app = create_app(settings)
    token = app.state.token_service.create(
        "read-ai",
        rules=[
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
            FolderRule(path=PurePosixPath("Secret"), access=FolderAccess.NONE),
        ],
    ).plaintext
    publisher = app.state.live_traversal

    with TestClient(app) as unauthenticated_client:
        assert (
            unauthenticated_client.get("/api/v1/graph/traversal/stream").status_code
            == 401
        )
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}):
        route = next(
            route
            for route in app.routes
            if getattr(route, "path", None) == "/api/v1/graph/traversal/stream"
        )
        record = app.state.token_service.verify(token)
        response = route.endpoint(
            auth=AuthContext(record, AccessPolicy(record.rules, record.admin))
        )
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        publisher.publish(TraversalEvent("t", 1, "read", "Secret/hidden.md"))
        publisher.publish(TraversalEvent("t", 2, "read", "AI/visible.md"))

        async def collect() -> list[str]:
            try:
                return [
                    await anext(response.body_iterator),
                    await anext(response.body_iterator),
                ]
            finally:
                await response.body_iterator.aclose()

        lines = asyncio.run(collect())

    assert any('"sequence": 2' in line for line in lines)
    assert not any("hidden.md" in line for line in lines)


def test_traversal_stream_disconnect_does_not_persist_activity(tmp_path: Path) -> None:
    (tmp_path / "AI").mkdir()
    settings = Settings(
        vault_path=tmp_path, database_url=f"sqlite:///{tmp_path / 'catalog.db'}"
    )
    app = create_app(settings)
    token = app.state.token_service.create(
        "read-all",
        rules=[FolderRule(path=PurePosixPath("."), access=FolderAccess.READ)],
    ).plaintext
    engine = create_database(settings.database_url)
    try:
        publisher = app.state.live_traversal
        with TestClient(app, headers={"Authorization": f"Bearer {token}"}):
            with CatalogSession(bind=engine) as session:
                before = session.query(ActivityEvent).count()
            route = next(
                route
                for route in app.routes
                if getattr(route, "path", None) == "/api/v1/graph/traversal/stream"
            )
            record = app.state.token_service.verify(token)
            response = route.endpoint(
                auth=AuthContext(record, AccessPolicy(record.rules, record.admin))
            )
            publisher.publish(TraversalEvent("t", 1, "read", "AI/live.md"))

            async def consume_one() -> None:
                try:
                    await anext(response.body_iterator)
                finally:
                    await response.body_iterator.aclose()

            asyncio.run(consume_one())
            assert not publisher._subscribers  # noqa: SLF001
        with CatalogSession(bind=engine) as session:
            assert session.query(ActivityEvent).count() == before
    finally:
        engine.dispose()
