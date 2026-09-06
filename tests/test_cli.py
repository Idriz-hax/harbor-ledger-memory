"""Temporary-vault coverage for the typed read-only CLI."""

import json
import subprocess
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import click
import pytest
from typer.testing import CliRunner

from harbor_ledger_memory import cli
from harbor_ledger_memory.cli import app
from harbor_ledger_memory.config import (
    FolderAccess,
    FolderRule,
    FrontendSettings,
    NetworkSettings,
    Settings,
)


def build_wheel(destination: Path) -> Path:
    project_root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(destination)],
        cwd=project_root,
        check=True,
    )
    wheels = list(destination.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _make_vault(root: Path) -> None:
    ai = root / "AI" / "Knowledge"
    ai.mkdir(parents=True)
    (root / "AI" / "INDEX.md").write_text(
        "# Root\n\n[[AI/Knowledge/example]]\n", encoding="utf-8"
    )
    (ai / "example.md").write_text(
        "---\ntype: knowledge\nstatus: active\n---\n# Example\n\nrecursive index\n",
        encoding="utf-8",
    )
    (root / "not-in-ai.md").write_text("private", encoding="utf-8")


def test_root_help_describes_vault_scope_and_local_management() -> None:
    result = CliRunner().invoke(app, ["--help"], color=False)

    assert result.exit_code == 0
    assert "Usage: hlm" in click.unstyle(result.output)
    assert (
        "Read-only commands for the configured Obsidian vault scope"
        not in result.output
    )
    assert "vault scope" in result.output.lower()
    assert "local application settings" in result.output.lower()


def test_cli_commands_use_the_read_only_service_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    _make_vault(tmp_path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("HLM_INDEX_ROOT", "AI")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    runner = CliRunner()

    scan = runner.invoke(app, ["scan", "--json"])
    assert scan.exit_code == 0, scan.stdout
    assert json.loads(scan.stdout)["files_indexed"] == 2

    node = runner.invoke(app, ["show-node", "AI/INDEX.md", "--json"])
    assert node.exit_code == 0, node.stdout
    assert json.loads(node.stdout)["path"] == "AI/INDEX.md"

    neighbours = runner.invoke(app, ["show-neighbours", "AI/INDEX.md", "--json"])
    assert neighbours.exit_code == 0, neighbours.stdout
    assert json.loads(neighbours.stdout)[0]["path"] == "AI/Knowledge/example.md"

    search = runner.invoke(app, ["search", "recursive index", "--json"])
    assert search.exit_code == 0, search.stdout
    assert json.loads(search.stdout)[0]["path"] == "AI/Knowledge/example.md"

    validation = runner.invoke(app, ["validate", "--format", "json"])
    assert validation.exit_code == 0, validation.stdout
    assert "findings" in json.loads(validation.stdout)

    rejected = runner.invoke(app, ["show-node", "AI/../not-in-ai.md", "--json"])
    assert rejected.exit_code != 0


def test_cli_settings_failure_is_a_clean_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path / "missing"))
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code != 0
    assert "vault path does not exist" in result.output


