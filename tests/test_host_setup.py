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


def test_jsonc_conflict_and_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    root = tmp_path / "cfg" / "opencode"
    root.mkdir(parents=True)
    jsonc = root / "opencode.jsonc"
    jsonc.write_text("// untouched")
    with pytest.raises(OpenCodeSetupError, match="JSONC"):
        setup(token="secret")
    assert jsonc.read_text() == "// untouched"
    jsonc.unlink()
    config = root / "opencode.json"
    config.write_text('{"mcp":{"servers":{"harbor-ledger-memory":{"type":"local"}}}}')
    with pytest.raises(OpenCodeSetupError, match="differs"):
        setup(token="secret")
    setup(token="secret", confirm_replace=lambda: True)


def test_dispatcher_rejects_unsupported_host():
    with pytest.raises(ValueError, match="Unsupported host"):
        setup_host("openclaw")
