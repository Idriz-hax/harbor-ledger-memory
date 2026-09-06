"""REST token enforcement: always-locked, per-token path 403s, read-sweeps.

Task 2 replaces open mode and scope-based auth with a per-token
:class:`AccessPolicy`: zero active tokens means locked (no more open mode),
and every route checks the token's own folder rules for write paths while
silently sweeping read responses to what the token can read.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.config import (
    ApiSettings,
    FolderAccess,
    FolderRule,
    FrontendSettings,
    McpSettings,
    Settings,
)
from harbor_ledger_memory.services.tokens import TokenService
from harbor_ledger_memory.services.ui_session import UI_CSRF_COOKIE

UNAUTHENTICATED = {"detail": "authentication required", "token_required": True}


def _make_app(
    tmp_path: Path,
    rules: tuple[FolderRule, ...] | None = None,
    *,
    frontend_enabled: bool = False,
) -> tuple[FastAPI, str]:
    """Build an app whose vault has the given global folder rules.

    ``vault_path=`` (not the ``HLM_VAULT_PATH`` env alias used by
    test_api.py): accepted identically at runtime via
    ``populate_by_name=True`` and keeps this file pyright-clean.
    """
    if rules is None:
        rules = (
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
        )
    for rule in rules:
        (tmp_path / rule.path.as_posix()).mkdir(parents=True, exist_ok=True)
    settings = Settings(
        vault_path=tmp_path,
        folder_rules=rules,
        database_url=f"sqlite:///{tmp_path / 'auth.db'}",
        frontend=FrontendSettings(enabled=frontend_enabled),
        api=ApiSettings(enabled=True),
        mcp=McpSettings(enabled=True),
    )
    return create_app(settings), f"sqlite:///{tmp_path / 'auth.db'}"


def _headers(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}


def test_zero_tokens_is_locked(tmp_path: Path) -> None:
    """No active tokens means every route 401s (open mode is gone)."""
    app, _ = _make_app(tmp_path)
    with TestClient(app) as client:
        resp = client.get("/api/v1/status")
        assert resp.status_code == 401
        assert resp.json() == UNAUTHENTICATED
        assert (
            client.post(
                "/api/v1/writes", json={"path": "AI/new.md", "content": "# New"}
            ).status_code
            == 401
        )
        assert client.post("/api/v1/writes/1/approve").status_code == 401


def test_valid_token_unlocks(tmp_path: Path) -> None:
    app, db = _make_app(tmp_path)
    with TestClient(app) as client:
        _, plaintext = TokenService(db).create("solo")
        status = client.get("/api/v1/status", headers=_headers(plaintext))
        assert status.status_code == 200


def test_ui_get_issues_cookie_and_cookie_session_is_propose_only(
    tmp_path: Path,
) -> None:
    app, db = _make_app(
        tmp_path,
        rules=(FolderRule(path=PurePosixPath("AI"), access=FolderAccess.AUTO_WRITE),),
        frontend_enabled=True,
    )
    origin = "http://127.0.0.1:8765"
    with TestClient(app, base_url=origin) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "set-cookie" not in client.get("/assets/missing.js").headers
        assert TokenService(db).list() == []
        assert client.get("/api/v1/settings").status_code == 200
        csrf = client.cookies.get(UI_CSRF_COOKIE)
        assert csrf is not None
        assert client.post(
            "/api/v1/writes",
            json={"path": "AI/new.md", "content": "# New"},
            headers={"Origin": origin, "X-HLM-CSRF": csrf},
        ).json()["status"] == "pending"
        assert client.post(
            "/api/v1/tokens",
            json={"name": "ui-token", "rules": []},
            headers={"Origin": origin, "X-HLM-CSRF": csrf},
        ).status_code == 201


def test_missing_and_invalid_token_yield_401(tmp_path: Path) -> None:
    app, db = _make_app(tmp_path)
    with TestClient(app) as client:
        TokenService(db).create("reader")
        assert client.get("/api/v1/status").status_code == 401
        assert client.get("/api/v1/status").json() == UNAUTHENTICATED
        resp = client.get(
            "/api/v1/status", headers={"Authorization": "Bearer hlm_wrong"}
        )
        assert resp.status_code == 401
        resp = client.post("/api/v1/writes", json={"path": "AI/x.md", "content": "# X"})
        assert resp.status_code == 401
        assert client.post("/api/v1/writes/1/approve").status_code == 401


def test_path_403_matrix(tmp_path: Path) -> None:
    """Write/resolve routes enforce the token's own folder rules by path."""
    app, db = _make_app(tmp_path)  # global: AI -> PROPOSE_WRITE
    with TestClient(app) as client:
        service = TokenService(db)
        _, reader = service.create(
            "reader",
            rules=[FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ)],
        )
        _, writer = service.create(
            "writer",
            rules=[
                FolderRule(
                    path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE
                )
            ],
        )
        _, admin = service.create("admin", rules=(), admin=True)

        # reader: read works, propose to AI (read-only for it) is a path 403.
        assert (
            client.get("/api/v1/status", headers=_headers(reader)).status_code == 200
        )
        resp = client.post(
            "/api/v1/writes",
            json={"path": "AI/x.md", "content": "# X"},
            headers=_headers(reader),
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "path 'AI/x.md' not writable by this token"}

        # writer: propose to AI succeeds, creating a pending proposal.
        assert (
            client.post(
                "/api/v1/writes",
                json={"path": "AI/x.md", "content": "# X"},
                headers=_headers(writer),
            ).status_code
            == 200
        )

        # resolve (approve) is a path check: reader can't, writer can.
        resp = client.post("/api/v1/writes/1/approve", headers=_headers(reader))
        assert resp.status_code == 403
        assert resp.json() == {"detail": "path 'AI/x.md' not writable by this token"}
        assert (
            client.post("/api/v1/writes/1/approve", headers=_headers(writer))
            .status_code
            == 200
        )
        # admin: management-only — a bare admin flag no longer implies
        # folder access, so writes and approvals are path-checked too.
        resp = client.post(
            "/api/v1/writes",
            json={"path": "AI/y.md", "content": "# Y"},
            headers=_headers(admin),
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "path 'AI/y.md' not writable by this token"}
        # The writer's pending proposal (id 2) still exists, but admin
        # cannot resolve it either.
        assert (
            client.post(
                "/api/v1/writes",
                json={"path": "AI/y.md", "content": "# Y"},
                headers=_headers(writer),
            ).status_code
            == 200
        )
        resp = client.post("/api/v1/writes/2/approve", headers=_headers(admin))
        assert resp.status_code == 403
        assert resp.json() == {"detail": "path 'AI/y.md' not writable by this token"}