def test_cli_status_reports_configured_write_policy(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("HLM_FOLDER_RULES", '[{"path":"AI","access":"propose-write"}]')
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    result = CliRunner().invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["read_only"] is False
    assert payload["write_policy"] == {
        "default_access": "read",
        "rules": [{"path": "AI", "access": "propose-write"}],
    }


def test_cli_status_counts_only_admitted_paths(monkeypatch, tmp_path: Path) -> None:
    _make_vault(tmp_path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("HLM_INDEX_ROOT", "AI")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    runner = CliRunner()
    assert runner.invoke(app, ["scan", "--json"]).exit_code == 0
    status = runner.invoke(app, ["status", "--json"])
    assert status.exit_code == 0, status.stdout
    assert json.loads(status.stdout)["indexed_notes"] == 2


def test_short_term_add_is_unavailable() -> None:
    result = CliRunner().invoke(app, ["short-term", "add", "AI/note.md"])

    assert result.exit_code != 0


def test_cli_invalid_feedback_is_reported_cleanly(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    result = CliRunner().invoke(
        app, ["feedback", "missing-trace", "--relevant", "AI/missing.md"]
    )
    assert result.exit_code != 0
    assert "Trace missing-trace not found" in result.output


def test_cli_config_set_and_show_use_persistent_toml(
    tmp_path: Path, monkeypatch
) -> None:
    _make_vault(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    runner = CliRunner()

    saved = runner.invoke(
        app,
        [
            "config",
            "set",
            "--index-root",
            "AI",
            "--folder-rules",
            '[{"path":"AI/Private","access":"deny"}]',
        ],
    )
    assert saved.exit_code == 0, saved.stdout
    assert "restart required" in saved.stdout

    shown = runner.invoke(app, ["config", "show", "--json"])
    assert shown.exit_code == 0, shown.stdout
    payload = json.loads(shown.stdout)
    assert payload["index_root"] == "AI"
    # The retired "deny" level is accepted for settings and normalized to
    # its canonical equivalent "none" on load.
    assert payload["folder_rules"][0]["access"] == "none"


def test_cli_config_set_runtime_loopback_and_mcp_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_vault(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    runner = CliRunner()

    saved = runner.invoke(
        app,
        [
            "config",
            "set",
            "--frontend-enabled",
            "--frontend-mode",
            "loopback",
            "--mcp-enabled",
            "--server-host",
            "127.0.0.1",
            "--server-port",
            "9876",
        ],
    )
    assert saved.exit_code == 0, saved.output
    shown = runner.invoke(app, ["config", "show", "--json"])
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.output)
    assert payload["frontend"] == {
        "enabled": True,
        "mode": "loopback",
        "public_origin": None,
    }
    assert payload["mcp"] == {"enabled": True}
    assert payload["server_host"] == "127.0.0.1"
    assert payload["server_port"] == 9876
    assert "password_verifier" not in shown.output
    loaded = Settings.load_from(
        tmp_path / "home/.config/harbor-ledger-memory/config.toml"
    )
    assert loaded.frontend.enabled is True
    assert loaded.mcp.enabled is True


def test_cli_config_set_runtime_lan_round_trip_and_tri_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_vault(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    runner = CliRunner()

    saved = runner.invoke(
        app,
        [
            "config",
            "set",
            "--frontend-mode",
            "lan",
            "--public-origin",
            "http://memory.local",
            "--network-enabled",
            "--network-external",
            "--network-host",
            "0.0.0.0",
            "--network-port",
            "9443",
            "--tls-cert",
            "/etc/hlm/cert.pem",
            "--tls-key",
            "/etc/hlm/key.pem",
            "--allowed-cidrs",
            '["192.168.1.0/24"]',
            "--insecure-http",
            "--server-host",
            "0.0.0.0",
            "--server-port",
            "9443",
        ],
    )
    assert saved.exit_code == 0, saved.output
    unchanged = runner.invoke(app, ["config", "set", "--mcp-enabled"])
    assert unchanged.exit_code == 0, unchanged.output
    loaded = Settings.load_from(
        tmp_path / "home/.config/harbor-ledger-memory/config.toml"
    )
    assert loaded.frontend.mode == "lan"
    assert loaded.frontend.public_origin == "http://memory.local"
    assert loaded.network.enabled is True
    assert loaded.network.allowed_cidrs == ("192.168.1.0/24",)
    assert loaded.network.insecure_http is True
    shown = runner.invoke(app, ["config", "show", "--json"])
    payload = json.loads(shown.output)
    assert payload["server_host"] == "0.0.0.0"
    assert payload["server_port"] == 9443
    assert payload["network"] == {
        "enabled": True,
        "external": True,
        "host": "0.0.0.0",
        "port": 9443,
        "allowed_cidrs": ["192.168.1.0/24"],
        "tls_cert": "/etc/hlm/cert.pem",
        "tls_key": "/etc/hlm/key.pem",
        "insecure_http": True,
    }
    assert loaded.network.tls_cert == "/etc/hlm/cert.pem"
    assert loaded.network.tls_key == "/etc/hlm/key.pem"


def test_cli_config_set_password_stores_only_argon2_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "correct horse")
    result = CliRunner().invoke(app, ["config", "set-password"])
    assert result.exit_code == 0, result.output
    config = tmp_path / ".config/harbor-ledger-memory/config.toml"
    text = config.read_text()
    assert "$argon2id$" in text
    assert "correct horse" not in text
    assert result.output.count("correct horse") == 0
    assert config.stat().st_mode & 0o777 == 0o600


def test_cli_config_set_password_rejects_insecure_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    config = tmp_path / ".config/harbor-ledger-memory/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("frontend = {}\n")
    config.chmod(0o644)
    result = CliRunner().invoke(app, ["config", "set-password"])
    assert result.exit_code != 0
    assert "0600" in result.output


def test_cli_installs_bundled_opencode_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    result = CliRunner().invoke(app, ["status"])
    target = tmp_path / ".config/opencode/skills/harbor-ledger-memory/SKILL.md"
    assert result.exit_code == 0, result.stdout
    assert target.read_text().startswith("---\nname: harbor-ledger-memory")


def test_cli_keeps_running_when_skill_sync_fails(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        cli,
        "sync_opencode_skill",
        lambda: (_ for _ in ()).throw(OSError("denied")),
    )
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.stdout
    assert "could not synchronize" in result.stderr


def test_bundled_skill_documents_read_tools_and_write_semantics() -> None:
    skill = Path(
        "backend/src/harbor_ledger_memory/skills/harbor-ledger-memory/SKILL.md"
    ).read_text(encoding="utf-8").lower()
    for tool in ("status", "settings_snapshot", "query", "neighbours", "scan"):
        assert tool in skill
    for access in ("default", "read", "deny"):
        assert access in skill
    assert "pending approval" in skill
    assert "auto-write" in skill
    assert "apply immediately" in skill


def test_serve_rejects_non_loopback_host(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        cli,
        "_load_settings",
        lambda: Settings(vault_path=tmp_path, frontend=FrontendSettings(enabled=True)),
    )
    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code != 0
    assert "loopback" in result.output.lower()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_serve_allows_loopback_hosts(monkeypatch, tmp_path: Path, host: str) -> None:
    calls: list[tuple[object, str, int, str]] = []
    monkeypatch.setattr(
        cli,
        "_load_settings",
        lambda: Settings(
            vault_path=tmp_path, frontend=FrontendSettings(enabled=True)
        ),
    )
    monkeypatch.setattr(cli, "create_app", lambda settings, **kwargs: object())
    monkeypatch.setattr(
        "uvicorn.run",
        lambda application, *, host, port, log_level: calls.append(
            (application, host, port, log_level)
        ),
    )
    result = CliRunner().invoke(app, ["serve", "--host", host])
    assert result.exit_code == 0, result.output
    assert calls[0][1] == host


def test_serve_does_not_open_browser_or_print_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "_load_settings", lambda: _serve_settings(tmp_path))
    monkeypatch.setattr(cli, "create_app", lambda settings, **kwargs: object())
    monkeypatch.setattr("uvicorn.run", lambda application, **kwargs: None)
    result = CliRunner().invoke(app, ["serve", "--host", "::1", "--port", "9876"])
    assert result.exit_code == 0, result.output
    assert "#hlm-" not in result.stdout
    assert "#hlm-" not in result.stderr


def _serve_settings(
    tmp_path: Path, folder_rules: tuple[FolderRule, ...] = ()
) -> Settings:
    return Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'tokens.db'}",
        folder_rules=folder_rules,
        frontend=FrontendSettings(enabled=True),
    )


def test_serve_requires_frontend_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "_load_settings", lambda: Settings(vault_path=tmp_path))
    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code != 0
    assert "frontend" in result.output.lower()


