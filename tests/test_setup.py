import tomllib

import pytest
from typer.testing import CliRunner

from harbor_ledger_memory import setup as setup_impl
from harbor_ledger_memory.cli import app
from harbor_ledger_memory.config import read_persistent_config, update_persistent_config


class Answers:
    def __init__(self, confirms, prompts):
        self.confirms, self.prompts = iter(confirms), iter(prompts)

    def confirm(self, *args, **kwargs):
        return next(self.confirms)

    def prompt(self, *args, **kwargs):
        return next(self.prompts)


def setup_with(monkeypatch, tmp_path, answers, output=None):
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(setup_impl, "config_path", lambda: cfg)
    monkeypatch.setattr(
        setup_impl, "read_persistent_config", lambda: read_persistent_config(cfg)
    )
    monkeypatch.setattr(
        setup_impl,
        "update_persistent_config",
        lambda updates: update_persistent_config(updates, cfg),
    )
    output = output if output is not None else []
    setup_impl.setup(
        confirm=answers.confirm, prompt=answers.prompt, output=output.append
    )
    return cfg, output


def baseconfirms(**options):
    return [
        options.get("ui", False),
        options.get("mcp", False),
        options.get("api", False),
        options.get("local", True),
        options.get("embeddings", False),
        options.get("token", False),
    ]


def test_valid_setup_preserves_existing_config(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'vault_path = "{vault}"\n'
        f'index_root = "AI"\n'
        f'database_url = "sqlite:///{tmp_path}/db.sqlite"\n'
        "server_port = 9001\n"
    )
    output = []
    monkeypatch.setattr(setup_impl, "config_path", lambda: cfg)
    monkeypatch.setattr(
        setup_impl, "read_persistent_config", lambda: read_persistent_config(cfg)
    )
    monkeypatch.setattr(
        setup_impl,
        "update_persistent_config",
        lambda updates: update_persistent_config(updates, cfg),
    )
    a = Answers(baseconfirms(ui=True), [str(vault)])
    setup_impl.setup(confirm=a.confirm, prompt=a.prompt, output=output.append)
    assert tomllib.loads(cfg.read_text())["server_port"] == "9001"
    assert tomllib.loads(cfg.read_text())["frontend"]["enabled"]
    assert any("hlm serve" in line for line in output)


def test_invalid_vault_and_cancellation_do_not_write(monkeypatch, tmp_path):
    answers = Answers([], [str(tmp_path / "missing")])
    with pytest.raises(setup_impl.SetupError, match="Invalid vault directory"):
        setup_with(monkeypatch, tmp_path, answers)
    assert not (tmp_path / "config.toml").exists()
    vault = tmp_path / "v"
    vault.mkdir()
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(setup_impl, "config_path", lambda: cfg)
    monkeypatch.setattr(setup_impl, "read_persistent_config", lambda: {})

    def cancel(*a, **k):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        setup_impl.setup(
            confirm=lambda *a, **k: cancel(),
            prompt=lambda *a, **k: str(vault),
            output=lambda _: None,
        )
    assert not cfg.exists()


def test_nonlocal_aborts_without_write(monkeypatch, tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'vault_path = "{vault}"\nindex_root = "AI"\n')
    monkeypatch.setattr(
        setup_impl, "read_persistent_config", lambda: read_persistent_config(cfg)
    )
    with pytest.raises(setup_impl.SetupError, match="nonlocal"):
        setup_with(
            monkeypatch, tmp_path, Answers([False, False, False, False], [str(vault)])
        )
    assert tomllib.loads(cfg.read_text())["vault_path"] == str(vault)


