import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harbor_ledger_memory.cli import app
from harbor_ledger_memory.config import MemorySettings, Settings
from harbor_ledger_memory.services import doctor
from harbor_ledger_memory.services.doctor import run_doctor


def test_doctor_json_has_stable_checks_and_is_read_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    result = CliRunner().invoke(app, ["doctor", "--json"], env={"HOME": str(home)})
    payload = json.loads(result.stdout)
    assert result.exit_code == 2
    assert payload["schema_version"] == 1
    assert {check["name"] for check in payload["checks"]} >= {
        "config",
        "vault",
        "database",
        "embeddings",
        "install",
    }
    assert {
        path: path.read_bytes() for path in home.rglob("*") if path.is_file()
    } == before


def test_doctor_text_shape_and_missing_vault_are_stable(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["doctor"],
        env={"HOME": str(tmp_path / "home")},
    )
    assert result.exit_code == 2
    assert result.stdout.startswith("hlm doctor\n")
    assert "config:" in result.stdout
    assert "exit code: 2" in result.stdout

    report = run_doctor(
        settings=Settings(
            vault_path=tmp_path / "missing-vault",
            database_url=f"sqlite:///{tmp_path / 'missing.db'}",
        )
    )
    assert next(c for c in report["checks"] if c["name"] == "vault")["status"] == "fail"
    assert not (tmp_path / "missing.db").exists()


def test_doctor_embedding_modes_are_actionable_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = Settings(
        vault_path=vault,
        database_url=f"sqlite:///{tmp_path / 'missing.db'}",
        memory=MemorySettings(embedding_model=None),
    )
    lexical = run_doctor(settings=settings)
    assert (
        next(c for c in lexical["checks"] if c["name"] == "embeddings")["status"]
        == "pass"
    )
    configured = settings.model_copy(
        update={"memory": MemorySettings(embedding_model="local-model")}
    )
    monkeypatch.setattr(doctor, "embedding_runtime_available", lambda: False)
    report = run_doctor(settings=configured)
    embedding = next(c for c in report["checks"] if c["name"] == "embeddings")
    assert embedding["status"] == "fail"
    assert not (tmp_path / "missing.db").exists()


def test_doctor_url_probe_reports_stale_version_without_url_or_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def read(self) -> bytes:
            return self.body

    responses = iter(
        (
            Response(b'{"status":"ok"}'),
            Response(b'{"info":{"version":"0.0.0"}}'),
        )
    )
    monkeypatch.setattr(doctor, "urlopen", lambda *_args, **_kwargs: next(responses))
    report = run_doctor(
        settings=Settings(vault_path=tmp_path, database_url="sqlite:///:memory:"),
        url="http://127.0.0.1:8765?token=secret",
    )
    server = next(c for c in report["checks"] if c["name"] == "server")
    assert server["status"] == "warn"
    assert "secret" not in json.dumps(report)


def test_doctor_rejects_remote_url_before_network_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(vault_path=tmp_path, database_url="sqlite:///:memory:")

    def unexpected_request(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("remote URL reached the network")

    monkeypatch.setattr(doctor, "urlopen", unexpected_request)
    report = run_doctor(settings=settings, url="https://example.com:443/health")
    server = next(c for c in report["checks"] if c["name"] == "server")
    assert server["status"] == "fail"
    assert report["exit_code"] == 2
