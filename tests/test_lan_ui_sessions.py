"""LAN Web UI session boundary coverage."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.config import (
    ApiSettings,
    FrontendSettings,
    McpSettings,
    NetworkSettings,
    Settings,
)
from harbor_ledger_memory.services.tokens import TokenService
from harbor_ledger_memory.services.ui_session import (
    UI_CSRF_COOKIE,
    UI_SESSION_COOKIE,
    UiSessionService,
)

_MCP_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "lan-session-test", "version": "1"},
    },
}


def _app(
    tmp_path: Path,
    *,
    origin: str = "http://hlm.local",
    cidrs: tuple[str, ...] = (),
    api_enabled: bool = False,
    mcp_enabled: bool = True,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    verifier = PasswordHasher().hash("correct horse")
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'lan.db'}",
        frontend=FrontendSettings(
            enabled=True,
            mode="lan",
            public_origin=origin,
            password_verifier=verifier,
        ),
        network=NetworkSettings(
            enabled=True,
            insecure_http=origin.startswith("http"),
            allowed_cidrs=cidrs,
        ),
        mcp=McpSettings(enabled=mcp_enabled),
        api=ApiSettings(enabled=api_enabled),
    )
    return create_app(settings, ui_origin=origin)


def test_lan_login_flags_failures_and_logout_csrf(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app, base_url="http://hlm.local") as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/login").status_code == 200
        assert client.post(
            "/login", data={"password": "wrong"}, headers={"Origin": "http://hlm.local"}
        ).status_code == 401
        response = client.post(
            "/login",
            data={"password": "correct horse"},
            headers={"Origin": "http://hlm.local"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        cookies = response.headers.get_list("set-cookie")
        assert any(
            UI_SESSION_COOKIE in value and "HttpOnly" in value for value in cookies
        )
        assert any(
            UI_CSRF_COOKIE in value and "SameSite=strict" in value
            for value in cookies
        )
        csrf = client.cookies.get(UI_CSRF_COOKIE)
        assert csrf is not None
        assert (
            client.post("/logout", headers={"Origin": "http://hlm.local"}).status_code
            == 403
        )
        assert client.post(
            "/logout",
            headers={"Origin": "http://hlm.local", "X-HLM-CSRF": csrf},
            follow_redirects=False,
        ).status_code == 303
        assert client.get("/", follow_redirects=False).status_code == 303


def test_lan_ui_does_not_accept_the_legacy_session_cookie(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app, base_url="http://hlm.local") as client:
        login = client.post(
            "/login",
            data={"password": "correct horse"},
            headers={"Origin": "http://hlm.local"},
            follow_redirects=False,
        )
        session = login.cookies.get(UI_SESSION_COOKIE)
        assert session is not None

        client.cookies.clear()
        client.cookies.set("legacy_ui_session", session)
        assert client.get("/", follow_redirects=False).status_code == 303


def test_lan_source_allowlist_rejects_replayed_cookie(tmp_path: Path) -> None:
    app = _app(tmp_path, cidrs=("10.0.0.0/8",))

    async def exercise() -> tuple[int, int]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("10.1.2.3", 1)),
            base_url="http://hlm.local",
        ) as allowed:
            login = await allowed.post(
                "/login",
                data={"password": "correct horse"},
                headers={"Origin": "http://hlm.local"},
            )
            session = login.cookies.get(UI_SESSION_COOKIE)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.168.1.2", 1)),
            base_url="http://hlm.local",
            cookies={UI_SESSION_COOKIE: session or ""},
        ) as denied:
            response = await denied.get("/")
            return login.status_code, response.status_code

    assert asyncio.run(exercise()) == (303, 403)


def test_lan_login_throttles_repeated_failures(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app, base_url="http://hlm.local") as client:
        headers = {"Origin": "http://hlm.local"}
        for _ in range(5):
            assert (
                client.post(
                    "/login", data={"password": "wrong"}, headers=headers
                ).status_code
                == 401
            )
        assert (
            client.post(
                "/login", data={"password": "wrong"}, headers=headers
            ).status_code
            == 429
        )


def test_session_expiry_and_revocation() -> None:
    now = [0.0]
    service = UiSessionService(
        idle_seconds=5, absolute_seconds=10, clock=lambda: now[0]
    )
    session = service.new_session()
    assert service.authenticate(session)
    now[0] = 6
    assert not service.authenticate(session)
    session = service.new_session()
    service.revoke(session)
    assert not service.authenticate(session)


def test_https_trusted_host_and_hsts_only_https(tmp_path: Path) -> None:
    https_app = _app(tmp_path, origin="https://hlm.local")
    with TestClient(https_app, base_url="https://hlm.local") as client:
        response = client.get("/login")
        assert response.status_code == 200
        assert response.headers["strict-transport-security"]
        assert "Secure" in client.post(
            "/login",
            data={"password": "correct horse"},
            headers={"Origin": "https://hlm.local"},
            follow_redirects=False,
        ).headers["set-cookie"]
        assert client.get("/login", headers={"Host": "evil.local"}).status_code == 400

    http_root = tmp_path / "http"
    http_root.mkdir()
    http_app = _app(http_root, origin="http://hlm.local")
    with TestClient(http_app, base_url="http://hlm.local") as client:
        response = client.get("/login")
        assert "strict-transport-security" not in response.headers
        assert client.get("/login", headers={"Host": "evil.local"}).status_code == 400


def test_frontend_disabled_does_not_mint_session(tmp_path: Path) -> None:
    settings = Settings(
        vault_path=tmp_path, database_url=f"sqlite:///{tmp_path / 'x.db'}"
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/")
        assert "set-cookie" not in response.headers


def test_api_and_mcp_enablement_are_independent(tmp_path: Path) -> None:
    disabled = _app(tmp_path, api_enabled=False, mcp_enabled=False)
    with TestClient(disabled, base_url="http://hlm.local") as client:
        assert client.post(
            "/login",
            data={"password": "correct horse"},
            headers={"Origin": "http://hlm.local"},
        ).status_code == 200
        assert client.get("/api/v1/settings").status_code == 200
        assert client.get("/mcp").status_code == 404
        token = disabled.state.token_service.create("integration").plaintext
        response = client.get(
            "/api/v1/status", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403
        assert response.json() == {
            "detail": "REST API disabled",
            "api_enabled": False,
        }

    enabled = _app(tmp_path / "enabled", api_enabled=True, mcp_enabled=True)
    with TestClient(enabled, base_url="http://hlm.local") as client:
        token = TokenService(
            f"sqlite:///{tmp_path / 'enabled' / 'lan.db'}"
        ).create("integration").plaintext
        assert client.get(
            "/api/v1/status", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 200


def test_legacy_query_form_obeys_api_gate_and_lan_session(tmp_path: Path) -> None:
    app = _app(tmp_path, api_enabled=False, mcp_enabled=False)
    token = app.state.token_service.create("form-integration").plaintext
    with TestClient(app, base_url="http://hlm.local") as client:
        assert client.get("/query", follow_redirects=False).status_code == 303
        assert client.post(
            "/query",
            data={"q": "memory", "token": token},
            follow_redirects=False,
        ).status_code == 303
        login = client.post(
            "/login",
            data={"password": "correct horse"},
            headers={"Origin": "http://hlm.local"},
        )
        assert login.status_code == 200
        csrf = client.cookies.get(UI_CSRF_COOKIE)
        assert csrf is not None
        disabled = client.post(
            "/query",
            data={"q": "memory", "token": token},
            headers={
                "Origin": "http://hlm.local",
                "X-HLM-CSRF": csrf,
            },
            follow_redirects=False,
        )
        assert disabled.status_code == 403
        assert disabled.json() == {
            "detail": "REST API disabled",
            "api_enabled": False,
        }


def test_lan_activity_stream_stops_after_session_revocation(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app, base_url="http://hlm.local") as client:
        assert client.post(
            "/login",
            data={"password": "correct horse"},
            headers={"Origin": "http://hlm.local"},
        ).status_code == 200
        session = client.cookies.get(UI_SESSION_COOKIE)
        assert session
        activity = app.state.activity_service
        event = activity.record("query", {"query": "still-private"})

        async def revoked_stream(**_: object) -> AsyncGenerator[str, None]:
            app.state.ui_session_service.revoke(session)
            yield activity.sse_message(event)

        activity.stream = revoked_stream
        with client.stream("GET", "/api/v1/activity/stream") as response:
            assert response.status_code == 200
            response.read()
            assert response.text == ""


def test_valid_lan_activity_session_still_streams(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app, base_url="http://hlm.local") as client:
        assert client.post(
            "/login",
            data={"password": "correct horse"},
            headers={"Origin": "http://hlm.local"},
        ).status_code == 200
        activity = app.state.activity_service
        event = activity.record("query", {"query": "visible"})

        async def valid_stream(**_: object) -> AsyncGenerator[str, None]:
            yield activity.sse_message(event)

        activity.stream = valid_stream
        with client.stream("GET", "/api/v1/activity/stream") as response:
            assert response.status_code == 200
            response.read()
            assert "visible" in response.text


def test_lan_ui_cookie_cannot_authenticate_mcp(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app, base_url="http://hlm.local") as client:
        response = client.post(
            "/login",
            data={"password": "correct horse"},
            headers={"Origin": "http://hlm.local"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.cookies.get(UI_SESSION_COOKIE)
        mcp_response = client.post(
            "/mcp/",
            json=_MCP_INITIALIZE,
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
        )
        assert mcp_response.status_code == 401
