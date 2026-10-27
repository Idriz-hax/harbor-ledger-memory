import json
from collections.abc import MutableMapping
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi.testclient import TestClient

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.config import ApiSettings, FolderAccess, FolderRule, Settings


def setup(tmp_path: Path):
    (tmp_path / "AI").mkdir()
    settings = Settings(
        vault_path=tmp_path,
        api=ApiSettings(enabled=True),
        folder_rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
        ),
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )
    app = create_app(settings)
    creator = app.state.token_service.create(
        "creator",
        [FolderRule(path=PurePosixPath("AI"), access=FolderAccess.AUTO_WRITE)],
        approve_own_proposals=True,
    ).plaintext
    ordinary = app.state.token_service.create(
        "ordinary",
        [FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE)],
    ).plaintext
    return app, settings, creator, ordinary


def test_agent_write_limits_policy_summaries_and_own_resolution(tmp_path: Path) -> None:
    app, settings, creator, ordinary = setup(tmp_path)
    with TestClient(app) as anon:
        unauth = anon.get("/api/v1/agent/writes/pending")
        assert unauth.status_code == 401
        assert unauth.headers["cache-control"] == "no-store"
    with TestClient(app, headers={"Authorization": f"Bearer {creator}"}) as client:

        def post(content: str, path: str = "AI/n.md"):
            return client.post(
                "/api/v1/agent/writes",
                json={"path": path, "content": content, "operation": "write"},
            )

        body = post("é" * 32768)
        assert body.status_code == 200
        assert body.json()["status"] == "pending"
        assert set(body.json()) == {"id", "path", "operation", "status", "requested_at"}
        assert body.headers["cache-control"] == "no-store"
        assert post("é" * 32769).status_code == 413
        assert post("x", "a" * 4097).status_code == 413
        assert post("x", "é" * 2049).status_code == 413
        assert post("x", "a" * 4096).status_code == 403
        for invalid_path in ("../escape.md", "AI/../../escape.md"):
            invalid = post("escape", invalid_path)
            assert invalid.status_code == 422
            assert invalid.headers["cache-control"] == "no-store"
        assert client.get("/api/v1/agent/writes/pending").json()["proposals"] == [
            body.json()
        ]
        assert (
            client.post(
                "/api/v1/agent/writes",
                content=b"not-json",
                headers={"content-type": "application/json"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/agent/writes",
                content=json.dumps(
                    {"path": "AI/a.md", "content": "x", "operation": "bad"}
                ),
                headers={"content-type": "application/json"},
            ).status_code
            == 422
        )
        too_big = client.post(
            "/api/v1/agent/writes",
            content=b"{" + b" " * (512 * 1024),
            headers={"content-type": "application/json", "content-length": "1"},
        )
        assert too_big.status_code == 413
        page = client.get("/api/v1/agent/writes/pending?limit=1")
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        assert len(page.json()["proposals"]) == 1
        assert "content" not in page.text and "diff" not in page.text
        proposal_id = body.json()["id"]
        assert (
            client.post(
                f"/api/v1/agent/writes/{proposal_id}/reject",
                headers={"Authorization": f"Bearer {ordinary}"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/agent/writes/{proposal_id}/approve",
                headers={"Authorization": f"Bearer {creator}"},
            ).json()["status"]
            == "applied"
        )
        assert (settings.vault_path / "AI/n.md").read_text() == "é" * 32768
        second = post("reject me", "AI/rejected.md").json()
        rejected = client.post(f"/api/v1/agent/writes/{second['id']}/reject")
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        assert "content" not in rejected.json() and "diff" not in rejected.json()
        assert client.get("/api/v1/agent/writes/pending?limit=101").status_code == 422


def test_pending_pages_filter_in_sql_and_preserve_cursor(tmp_path: Path) -> None:
    app, _, creator, _ = setup(tmp_path)
    token = app.state.token_service.create(
        "limited",
        [
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
            FolderRule(path=PurePosixPath("AI/private"), access=FolderAccess.NONE),
        ],
    ).plaintext
    with TestClient(app, headers={"Authorization": f"Bearer {creator}"}) as client:
        ids: list[int] = []
        for path in ("AI/a.md", "AI/private/hidden.md", "AI/b.md"):
            ids.append(
                client.post(
                    "/api/v1/agent/writes",
                    json={"path": path, "content": "secret", "operation": "write"},
                ).json()["id"]
            )
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        first = client.get("/api/v1/agent/writes/pending?limit=1").json()
        assert [p["id"] for p in first["proposals"]] == [ids[2]]
        assert first["next_before_id"] == ids[2]
        second = client.get(
            f"/api/v1/agent/writes/pending?limit=1&before_id={first['next_before_id']}"
        ).json()
        assert [p["id"] for p in second["proposals"]] == [ids[0]]
        assert second["next_before_id"] is None
        assert all(
            "private" not in p["path"] for p in first["proposals"] + second["proposals"]
        )
        assert client.get("/api/v1/agent/writes/pending?before_id=0").status_code == 422


def test_only_creator_with_approval_flag_can_resolve(tmp_path: Path) -> None:
    app, _, creator, _ = setup(tmp_path)
    other = app.state.token_service.create(
        "other-approved",
        [FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE)],
        approve_own_proposals=True,
    ).plaintext
    with TestClient(app, headers={"Authorization": f"Bearer {creator}"}) as client:
        proposal = client.post(
            "/api/v1/agent/writes",
            json={
                "path": "AI/owned.md",
                "content": "private body",
                "operation": "write",
            },
        ).json()
    with TestClient(app, headers={"Authorization": f"Bearer {other}"}) as client:
        denied = client.post(f"/api/v1/agent/writes/{proposal['id']}/reject")
        assert denied.status_code == 404
        assert denied.headers["cache-control"] == "no-store"
        assert "private body" not in denied.text and "diff" not in denied.text
        assert client.get("/api/v1/agent/writes/pending").json()["proposals"]
    with TestClient(app, headers={"Authorization": f"Bearer {creator}"}) as client:
        done = client.post(f"/api/v1/agent/writes/{proposal['id']}/reject")
        assert done.status_code == 200
        assert set(done.json()) == {"id", "path", "operation", "status", "requested_at"}


def test_lone_surrogates_return_no_store_422_without_proposals(tmp_path: Path) -> None:
    app, _, creator, _ = setup(tmp_path)
    with TestClient(
        app,
        headers={"Authorization": f"Bearer {creator}"},
        raise_server_exceptions=False,
    ) as client:
        for payload in (
            {"path": "AI/\ud800.md", "content": "ok", "operation": "write"},
            {"path": "AI/valid.md", "content": "\ud800", "operation": "write"},
        ):
            response = client.post(
                "/api/v1/agent/writes",
                content=json.dumps(payload, ensure_ascii=True),
                headers={"content-type": "application/json"},
            )
            assert response.status_code == 422
            assert response.headers["cache-control"] == "no-store"
        assert client.get("/api/v1/agent/writes/pending").json()["proposals"] == []


def test_agent_write_openapi_documents_schemas_and_errors(tmp_path: Path) -> None:
    app, _, _, _ = setup(tmp_path)
    openapi = app.openapi()
    paths = openapi["paths"]
    post = paths["/api/v1/agent/writes"]["post"]
    request_schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert set(request_schema["properties"]) == {"path", "content", "operation"}
    expected = {"id", "path", "operation", "status", "requested_at"}
    route_ops = [
        post,
        paths["/api/v1/agent/writes/pending"]["get"],
        paths["/api/v1/agent/writes/{id}/approve"]["post"],
        paths["/api/v1/agent/writes/{id}/reject"]["post"],
    ]
    for operation in route_ops:
        assert {"401", "403", "404", "413", "422"} <= set(operation["responses"])
    summary = post["responses"]["200"]["content"]["application/json"]["schema"]
    if "$ref" in summary:
        summary = openapi["components"]["schemas"][summary["$ref"].rsplit("/", 1)[-1]]
    assert set(summary["properties"]) == expected
    pending = route_ops[1]["responses"]["200"]["content"]["application/json"]["schema"]
    if "$ref" in pending:
        pending = openapi["components"]["schemas"][pending["$ref"].rsplit("/", 1)[-1]]
    assert "proposals" in pending["properties"]
    summary_items = pending["properties"]["proposals"]["items"]
    if "$ref" in summary_items:
        summary_items = openapi["components"]["schemas"][
            summary_items["$ref"].rsplit("/", 1)[-1]
        ]
    assert set(summary_items["properties"]) == expected
    for operation in route_ops[2:]:
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        if "$ref" in schema:
            schema = openapi["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
        assert set(schema["properties"]) == expected


def test_agent_raw_chunked_limit_rejects_without_persisting(tmp_path: Path) -> None:
    app, settings, creator, _ = setup(tmp_path)
    token = f"Bearer {creator}".encode()
    payload = (
        b'{"path":"AI/chunk.md","content":"'
        + b"x" * (512 * 1024)
        + b'","operation":"write"}'
    )
    chunks = iter((payload[:200_000], payload[200_000:400_000], payload[400_000:]))
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/agent/writes",
        "raw_path": b"/api/v1/agent/writes",
        "query_string": b"",
        "headers": [(b"authorization", token), (b"content-type", b"application/json")],
        "client": ("testclient", 1),
        "server": ("testserver", 80),
        "state": {},
    }

    async def receive():
        try:
            return {"type": "http.request", "body": next(chunks), "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    messages: list[dict[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        messages.append(dict(message))

    import asyncio

    asyncio.run(app(scope, receive, send))
    assert (
        next(m["status"] for m in messages if m["type"] == "http.response.start") == 413
    )
    assert not (settings.vault_path / "AI/chunk.md").exists()


def test_legacy_write_still_auto_applies(tmp_path: Path) -> None:
    app, _settings, creator, _ = setup(tmp_path)
    with TestClient(app, headers={"Authorization": f"Bearer {creator}"}) as client:
        result = client.post(
            "/api/v1/writes", json={"path": "AI/legacy.md", "content": "legacy"}
        )
        assert result.status_code == 200
        assert result.json()["status"] == "applied"
        assert (
            client.get("/api/v1/agent/writes/pending").headers["cache-control"]
            == "no-store"
        )
