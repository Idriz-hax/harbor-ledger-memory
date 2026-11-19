import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.cli import app as cli_app
from harbor_ledger_memory.config import ApiSettings, Settings
from harbor_ledger_memory.services.activity import ActivityService
from harbor_ledger_memory.services.tokens import TokenService


def test_api_token_lifecycle_audit(tmp_path):
    db = f"sqlite:///{tmp_path / 'audit.db'}"
    app = create_app(
        Settings(vault_path=tmp_path, database_url=db, api=ApiSettings(enabled=True))
    )
    service = TokenService(db)
    admin = service.create("admin", admin=True, agent_principal="agent-admin")
    reader = service.create("reader")
    service.close()
    headers = {"Authorization": f"Bearer {admin.plaintext}"}
    reader_headers = {"Authorization": f"Bearer {reader.plaintext}"}
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/v1/tokens",
                headers=headers,
                json={"name": "target", "actor": {"source": "forged"}},
            ).status_code
            == 422
        )
        created = client.post(
            "/api/v1/tokens", headers=headers, json={"name": "target"}
        )
        assert created.status_code == 201
        secret = created.json()["token"]
        assert (
            client.get("/api/v1/tokens", headers=headers).json()[-1]["agent_principal"]
            is None
        )
        assert (
            client.patch(
                "/api/v1/tokens/target",
                headers=headers,
                json={"agent_principal": "agent-target"},
            ).status_code
            == 200
        )
        assert (
            client.delete("/api/v1/tokens/target", headers=headers).status_code == 200
        )
        events = [
            e
            for e in client.get("/api/v1/activity", headers=headers).json()["events"]
            if e["event_type"].startswith("token_")
        ]
        reader_events = [
            e
            for e in client.get("/api/v1/activity", headers=reader_headers).json()[
                "events"
            ]
            if e["event_type"].startswith("token_")
        ]
    assert len(events) == 3
    for event in events:
        assert event["payload"]["actor"] == {
            "source": "token",
            "name": "admin",
            "agent_principal": "agent-admin",
        }
        assert event["payload"]["target"]["name"] == "target"
    assert [e["payload"]["target"]["agent_principal"] for e in events] in (
        [None, "agent-target", "agent-target"],
        ["agent-target", "agent-target", None],
    )
    assert admin.plaintext not in json.dumps(events) and secret not in json.dumps(
        events
    )
    assert len(reader_events) == 3
    assert all(event["payload"] == {"redacted": True} for event in reader_events)
    assert "target" not in json.dumps(reader_events)


def test_cli_token_lifecycle_audit(tmp_path, monkeypatch):
    db = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", db)
    runner = CliRunner()
    created = runner.invoke(cli_app, ["token", "create", "--name", "target"])
    assert created.exit_code == 0, created.output
    secret = created.stdout.splitlines()[0]
    updated = runner.invoke(
        cli_app, ["token", "update", "target", "--agent-principal", "agent-target"]
    )
    assert updated.exit_code == 0, updated.output
    revoked = runner.invoke(cli_app, ["token", "revoke", "target"])
    assert revoked.exit_code == 0, revoked.output
    activity = ActivityService(db)
    try:
        events = [
            e.as_dict() for e in activity.history() if e.event_type.startswith("token_")
        ]
    finally:
        activity.close()
    assert len(events) == 3
    assert all(e["payload"]["actor"] == {"source": "CLI"} for e in events)
    assert all(e["payload"]["target"]["name"] == "target" for e in events)
    assert secret not in json.dumps(events)