def test_empty_folder_skips_token_and_outputs_ui_guidance(monkeypatch, tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    cfg, out = setup_with(
        monkeypatch,
        tmp_path,
        Answers(baseconfirms(ui=True, token=True), [str(vault), "named", " "]),
    )
    assert "api_tokens" not in cfg.read_text()
    assert any("token creation skipped" in line for line in out)
    assert any("hlm serve" in line for line in out)
    assert any("Settings > Tokens" in line for line in out)


def test_embedding_missing_aborts_before_write(monkeypatch, tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    monkeypatch.setattr(setup_impl.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(setup_impl.SetupError, match="not installed"):
        setup_with(
            monkeypatch, tmp_path, Answers(baseconfirms(embeddings=True), [str(vault)])
        )
    assert not (tmp_path / "config.toml").exists()


def test_embedding_installed_persists_model(monkeypatch, tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    monkeypatch.setattr(setup_impl.importlib.util, "find_spec", lambda name: object())
    cfg, out = setup_with(
        monkeypatch,
        tmp_path,
        Answers(baseconfirms(embeddings=True), [str(vault), "custom/model"]),
    )
    assert tomllib.loads(cfg.read_text())["memory"]["embedding_model"] == "custom/model"


def test_default_root_token_and_plaintext_only_output(monkeypatch, tmp_path):
    from harbor_ledger_memory.services.tokens import TokenService

    vault = tmp_path / "v"
    vault.mkdir()
    db = f"sqlite:///{tmp_path / 'tokens.db'}"
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(setup_impl, "config_path", lambda: cfg)
    monkeypatch.setattr(
        setup_impl, "read_persistent_config", lambda: {"database_url": db}
    )
    from types import SimpleNamespace

    monkeypatch.setattr(
        setup_impl.Settings,
        "load_from",
        classmethod(lambda cls, path: SimpleNamespace(database_url=db)),
    )
    from harbor_ledger_memory.config import update_persistent_config

    monkeypatch.setattr(
        setup_impl,
        "update_persistent_config",
        lambda updates: update_persistent_config(updates, cfg),
    )
    output = []
    answers = Answers(baseconfirms(token=True), [str(vault), "private", "notes"])
    setup_impl.setup(
        confirm=answers.confirm, prompt=answers.prompt, output=output.append
    )
    service = TokenService(db)
    try:
        record = service.list()[0]
        assert [(r.path.as_posix(), r.access.value) for r in record.rules] == [
            (".", "none"),
            ("notes", "read"),
        ]
    finally:
        service.close()
    secret = output[0]
    assert secret.startswith("hlm_") and secret not in cfg.read_text()
    assert "secret database" not in " ".join(output)


def test_token_rules_scoped_to_loaded_index_root(monkeypatch, tmp_path):
    from harbor_ledger_memory.services.access import AccessPolicy
    from harbor_ledger_memory.services.tokens import TokenService

    vault = tmp_path / "v"
    vault.mkdir()
    db = f"sqlite:///{tmp_path / 'tokens.db'}"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'vault_path = "{vault}"\nindex_root = "AI"\ndatabase_url = "{db}"\n'
    )
    monkeypatch.setattr(setup_impl, "config_path", lambda: cfg)
    monkeypatch.setattr(
        setup_impl, "read_persistent_config", lambda: read_persistent_config(cfg)
    )
    from harbor_ledger_memory.config import update_persistent_config

    monkeypatch.setattr(
        setup_impl,
        "update_persistent_config",
        lambda updates: update_persistent_config(updates, cfg),
    )
    a = Answers(baseconfirms(token=True), [str(vault), "scoped", "AI/Notes"])
    setup_impl.setup(confirm=a.confirm, prompt=a.prompt, output=lambda _: None)
    service = TokenService(db)
    try:
        record = service.list()[0]
        assert not record.admin
        assert [(r.path.as_posix(), r.access.value) for r in record.rules] == [
            (".", "none"),
            ("AI/Notes", "read"),
        ]
        policy = AccessPolicy(record.rules, admin=record.admin)
        assert policy.can_read("AI/Notes/page.md")
        assert not policy.can_read("AI/Other/page.md")
        assert not policy.can_read("Outside/page.md")
    finally:
        service.close()


def test_invalid_folder_exact_message_before_write(monkeypatch, tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'vault_path = "{vault}"\nindex_root = "AI"\n')
    monkeypatch.setattr(setup_impl, "config_path", lambda: cfg)
    monkeypatch.setattr(
        setup_impl, "read_persistent_config", lambda: read_persistent_config(cfg)
    )
    with pytest.raises(setup_impl.SetupError, match="Invalid folder 'Outside'"):
        setup_impl.setup(
            confirm=Answers(baseconfirms(token=True), []).confirm,
            prompt=Answers([], [str(vault), "x", "Outside"]).prompt,
            output=lambda _: None,
        )
    assert tomllib.loads(cfg.read_text())["index_root"] == "AI"


def test_cli_token_failure_has_recovery_without_traceback(monkeypatch, tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    monkeypatch.setattr(setup_impl, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(setup_impl, "read_persistent_config", lambda: {})
    saved = []
    monkeypatch.setattr(
        setup_impl, "update_persistent_config", lambda updates: saved.append(updates)
    )

    class Failed:
        def __init__(self, *a):
            pass

        def create(self, *a, **k):
            raise OSError("secret database detail")

        def close(self):
            raise RuntimeError("cleanup detail")

    monkeypatch.setattr(setup_impl, "TokenService", Failed)
    from types import SimpleNamespace

    monkeypatch.setattr(
        setup_impl.Settings,
        "load_from",
        classmethod(lambda cls, path: SimpleNamespace(database_url="sqlite:///unused")),
    )
    confirms = iter([False, False, False, True, False, True])
    prompts = iter([str(vault), "name", "Folder"])
    runner = CliRunner()
    monkeypatch.setattr(setup_impl, "read_persistent_config", lambda: {})
    monkeypatch.setattr(
        "harbor_ledger_memory.cli.run_setup",
        lambda: setup_impl.setup(
            confirm=lambda *a, **k: next(confirms),
            prompt=lambda *a, **k: next(prompts),
            output=print,
        ),
    )
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 1
    assert (
        "Rerun `hlm setup`" in result.output
        and "secret database detail" not in result.output
    )
    assert "Traceback" not in result.output
    assert saved


def test_close_failure_still_displays_created_token(monkeypatch, tmp_path):
    from types import SimpleNamespace

    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(setup_impl, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(setup_impl, "read_persistent_config", lambda: {})
    monkeypatch.setattr(setup_impl, "update_persistent_config", lambda updates: None)
    monkeypatch.setattr(
        setup_impl.Settings,
        "load_from",
        classmethod(lambda cls, path: SimpleNamespace(database_url="sqlite:///unused")),
    )

    class Created:
        def __init__(self, *args):
            pass

        def create(self, *args, **kwargs):
            return SimpleNamespace(plaintext="hlm_one_time_secret")

        def close(self):
            raise RuntimeError("cleanup detail")

    monkeypatch.setattr(setup_impl, "TokenService", Created)
    output = []
    answers = Answers(baseconfirms(token=True), [str(vault), "name", "Folder"])
    setup_impl.setup(
        confirm=answers.confirm, prompt=answers.prompt, output=output.append
    )

    assert output[0] == "hlm_one_time_secret"
    assert any("cleanup failed" in line for line in output)


@pytest.mark.parametrize("host", ["opencode", "openclaw", "hermes"])
def test_selected_host_creates_scoped_token_without_optional_prompt(
    monkeypatch, tmp_path, host
):
    from types import SimpleNamespace

    vault = tmp_path / "v"
    vault.mkdir()
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(setup_impl, "config_path", lambda: cfg)
    monkeypatch.setattr(setup_impl, "read_persistent_config", lambda: {})
    monkeypatch.setattr(setup_impl, "update_persistent_config", lambda _: None)
    monkeypatch.setattr(
        setup_impl.Settings, "load_from",
        classmethod(lambda cls, path: SimpleNamespace(database_url="unused")),
    )
    calls = []

    class Tokens:
        def __init__(self, _url): pass
        def create(self, name, rules, admin):
            calls.append((name, rules, admin))
            return SimpleNamespace(plaintext="hlm_once")
        def close(self): pass

    monkeypatch.setattr(setup_impl, "TokenService", Tokens)
    monkeypatch.setattr(
        "harbor_ledger_memory.host_setup.setup_host",
        lambda selected, **kwargs: calls.append((selected, kwargs))
        or "Host instructions",
    )
    prompts = iter([str(vault), host, "host-token", "Notes"])
    confirms = iter([False, True, False, True, False])
    output = []
    setup_impl.setup(confirm=lambda *a, **k: next(confirms),
                     prompt=lambda *a, **k: next(prompts), output=output.append)
    assert calls[0][0] == "host-token"
    assert [rule.path.as_posix() for rule in calls[0][1]] == [".", "Notes"]
    assert calls[1][0] == host and calls[1][1]["token"] == "hlm_once"
    if host == "opencode":
        assert "hlm_once" not in " ".join(output)
    else:
        assert "hlm_once" in output
    if host != "opencode":
        assert any("shown once" in line for line in output)
        assert any("Host instructions" in line for line in output)


@pytest.mark.parametrize("host", ["opencode", "openclaw", "hermes"])
def test_selected_host_empty_folders_fails_before_config_write(
    monkeypatch, tmp_path, host
):
    vault = tmp_path / "v"
    vault.mkdir()
    monkeypatch.setattr(setup_impl, "read_persistent_config", lambda: {})
    with pytest.raises(setup_impl.SetupError, match="at least one scoped folder"):
        setup_with(monkeypatch, tmp_path,
                   Answers([False, True, False, True, False, False],
                           [str(vault), host, "name", " "]))
    assert not (tmp_path / "config.toml").exists()


def test_opencode_adapter_error_outputs_one_time_token_and_manual_recovery(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    vault = tmp_path / "v"
    vault.mkdir()
    monkeypatch.setattr(setup_impl, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(setup_impl, "read_persistent_config", lambda: {})
    monkeypatch.setattr(setup_impl, "update_persistent_config", lambda _: None)
    monkeypatch.setattr(
        setup_impl.Settings,
        "load_from",
        classmethod(lambda cls, path: SimpleNamespace(database_url="unused")),
    )
    class Tokens:
        def __init__(self, _url): pass
        def create(self, *a, **k): return SimpleNamespace(plaintext="once")
        def close(self): pass
    monkeypatch.setattr(setup_impl, "TokenService", Tokens)
    monkeypatch.setattr("harbor_ledger_memory.host_setup.setup_host",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("details")))
    output = []
    with pytest.raises(setup_impl.SetupError, match="manually merge") as error:
        setup_impl.setup(
            confirm=Answers([False, True, False, True, False], []).confirm,
            prompt=Answers([], [str(vault), "opencode", "name", "Folder"]).prompt,
            output=output.append,
        )
    assert output.count("once") == 1
    assert "opencode-token" not in " ".join(output)
    assert "hlm token revoke name" in str(error.value)


def test_declined_opencode_replacement_is_cancellation_with_revoke_guidance(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from harbor_ledger_memory.hosts.opencode import OpenCodeSetupCancelled

    vault = tmp_path / "v"
    vault.mkdir()
    monkeypatch.setattr(setup_impl, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(setup_impl, "read_persistent_config", lambda: {})
    monkeypatch.setattr(setup_impl, "update_persistent_config", lambda _: None)
    monkeypatch.setattr(
        setup_impl.Settings, "load_from",
        classmethod(lambda cls, path: SimpleNamespace(database_url="unused")),
    )

    class Tokens:
        def __init__(self, _url): pass
        def create(self, *a, **k): return SimpleNamespace(plaintext="once")
        def close(self): pass

    monkeypatch.setattr(setup_impl, "TokenService", Tokens)

    def decline(*args, **kwargs):
        assert kwargs["confirm_replace"]() is False
        raise OpenCodeSetupCancelled("declined")

    monkeypatch.setattr("harbor_ledger_memory.host_setup.setup_host", decline)
    output = []
    with pytest.raises(setup_impl.SetupError, match="cancelled") as error:
        setup_impl.setup(
            confirm=Answers([False, True, False, True, False, False], []).confirm,
            prompt=Answers([], [str(vault), "opencode", "name", "Folder"]).prompt,
            output=output.append,
        )
    assert output.count("once") == 1
    assert "hlm token revoke name" in str(error.value)


def test_mcp_disabled_does_not_ask_for_host(monkeypatch, tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    setup_with(monkeypatch, tmp_path,
               Answers([False, False, False, True, False, False], [str(vault)]))
    assert (tmp_path / "config.toml").exists()
