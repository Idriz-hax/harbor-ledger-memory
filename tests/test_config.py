import importlib
from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError

from harbor_ledger_memory.config import (
    ApiSettings,
    FolderAccess,
    FolderRule,
    FrontendSettings,
    McpSettings,
    MemorySettings,
    NetworkSettings,
    Settings,
    config_path,
    update_persistent_config,
)


def test_harbor_ledger_memory_identity() -> None:
    assert importlib.import_module("harbor_ledger_memory")
    assert config_path() == (
        Path.home() / ".config" / "harbor-ledger-memory" / "config.toml"
    )


def test_hlm_vault_env_alias_loads_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.delenv("LEGACY_VAULT_PATH", raising=False)

    loaded = Settings.load_from(tmp_path / "missing.toml")

    assert loaded.vault_path == tmp_path


def test_legacy_vault_env_alias_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HLM_VAULT_PATH", raising=False)
    monkeypatch.setenv("LEGACY_VAULT_PATH", str(tmp_path))

    with pytest.raises(ValidationError, match="HLM_VAULT_PATH"):
        Settings.load_from(tmp_path / "missing.toml")


def test_server_features_are_disabled_by_default() -> None:
    settings = Settings(vault_path=Path("."))

    assert settings.frontend == FrontendSettings()
    assert settings.api == ApiSettings()
    assert settings.mcp == McpSettings()
    assert settings.network == NetworkSettings()


def test_server_settings_persist_as_nested_toml_and_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    update_persistent_config(
        {
            "frontend": {"enabled": True, "mode": "lan"},
            "api": {"enabled": True},
            "mcp": {"enabled": True},
            "network": {"external": True, "host": "0.0.0.0"},
        },
        path,
    )
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("HLM_FRONTEND__ENABLED", "false")
    monkeypatch.setenv("HLM_API__ENABLED", "true")
    monkeypatch.setenv("HLM_NETWORK__HOST", "127.0.0.1")
    loaded = Settings.load_from(path)
    assert loaded.frontend.enabled is False
    assert loaded.api.enabled is True
    assert loaded.mcp.enabled is True
    assert loaded.network.host == "127.0.0.1"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("name", ["HLM_NETWORK__ENABLED", "HLM_NETWORK_ENABLED"])
def test_network_enabled_env_alias_overrides_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    path = tmp_path / "config.toml"
    update_persistent_config({"network": {"enabled": True}}, path)
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv(name, "false")

    loaded = Settings.load_from(path)

    assert loaded.network.enabled is False


@pytest.mark.parametrize(
    "name", ["HLM_NETWORK__ALLOWED_CIDRS", "HLM_NETWORK_ALLOWED_CIDRS"]
)
def test_network_allowed_cidrs_accepts_json_env_arrays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv(name, '["192.168.1.0/24", "10.0.0.0/8"]')

    loaded = Settings.load_from(tmp_path / "missing.toml")

    assert loaded.network.allowed_cidrs == ("192.168.1.0/24", "10.0.0.0/8")


def test_network_allowed_cidrs_rejects_invalid_env_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("HLM_NETWORK__ALLOWED_CIDRS", "not-json")

    with pytest.raises(ValueError, match="allowed CIDRs must be a JSON array"):
        Settings.load_from(tmp_path / "missing.toml")


def test_settings_require_an_existing_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HLM_VAULT_PATH", str(tmp_path / "missing"))

    with pytest.raises(ValueError, match="vault path does not exist"):
        Settings.load()


def test_short_term_cache_defaults() -> None:
    settings = MemorySettings()

    assert (
        settings.short_term_capacity,
        settings.short_term_ttl_days,
        settings.short_term_max_boost,
    ) == (50, 14, 0.25)


def test_short_term_cache_aliases() -> None:
    settings = MemorySettings.model_validate(
        {
            "HLM_SHORT_TERM_CAPACITY": 25,
            "HLM_SHORT_TERM_TTL_DAYS": 7,
            "HLM_SHORT_TERM_MAX_BOOST": 0.5,
        }
    )

    assert (
        settings.short_term_capacity,
        settings.short_term_ttl_days,
        settings.short_term_max_boost,
    ) == (25, 7, 0.5)


def test_short_term_cache_env_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HLM_SHORT_TERM_CAPACITY", "25")
    monkeypatch.setenv("HLM_SHORT_TERM_TTL_DAYS", "7")
    monkeypatch.setenv("HLM_SHORT_TERM_MAX_BOOST", "0.5")

    settings = MemorySettings()

    assert (
        settings.short_term_capacity,
        settings.short_term_ttl_days,
        settings.short_term_max_boost,
    ) == (25, 7, 0.5)


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"short_term_capacity": 0}, "short_term_capacity"),
        ({"short_term_ttl_days": 0}, "short_term_ttl_days"),
        ({"short_term_max_boost": 0}, "short_term_max_boost"),
        ({"short_term_max_boost": 1.1}, "short_term_max_boost"),
    ],
)
def test_short_term_cache_validation(values: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        MemorySettings.model_validate(values)


def test_embedding_model_default_is_none() -> None:
    s = MemorySettings()
    assert s.embedding_model is None


def test_embedding_model_can_be_set() -> None:
    s = MemorySettings(HLM_EMBEDDING_MODEL="all-MiniLM-L6-v2")
    assert s.embedding_model == "all-MiniLM-L6-v2"


def test_scope_defaults_to_full_vault_and_reads_allow() -> None:
    settings = Settings(vault_path=Path("."))

    assert settings.index_root.as_posix() == "."
    assert settings.folder_rules == ()


def test_folder_rules_use_most_specific_access_and_reject_conflicts() -> None:
    settings = Settings(
        vault_path=Path("."),
        index_root="Notes",
        folder_rules=(
            FolderRule(path="Notes/Private", access=FolderAccess.DENY),
            FolderRule(path="Notes/Private/Public", access=FolderAccess.READ),
        ),
    )
    assert settings.folder_rules[1].access is FolderAccess.READ

    with pytest.raises(ValidationError, match="conflicting folder rules"):
        Settings(
            vault_path=Path("."),
            folder_rules=(
                FolderRule(path="Private", access=FolderAccess.DENY),
                FolderRule(path="Private", access=FolderAccess.READ),
            ),
        )

    with pytest.raises(ValidationError, match="outside index root"):
        Settings(
            vault_path=Path("."),
            index_root=PurePosixPath("Notes"),
            folder_rules=(
                FolderRule(path=PurePosixPath("Other"), access=FolderAccess.DENY),
            ),
        )


def test_persistent_config_precedes_defaults_but_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HLM_VAULT_PATH", str(vault))
    update_persistent_config({"index_root": "Notes", "embedding_model": "toml"})

    loaded = Settings.load()
    assert loaded.index_root == PurePosixPath("Notes")
    assert loaded.memory.embedding_model == "toml"

    monkeypatch.setenv("HLM_INDEX_ROOT", "Projects")
    monkeypatch.setenv("HLM_EMBEDDING_MODEL", "env")
    loaded = Settings.load()
    assert loaded.index_root == PurePosixPath("Projects")
    assert loaded.memory.embedding_model == "env"