@pytest.mark.parametrize(
    "settings, host, message",
    [
        (
            Settings(vault_path=Path("."), frontend=FrontendSettings(enabled=True)),
            "0.0.0.0",
            "loopback",
        ),
        (
            Settings(
                vault_path=Path("."),
                frontend=FrontendSettings(enabled=True, mode="lan"),
            ),
            "127.0.0.1",
            "non-loopback",
        ),
        (
            Settings(
                vault_path=Path("."),
                frontend=FrontendSettings(enabled=True, mode="lan"),
                network=NetworkSettings(enabled=False),
            ),
            "192.168.1.10",
            "network",
        ),
        (
            Settings(
                vault_path=Path("."),
                frontend=FrontendSettings(enabled=True, mode="lan"),
                network=NetworkSettings(enabled=True),
            ),
            "192.168.1.10",
            "password",
        ),
    ],
)
def test_serve_rejects_invalid_mode_combinations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    settings: Settings,
    host: str,
    message: str,
) -> None:
    settings = settings.model_copy(update={"vault_path": tmp_path})
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)
    result = CliRunner().invoke(app, ["serve", "--host", host])
    assert result.exit_code != 0
    assert message in result.output.lower()


def test_serve_rejects_invalid_lan_origin_and_tls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings.model_construct(
        vault_path=tmp_path,
        frontend=FrontendSettings(
            enabled=True,
            mode="lan",
            public_origin="not-an-origin",
            password_verifier="x",
        ),
        network=NetworkSettings(enabled=True),
    )
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)
    result = CliRunner().invoke(app, ["serve", "--host", "192.168.1.10"])
    assert result.exit_code != 0
    assert "public_origin" in result.output