def test_auto_write_requires_token_auto_access(tmp_path: Path) -> None:
    """Auto-apply follows the token's own grant; the global draft never does."""
    app, db = _make_app(
        tmp_path,
        rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.AUTO_WRITE),
        ),
    )
    original = "---\nmanaged_by: harbor-ledger-memory\n---\n# Managed\nv1\n"
    (tmp_path / "AI" / "managed.md").write_text(original, encoding="utf-8")
    with TestClient(app) as client:
        service = TokenService(db)
        _, writer = service.create(
            "writer",
            rules=[
                FolderRule(
                    path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE
                )
            ],
        )
        _, auto = service.create(
            "auto",
            rules=[
                FolderRule(
                    path=PurePosixPath("AI"), access=FolderAccess.AUTO_WRITE
                )
            ],
        )
        _, admin = service.create("admin", rules=(), admin=True)

        # propose-only token: queued pending even though the global draft
        # grants auto-writes.
        resp = client.post(
            "/api/v1/writes",
            json={"path": "AI/queued.md", "content": "# Q"},
            headers=_headers(writer),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"

        # auto-write token: a managed update applies immediately.
        resp = client.post(
            "/api/v1/writes",
            json={"path": "AI/managed.md", "content": original.replace("v1", "v2")},
            headers=_headers(auto),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        assert (tmp_path / "AI" / "managed.md").read_text(encoding="utf-8") == (
            original.replace("v1", "v2")
        )

        # management-only admin: no folder access at all.
        resp = client.post(
            "/api/v1/writes",
            json={"path": "AI/x.md", "content": "# X"},
            headers=_headers(admin),
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "path 'AI/x.md' not writable by this token"}


def test_revoking_last_token_stays_locked(tmp_path: Path) -> None:
    """Revoking the final active token does not restore open mode."""
    app, db = _make_app(tmp_path)
    with TestClient(app) as client:
        service = TokenService(db)
        _, plaintext = service.create("solo")
        assert (
            client.get("/api/v1/status", headers=_headers(plaintext)).status_code
            == 200
        )
        service.revoke("solo")
        # No active tokens remain -> locked, even with a stale bearer.
        resp = client.get("/api/v1/status", headers=_headers(plaintext))
        assert resp.status_code == 401
        assert client.get("/api/v1/status").status_code == 401


def test_html_query_form_requires_token(tmp_path: Path) -> None:
    """The HTML form is no longer an unauthenticated internal bypass."""
    app, db = _make_app(tmp_path)
    with TestClient(app) as client:
        # Zero tokens: the form 401s like every other surface.
        resp = client.post("/query", data={"q": "hello", "project": ""})
        assert resp.status_code == 401
        assert resp.json() == UNAUTHENTICATED

        _, plaintext = TokenService(db).create("reader")
        # Invalid tokens 401 too.
        resp = client.post(
            "/query", data={"q": "hello", "project": "", "token": "hlm_wrong"}
        )
        assert resp.status_code == 401
        assert resp.json() == UNAUTHENTICATED
        # The token travels in the plain form field (forms cannot set
        # headers) and the Authorization header works as a fallback.
        resp = client.post(
            "/query", data={"q": "hello", "project": "", "token": plaintext}
        )
        assert resp.status_code == 200
        resp = client.post(
            "/query",
            data={"q": "hello", "project": ""},
            headers=_headers(plaintext),
        )
        assert resp.status_code == 200


def test_activity_stream_accepts_token_query_param(tmp_path: Path) -> None:
    app, db = _make_app(tmp_path)
    with TestClient(app) as client:
        service = TokenService(db)
        _, plaintext = service.create("reader")
        with client.stream("GET", "/api/v1/activity/stream") as resp:
            assert resp.status_code == 401

        # TestClient's transport runs the ASGI app to completion before
        # returning a response, so an infinite SSE body can never stream
        # incrementally through it (see test_write_events_reach_preconnected_
        # sse_and_rest_history in test_api.py).  The 200 cases are therefore
        # driven directly as a background task on the client portal; the
        # finite 401 cases above keep using client.stream.
        def stream_status(
            extra_headers: list[tuple[bytes, bytes]], query: bytes
        ) -> int:
            portal = client.portal
            assert portal is not None
            started: dict[str, int] = {}
            response_started = threading.Event()
            disconnect = portal.call(asyncio.Event)

            async def receive() -> dict[str, str]:
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    started["status"] = int(message["status"])
                    response_started.set()

            scope: dict[str, object] = {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "path": "/api/v1/activity/stream",
                "raw_path": b"/api/v1/activity/stream",
                "root_path": "",
                "scheme": "http",
                "query_string": query,
                "headers": extra_headers,
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "extensions": {},
                "state": {},
            }

            async def run_stream() -> None:
                await app(scope, receive, send)

            stream_future = portal.start_task_soon(run_stream)
            assert response_started.wait(10), "SSE stream did not send a response"
            portal.call(disconnect.set)
            stream_future.result(timeout=10)
            return int(started["status"])

        assert stream_status([], urlencode({"token": plaintext}).encode()) == 401
        assert (
            stream_status(
                [(b"authorization", f"Bearer {plaintext}".encode())], b""
            )
            == 200
        )
        # All other endpoints stay header-only: the query param is ignored there.
        resp = client.get("/api/v1/status", params={"token": plaintext})
        assert resp.status_code == 401


def test_token_lifecycle_over_rest(tmp_path: Path) -> None:
    app, db = _make_app(tmp_path)
    with TestClient(app) as client:
        service = TokenService(db)
        _, admin = service.create("dash", rules=(), admin=True)
        headers = _headers(admin)
        body = {
            "name": "opencode",
            "rules": [
                {"path": "AI", "access": "propose-write"},
            ],
            "admin": False,
        }
        resp = client.post("/api/v1/tokens", json=body, headers=headers)
        assert resp.status_code == 201
        created = resp.json()
        assert created["token"].startswith("hlm_")
        assert created["name"] == "opencode"
        assert created["admin"] is False
        assert created["rules"] == [{"path": "AI", "access": "propose-write"}]
        assert "created_at" in created
        # GET /tokens is a bare active list with no plaintext/hash leakage.
        resp = client.get("/api/v1/tokens", headers=headers)
        assert resp.status_code == 200
        rows: list[dict[str, object]] = resp.json()
        assert isinstance(rows, list)
        assert {row["name"] for row in rows} == {"dash", "opencode"}
        assert all(
            "token" not in row and "token_hash" not in row and "revoked_at" not in row
            for row in rows
        )
        # Duplicate active name -> 409.
        resp = client.post(
            "/api/v1/tokens",
            json={"name": "opencode", "rules": [], "admin": False},
            headers=headers,
        )
        assert resp.status_code == 409
        # Empty name -> 400 (service-level).
        resp = client.post(
            "/api/v1/tokens", json={"name": "   ", "rules": []}, headers=headers
        )
        assert resp.status_code == 400
        # Malformed rule -> 422 (pydantic FolderRule validation).
        resp = client.post(
            "/api/v1/tokens",
            json={"name": "x", "rules": [{"path": "AI", "access": "bogus"}]},
            headers=headers,
        )
        assert resp.status_code == 422
        deleted = client.delete("/api/v1/tokens/opencode", headers=headers)
        assert deleted.status_code == 200
        # Deleted token is gone from the active list.
        rows = client.get("/api/v1/tokens", headers=headers).json()
        assert {row["name"] for row in rows} == {"dash"}
        assert client.delete("/api/v1/tokens/ghost", headers=headers).status_code == 404


def test_token_endpoints_and_settings_put_require_admin(tmp_path: Path) -> None:
    app, db = _make_app(tmp_path)
    with TestClient(app) as client:
        service = TokenService(db)
        _, writer = service.create(
            "writer",
            rules=[
                FolderRule(
                    path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE
                )
            ],
        )
        headers = _headers(writer)
        assert client.get("/api/v1/tokens", headers=headers).status_code == 403
        resp = client.post(
            "/api/v1/tokens",
            json={"name": "x", "rules": [], "admin": False},
            headers=headers,
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "token missing admin access"}
        assert client.delete("/api/v1/tokens/x", headers=headers).status_code == 403
        # Partial settings update is a valid empty body; admin is still required.
        assert (
            client.put("/api/v1/settings", json={}, headers=headers).status_code
            == 403
        )


def test_delete_after_name_reuse_revokes_new_active_token(tmp_path: Path) -> None:
    app, db = _make_app(tmp_path)
    with TestClient(app) as client:
        service = TokenService(db)
        _, admin = service.create("dash", rules=(), admin=True)
        headers = _headers(admin)
        payload: dict[str, object] = {"name": "alpha", "rules": [], "admin": False}
        first = client.post("/api/v1/tokens", json=payload, headers=headers)
        assert first.status_code == 201
        assert client.delete("/api/v1/tokens/alpha", headers=headers).status_code == 200
        # Active list is empty of alpha after revocation.
        rows = client.get("/api/v1/tokens", headers=headers).json()
        assert all(row["name"] != "alpha" for row in rows)
        # Name reuse works: mint a second alpha and verify it authenticates.
        second = client.post("/api/v1/tokens", json=payload, headers=headers)
        assert second.status_code == 201
        second_token = second.json()["token"]
        assert (
            client.get("/api/v1/status", headers=_headers(second_token)).status_code
            == 200
        )
        assert client.delete("/api/v1/tokens/alpha", headers=headers).status_code == 200
        # The newly active alpha was revoked, so it no longer authenticates.
        resp = client.get("/api/v1/status", headers=_headers(second_token))
        assert resp.status_code == 401


def test_status_reports_the_calling_tokens_own_rules(tmp_path: Path) -> None:
    """Status lists the calling token's own rules, not a global sweep."""
    app, db = _make_app(
        tmp_path,
        rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
            FolderRule(
                path=PurePosixPath("Secret"), access=FolderAccess.PROPOSE_WRITE
            ),
        ),
    )
    with TestClient(app) as client:
        service = TokenService(db)
        # reader denies Secret in its own policy; admin has no own rules.
        _, reader = service.create(
            "reader",
            rules=[FolderRule(path=PurePosixPath("Secret"), access=FolderAccess.NONE)],
        )
        _, admin = service.create("admin", rules=(), admin=True)

        # reader: write_policy lists only this token's own rule (Secret: none).
        reader_status = (
            client.get("/api/v1/status", headers=_headers(reader)).json()
        )
        assert reader_status["write_policy"]["rules"] == [
            {"path": "Secret", "access": "none"},
        ]
        # admin: no own rules -> empty write policy.
        admin_status = client.get("/api/v1/status", headers=_headers(admin)).json()
        assert admin_status["write_policy"]["rules"] == []


def _seed_vault_files(tmp_path: Path) -> None:
    (tmp_path / "AI" / "Knowledge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Secret").mkdir(parents=True, exist_ok=True)
    (tmp_path / "AI" / "Knowledge" / "visible.md").write_text(
        "# Visible\nMemory systems and retrieval fundamentals.\n", encoding="utf-8"
    )
    (tmp_path / "Secret" / "hidden.md").write_text(
        "# Hidden\nMemory systems and retrieval private notes.\n", encoding="utf-8"
    )


def test_graph_read_sweep_hides_unreadable_nodes(tmp_path: Path) -> None:
    app, db = _make_app(
        tmp_path,
        rules=(
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
            FolderRule(
                path=PurePosixPath("Secret"), access=FolderAccess.PROPOSE_WRITE
            ),
        ),
    )
    _seed_vault_files(tmp_path)
    with TestClient(app) as client:
        _, reader = TokenService(db).create(
            "reader",
            rules=[FolderRule(path=PurePosixPath("Secret"), access=FolderAccess.NONE)],
        )
        data = client.get("/api/v1/graph", headers=_headers(reader)).json()
        paths = {node["path"] for node in data["nodes"]}
        assert "AI/Knowledge/visible.md" in paths
        assert "Secret/hidden.md" not in paths


def test_query_read_sweep_hides_unreadable_memories(tmp_path: Path) -> None:
    rules = (
        FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
        FolderRule(path=PurePosixPath("Secret"), access=FolderAccess.PROPOSE_WRITE),
    )
    app, db = _make_app(tmp_path, rules=rules)
    (tmp_path / "AI" / "Knowledge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Secret").mkdir(parents=True, exist_ok=True)
    (tmp_path / "AI" / "Knowledge" / "visible.md").write_text(
        "---\ntype: knowledge\ntags: [memory]\nstatus: active\n---\n"
        "# Visible\nMemory systems and retrieval fundamentals.\n"
        "[[Secret/hidden]]\n",
        encoding="utf-8",
    )
    (tmp_path / "Secret" / "hidden.md").write_text(
        "---\ntype: knowledge\ntags: [memory]\nstatus: active\n---\n"
        "# Hidden\nMemory systems and retrieval private notes.\n",
        encoding="utf-8",
    )
    with TestClient(app) as client:
        _, reader = TokenService(db).create(
            "reader",
            rules=[FolderRule(path=PurePosixPath("Secret"), access=FolderAccess.NONE)],
        )
        data = client.post(
            "/api/v1/queries",
            json={"query": "memory systems"},
            headers=_headers(reader),
        ).json()
        paths = {memory["path"] for memory in data["selected_memories"]}
        assert "AI/Knowledge/visible.md" in paths
        assert "Secret/hidden.md" not in paths


def test_mcp_requires_token_always(tmp_path: Path) -> None:
    """The mounted /mcp sub-app is always locked (no open mode)."""
    app, db = _make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        resp = client.get("/mcp")
        assert resp.status_code == 401
        body = resp.json()
        assert body["detail"] == "authentication required"
        assert body["token_required"] is True
        # The MCP 401 also carries an actionable hint (REST does not).
        assert "hint" in body
        # With a valid token the transport handshake proceeds past the auth
        # gate (200 from the MCP layer, never the 401).
        _, plaintext = TokenService(db).create("mcp", rules=(), admin=True)
        handshake = client.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {plaintext}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Host": "127.0.0.1:8000",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert handshake.status_code == 200
