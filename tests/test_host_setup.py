import json
import stat

import pytest

from harbor_ledger_memory.host_setup import setup_host
from harbor_ledger_memory.hosts.opencode import OpenCodeSetupError, setup


def test_opencode_merge_is_secure_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    config_dir = tmp_path / "cfg" / "opencode"
    config_dir.mkdir(parents=True)
    config = config_dir / "opencode.json"
    config.write_text('{"other":{"keep":true},"mcp":{"servers":{"other":{"type":"local"}}}}')
    setup(token="secret")
    initial = json.loads(config.read_text())
    setup(token="secret")
    assert json.loads(config.read_text()) == initial
    assert initial["other"] == {"keep": True}
    assert "other" in initial["mcp"]["servers"]
    assert "secret" not in config.read_text()
    token = tmp_path / "cfg" / "harbor-ledger-memory" / "opencode-token"
    assert token.read_text() == "secret"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    assert initial["mcp"]["servers"]["harbor-ledger-memory"] == {
        "type": "remote", "url": "http://127.0.0.1:8765/mcp/", "oauth": False,
        "headers": {"Authorization": f"Bearer {{file:{token}}}"},
    }


@pytest.mark.parametrize(
    "contents",
    ["{bad", '{"x":1,"x":2}', "[]", '{"mcp":null}', '{"mcp":{"servers":[]}}'],
)
def test_bad_shapes_fail_without_config_change(tmp_path, monkeypatch, contents):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / "cfg" / "opencode" / "opencode.json"
    path.parent.mkdir(parents=True)
    path.write_text(contents)
    with pytest.raises(OpenCodeSetupError):
        setup(token="secret")
    assert path.read_text() == contents
    assert not (tmp_path / "cfg" / "harbor-ledger-memory" / "opencode-token").exists()


def test_jsonc_conflict_and_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    root = tmp_path / "cfg" / "opencode"
    root.mkdir(parents=True)
    jsonc = root / "opencode.jsonc"
    jsonc.write_text("// untouched")
    with pytest.raises(OpenCodeSetupError, match="JSONC"):
        setup(token="secret")
    assert jsonc.read_text() == "// untouched"
    assert not (tmp_path / "cfg" / "harbor-ledger-memory" / "opencode-token").exists()
    jsonc.unlink()
    config = root / "opencode.json"
    config.write_text('{"mcp":{"servers":{"harbor-ledger-memory":{"type":"local"}}}}')
    with pytest.raises(OpenCodeSetupError, match="differs"):
        setup(token="secret")
    assert not (tmp_path / "cfg" / "harbor-ledger-memory" / "opencode-token").exists()
    setup(token="secret", confirm_replace=lambda: True)


def test_config_write_failure_rolls_secret_back(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from harbor_ledger_memory.hosts import opencode

    config = tmp_path / "cfg" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}")
    secret = tmp_path / "cfg" / "harbor-ledger-memory" / "opencode-token"
    secret.parent.mkdir()
    secret.write_bytes(b"old\xff")
    secret.chmod(0o640)
    original_write = opencode._atomic_write

    def fail_config(path, content, mode):
        if path == config:
            raise OSError("simulated config failure")
        original_write(path, content, mode)

    monkeypatch.setattr(opencode, "_atomic_write", fail_config)
    with pytest.raises(OSError, match="simulated"):
        setup(token="new")
    assert secret.read_bytes() == b"old\xff"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o640


def test_config_write_failure_removes_new_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from harbor_ledger_memory.hosts import opencode

    original_write = opencode._atomic_write
    config = tmp_path / "cfg" / "opencode" / "opencode.json"

    def fail_config(path, content, mode):
        if path == config:
            raise OSError("simulated config failure")
        original_write(path, content, mode)

    monkeypatch.setattr(opencode, "_atomic_write", fail_config)
    with pytest.raises(OSError, match="simulated"):
        setup(token="new")
    assert not (tmp_path / "cfg" / "harbor-ledger-memory" / "opencode-token").exists()


def test_dispatcher_rejects_unsupported_host():
    with pytest.raises(ValueError, match="Unsupported host"):
        setup_host("unknown")


def test_hermes_profile_config_preserves_keys_and_never_writes_token(tmp_path):
    from harbor_ledger_memory.hosts.hermes import setup as setup_hermes

    plugin = tmp_path / "plugins" / "hlm"
    plugin.mkdir(parents=True)
    config = plugin / "config.json"
    config.write_text('{"review_url":"https://review.example","other":true}')
    result = setup_hermes(hermes_home=tmp_path)
    saved = json.loads(config.read_text())
    assert saved == {
        "review_url": "https://review.example",
        "other": True,
        "base_url": "http://127.0.0.1:8765",
    }
    assert "token" not in config.read_text().lower()
    root = (
        __import__("pathlib").Path(__file__).parents[1]
        / "integrations"
        / "hermes"
        / "hlm"
    )
    for name in ("__init__.py", "client.py"):
        assert (plugin / name).read_bytes() == (root / name).read_bytes()
    assert result == config


def test_openclaw_returns_actionable_manual_instructions(tmp_path, monkeypatch):
    from harbor_ledger_memory.hosts.openclaw import setup as setup_openclaw

    path = tmp_path / ".openclaw" / "openclaw.json"
    path.parent.mkdir()
    path.write_text("{ plugins: { slots: { memory: 'memory-core' } } }")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    instructions = setup_host("openclaw")
    assert path.read_text().endswith("}")
    assert "cd integrations/openclaw" in instructions
    assert "npm pack" in instructions
    assert "npm-pack:./harbor-ledger-memory-openclaw-2.0.0.tgz --force" in instructions
    assert "memory-core" in instructions
    assert 'id: "HLM_TOKEN"' in instructions
    assert "HLM_TOKEN" in setup_openclaw()
    assert "secret" not in path.read_text()