@pytest.mark.parametrize("origin", ["https://hlm.local:bad", "https://hlm.local/"])
def test_serve_rejects_malformed_or_pathful_lan_origins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, origin: str
) -> None:
    settings = Settings.model_construct(
        vault_path=tmp_path,
        frontend=FrontendSettings(
            enabled=True,
            mode="lan",
            public_origin=origin,
            password_verifier="x",
        ),
        network=NetworkSettings(enabled=True, insecure_http=True),
    )
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)
    result = CliRunner().invoke(app, ["serve", "--host", "192.168.1.10"])
    assert result.exit_code != 0
    assert "public_origin" in result.output or "origin" in result.output


def test_serve_validates_cidr_allowlist_before_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings.model_construct(
        vault_path=tmp_path,
        frontend=FrontendSettings(
            enabled=True,
            mode="lan",
            public_origin="https://hlm.local",
            password_verifier="x",
        ),
        network=NetworkSettings.model_construct(
            enabled=True, allowed_cidrs=("not-a-cidr",), insecure_http=True
        ),
    )
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)
    result = CliRunner().invoke(app, ["serve", "--host", "192.168.1.10"])
    assert result.exit_code != 0
    assert "cidr" in result.output.lower()


def test_serve_valid_loopback_wires_transport_before_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "_load_settings", lambda: _serve_settings(tmp_path))
    monkeypatch.setattr(cli, "create_app", lambda settings, **kwargs: object())
    monkeypatch.setattr(
        "uvicorn.run", lambda application, **kwargs: calls.append(kwargs)
    )
    result = CliRunner().invoke(app, ["serve", "--host", "127.0.0.1", "--port", "9876"])
    assert result.exit_code == 0, result.output
    assert calls == [{"host": "127.0.0.1", "port": 9876, "log_level": "info"}]


def test_serve_valid_https_lan_wires_tls_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("cert")
    key.write_text("key")
    settings = Settings(
        vault_path=tmp_path,
        frontend=FrontendSettings(
            enabled=True,
            mode="lan",
            public_origin="HTTPS://HLM.LOCAL:443",
            password_verifier="$argon2id$v=19$m=1,t=1,p=1$x$y",
        ),
        network=NetworkSettings(
            enabled=True,
            tls_cert=str(cert),
            tls_key=str(key),
        ),
    )
    calls: list[dict[str, object]] = []
    origins: list[str] = []
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "create_app",
        lambda settings, **kwargs: (origins.append(kwargs["ui_origin"]) or object()),
    )
    monkeypatch.setattr(
        "uvicorn.run", lambda application, **kwargs: calls.append(kwargs)
    )
    result = CliRunner().invoke(app, ["serve", "--host", "192.168.1.10"])
    assert result.exit_code == 0, result.output
    assert origins == ["https://hlm.local"]
    assert calls[0]["ssl_certfile"] == str(cert)
    assert calls[0]["ssl_keyfile"] == str(key)


