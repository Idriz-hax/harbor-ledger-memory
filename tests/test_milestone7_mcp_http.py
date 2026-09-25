import json
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi.testclient import TestClient

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.config import FolderAccess, FolderRule, McpSettings, Settings

_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "host": "127.0.0.1:8000",
}


def _event(response: Any) -> dict[str, Any]:
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return response.json()


def _initialize(client: TestClient, token: str) -> str:
    response = client.post(
        "/mcp/",
        headers={**_HEADERS, "authorization": f"Bearer {token}"},
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
    assert response.status_code == 200
    return response.headers["mcp-session-id"]


def _call(
    client: TestClient, token: str, session: str, name: str, **arguments: Any
) -> dict[str, Any]:
    response = client.post(
        "/mcp/",
        headers={
            **_HEADERS,
            "authorization": f"Bearer {token}",
            "mcp-session-id": session,
        },
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return _event(response)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        vault_path=tmp_path,
        index_root=PurePosixPath("AI"),
        database_url=f"sqlite:///{tmp_path / 'mcp-http.db'}",
        mcp=McpSettings(enabled=True),
    )


def test_mounted_http_read_scope_and_revocation(tmp_path: Path) -> None:
    (tmp_path / "AI" / "Allowed").mkdir(parents=True)
    (tmp_path / "AI" / "Denied").mkdir()
    (tmp_path / "AI" / "Allowed" / "note.md").write_text(
        "# Allowed\n\npermitted integration phrase", encoding="utf-8"
    )
    (tmp_path / "AI" / "Denied" / "secret.md").write_text(
        "# Denied\n\npermitted integration phrase", encoding="utf-8"
    )
    application = create_app(_settings(tmp_path))
    token = application.state.token_service.create(
        "opencode-read",
        rules=[
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.NONE),
            FolderRule(path=PurePosixPath("AI/Allowed"), access=FolderAccess.READ),
        ],
    )
    with TestClient(application) as client:
        session = _initialize(client, token.plaintext)
        result = _call(
            client,
            token.plaintext,
            session,
            "query",
            text="permitted integration phrase",
        )
        body = json.loads(result["result"]["content"][0]["text"])
        paths = [item["path"] for item in body["selected_memories"]]
        assert paths == ["AI/Allowed/note.md"]
        application.state.token_service.revoke("opencode-read")
        response = client.post(
            "/mcp/",
            headers={**_HEADERS, "authorization": f"Bearer {token.plaintext}"},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert response.status_code == 401


def test_mounted_http_self_approval_requires_explicit_opt_in(tmp_path: Path) -> None:
    (tmp_path / "AI").mkdir()
    application = create_app(_settings(tmp_path))
    rules = [FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE)]
    plain = application.state.token_service.create("plain", rules=rules)
    enabled = application.state.token_service.create(
        "enabled", rules=rules, approve_own_proposals=True
    )
    with TestClient(application) as client:
        session = _initialize(client, plain.plaintext)
        proposal = _call(
            client,
            plain.plaintext,
            session,
            "propose_write",
            path="AI/plain.md",
            content="# Plain",
        )
        proposal_id = json.loads(proposal["result"]["content"][0]["text"])["id"]
        denied = _call(
            client,
            plain.plaintext,
            session,
            "approve_proposal",
            proposal_id=proposal_id,
        )
        assert "approve-own-proposals" in json.dumps(denied)

        enabled_session = _initialize(client, enabled.plaintext)
        enabled_proposal = _call(
            client,
            enabled.plaintext,
            enabled_session,
            "propose_write",
            path="AI/enabled.md",
            content="# Enabled",
        )
        enabled_id = json.loads(enabled_proposal["result"]["content"][0]["text"])["id"]
        approved = _call(
            client,
            enabled.plaintext,
            enabled_session,
            "approve_proposal",
            proposal_id=enabled_id,
        )
        assert (
            json.loads(approved["result"]["content"][0]["text"])["status"] == "applied"
        )


def test_mounted_http_neighbours_respects_explicit_denials(tmp_path: Path) -> None:
    (tmp_path / "AI" / "Allowed").mkdir(parents=True)
    (tmp_path / "AI" / "Denied").mkdir()
    (tmp_path / "AI" / "Allowed" / "one.md").write_text(
        "# One\n\n[[AI/Allowed/two]]", encoding="utf-8"
    )
    (tmp_path / "AI" / "Allowed" / "two.md").write_text("# Two", encoding="utf-8")
    (tmp_path / "AI" / "Denied" / "secret.md").write_text(
        "# Secret\n\n[[AI/Allowed/one]]", encoding="utf-8"
    )
    application = create_app(_settings(tmp_path))
    token = application.state.token_service.create(
        "neighbours-read",
        rules=[
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.NONE),
            FolderRule(path=PurePosixPath("AI/Allowed"), access=FolderAccess.READ),
        ],
    )
    with TestClient(application) as client:
        session = _initialize(client, token.plaintext)
        denied = _call(
            client,
            token.plaintext,
            session,
            "neighbours",
            path="AI/Denied/secret.md",
        )
        assert json.loads(denied["result"]["content"][0]["text"]) == []
        allowed = _call(
            client,
            token.plaintext,
            session,
            "neighbours",
            path="AI/Allowed/one.md",
        )
        paths = {
            item["path"]
            for item in json.loads(allowed["result"]["content"][0]["text"])
        }
        assert "AI/Allowed/two.md" in paths
        assert all("Denied" not in path for path in paths)


def test_mounted_http_restart_requires_mcp_reconnect(tmp_path: Path) -> None:
    (tmp_path / "AI").mkdir()
    settings = _settings(tmp_path)
    first = create_app(settings)
    token = first.state.token_service.create(
        "restartable",
        rules=[FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ)],
    )
    with TestClient(first) as client:
        stale_session = _initialize(client, token.plaintext)

    second = create_app(settings)
    with TestClient(second) as client:
        stale = client.post(
            "/mcp/",
            headers={
                **_HEADERS,
                "authorization": f"Bearer {token.plaintext}",
                "mcp-session-id": stale_session,
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        assert stale.status_code == 404
        fresh_session = _initialize(client, token.plaintext)
        assert fresh_session != stale_session