def test_serve_valid_insecure_http_lan_omits_ssl_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(
        vault_path=tmp_path,
        frontend=FrontendSettings(
            enabled=True,
            mode="lan",
            public_origin="http://HLM.LOCAL:80",
            password_verifier="x",
        ),
        network=NetworkSettings(enabled=True, insecure_http=True),
    )
    calls: list[dict[str, object]] = []
    origins: list[str] = []
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "create_app",
        lambda settings, **kwargs: (origins.append(kwargs["ui_origin"]) or object()),
    )
    monkeypatch.setattr(
        "uvicorn.run", lambda application, **kwargs: calls.append(kwargs)
    )

    result = CliRunner().invoke(app, ["serve", "--host", "192.168.1.10"])

    assert result.exit_code == 0, result.output
    assert origins == ["http://hlm.local"]
    assert "ssl_certfile" not in calls[0]
    assert "ssl_keyfile" not in calls[0]


def test_serve_rejects_http_lan_without_acknowledgement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings.model_construct(
        vault_path=tmp_path,
        frontend=FrontendSettings(
            enabled=True,
            mode="lan",
            public_origin="http://hlm.local",
            password_verifier="x",
        ),
        network=NetworkSettings.model_construct(enabled=True, insecure_http=False),
    )
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)

    result = CliRunner().invoke(app, ["serve", "--host", "192.168.1.10"])

    assert result.exit_code != 0
    assert "insecure_http" in result.output


def test_serve_backfills_legacy_tokens_before_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sqlalchemy import create_engine, text

    from harbor_ledger_memory.services.tokens import TokenService

    # A pre-rules row (rules IS NULL) as old clients left it: create() no
    # longer accepts scope strings, so seed the row directly.
    service = TokenService(f"sqlite:///{tmp_path / 'tokens.db'}")
    service.close()
    engine = create_engine(f"sqlite:///{tmp_path / 'tokens.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO api_tokens "
                "(name, token_hash, scopes, created_at, revoked_at) "
                "VALUES ('legacy', 'hash-1', '[\"read\", \"admin\"]', "
                "'2026-08-31T00:00:00+00:00', NULL)"
            )
        )
    engine.dispose()
    expected = FolderRule(
        path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE
    )
    monkeypatch.setattr(
        cli,
        "_load_settings",
        lambda: _serve_settings(tmp_path, folder_rules=(expected,)),
    )

    def fake_app(settings: Settings, **kwargs: object) -> object:
        return object()

    def fake_run(
        application: object, *, host: str, port: int, log_level: str
    ) -> None:
        return None

    monkeypatch.setattr(cli, "create_app", fake_app)
    monkeypatch.setattr("uvicorn.run", fake_run)
    result = CliRunner().invoke(app, ["serve", "--host", "127.0.0.1"])
    assert result.exit_code == 0, result.output
    service = TokenService(f"sqlite:///{tmp_path / 'tokens.db'}")
    try:
        record = service.list()[0]
    finally:
        service.close()
    assert record.rules == (expected,)
    assert record.admin is True


def test_bundled_skill_documents_mcp_workflow() -> None:
    skill = Path(
        "backend/src/harbor_ledger_memory/skills/harbor-ledger-memory/SKILL.md"
    ).read_text()
    for term in (
        "status",
        "query",
        "scan",
        "propose_write",
        "approve_proposal",
        "reject_proposal",
        "folder rule",
        "reconnect",
    ):
        assert term in skill.lower()


def test_bundled_skill_documents_feedback_contract() -> None:
    skill = Path(
        "backend/src/harbor_ledger_memory/skills/harbor-ledger-memory/SKILL.md"
    ).read_text()
    for term in (
        "feedback",
        "trace_id",
        "relevant_paths",
        "irrelevant_paths",
        "auto-write",
    ):
        assert term in skill.lower()


def test_wheel_contains_obsidian_memory_skill(tmp_path: Path) -> None:
    wheel = build_wheel(tmp_path)
    with ZipFile(wheel) as archive:
        assert (
            "harbor_ledger_memory/skills/harbor-ledger-memory/SKILL.md"
            in archive.namelist()
        )


def test_cli_token_create_list_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_vault(tmp_path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    runner = CliRunner()

    created = runner.invoke(
        app,
        [
            "token",
            "create",
            "--name",
            "opencode",
            "--admin",
            "--rule",
            "AI=auto-write",
            "--rule",
            "Inbox=none",
        ],
    )
    assert created.exit_code == 0, created.stdout
    first_line = created.stdout.strip().splitlines()[0]
    assert first_line.startswith("hlm_")
    assert "only once" in created.stdout

    listed = runner.invoke(app, ["token", "list", "--json"])
    assert listed.exit_code == 0, listed.stdout
    rows = json.loads(listed.stdout)
    assert [row["name"] for row in rows] == ["opencode"]
    assert rows[0]["admin"] is True
    assert rows[0]["rules"] == [
        {"path": "AI", "access": "auto-write"},
        {"path": "Inbox", "access": "none"},
    ]
    assert all("token_hash" not in row and "token" not in row for row in rows)

    # The printed token actually authenticates against the same store.
    from harbor_ledger_memory.services.tokens import TokenService

    service = TokenService(f"sqlite:///{tmp_path / 'tokens.db'}")
    try:
        record = service.verify(first_line)
        assert record is not None
        assert record.admin is True
    finally:
        service.close()

    revoked = runner.invoke(app, ["token", "revoke", "opencode"])
    assert revoked.exit_code == 0, revoked.stdout
    assert "revoked" in revoked.stdout.lower()
    missing = runner.invoke(app, ["token", "revoke", "ghost"])
    assert missing.exit_code != 0


def test_cli_token_create_rule_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_vault(tmp_path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    runner = CliRunner()

    invalid_level = runner.invoke(
        app, ["token", "create", "--name", "x", "--rule", "AI=superuser"]
    )
    assert invalid_level.exit_code != 0
    for level in ("none", "read", "propose-write", "auto-write"):
        assert level in invalid_level.output

    # The retired "deny" level is rejected with the valid levels listed.
    legacy_deny = runner.invoke(
        app, ["token", "create", "--name", "x", "--rule", "AI=deny"]
    )
    assert legacy_deny.exit_code != 0
    assert "invalid level 'deny'" in legacy_deny.output
    for level in ("none", "read", "propose-write", "auto-write"):
        assert level in legacy_deny.output

    no_separator = runner.invoke(
        app, ["token", "create", "--name", "x", "--rule", "AI"]
    )
    assert no_separator.exit_code != 0
    assert "PATH=LEVEL" in no_separator.output

    empty_path = runner.invoke(
        app, ["token", "create", "--name", "x", "--rule", "=read"]
    )
    assert empty_path.exit_code != 0

    absolute_path = runner.invoke(
        app, ["token", "create", "--name", "x", "--rule", "/etc=none"]
    )
    assert absolute_path.exit_code != 0

    ok = runner.invoke(
        app, ["token", "create", "--name", "x", "--rule", "AI/Notes=propose-write"]
    )
    assert ok.exit_code == 0, ok.stdout


def test_cli_token_create_defaults_to_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_vault(tmp_path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    runner = CliRunner()

    created = runner.invoke(app, ["token", "create", "--name", "reader"])
    assert created.exit_code == 0, created.stdout
    listed = runner.invoke(app, ["token", "list", "--json"])
    assert listed.exit_code == 0, listed.stdout
    rows = json.loads(listed.stdout)
    assert rows[0]["rules"] == []
    assert rows[0]["admin"] is False


def test_cli_token_list_renders_admin_badge_and_rule_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_vault(tmp_path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    runner = CliRunner()
    created = runner.invoke(
        app,
        [
            "token",
            "create",
            "-n",
            "boss",
            "--admin",
            "--rule",
            "AI=auto-write",
            "--rule",
            "Inbox=none",
        ],
    )
    assert created.exit_code == 0, created.stdout
    assert runner.invoke(app, ["token", "create", "-n", "reader"]).exit_code == 0

    listed = runner.invoke(app, ["token", "list"])
    assert listed.exit_code == 0, listed.stdout
    assert "boss" in listed.stdout
    assert "[ADMIN]" in listed.stdout
    assert "AI: auto-write, Inbox: none" in listed.stdout
    assert "read-only" in listed.stdout
    reader_line = next(
        line for line in listed.stdout.splitlines() if line.startswith("reader")
    )
    assert "[ADMIN]" not in reader_line
