"""Application configuration, scope policy, and persistent settings."""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
import tomllib
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from harbor_ledger_memory.domain.retrieval import QuerySettings


class FolderAccess(StrEnum):
    """Permission modes for a folder rule."""

    NONE = "none"
    # ``deny`` is the retired legacy level: an alias of ``none`` kept so
    # persisted settings, TOML files, and legacy token rows still validate.
    # New token rules must use ``none`` (TokenService rejects ``deny``).
    DENY = "deny"
    READ = "read"
    PROPOSE_WRITE = "propose-write"
    AUTO_WRITE = "auto-write"

    @property
    def is_readable(self) -> bool:
        """Whether this mode permits reads."""
        return self in (
            FolderAccess.READ,
            FolderAccess.PROPOSE_WRITE,
            FolderAccess.AUTO_WRITE,
        )

    @property
    def is_writable(self) -> bool:
        """Whether this mode permits a policy-controlled vault write."""
        return self in (FolderAccess.PROPOSE_WRITE, FolderAccess.AUTO_WRITE)

    @property
    def is_auto_writable(self) -> bool:
        """Whether this mode permits an automatic policy-controlled vault write."""
        return self is FolderAccess.AUTO_WRITE


class FolderRule(BaseModel):
    """A single vault-relative folder access rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: PurePosixPath
    access: FolderAccess

    @field_validator("path", mode="before")
    @classmethod
    def validate_path_value(cls, value: object) -> PurePosixPath:
        if not isinstance(value, (str, PurePosixPath)):
            raise ValueError("folder path must be a POSIX path")
        path = PurePosixPath(value)
        if not str(value).strip():
            raise ValueError("folder path must not be empty")
        if path.is_absolute():
            raise ValueError("folder path must be vault-relative")
        if ".." in path.parts:
            raise ValueError("folder path must not contain '..'")
        if "\x00" in path.as_posix():
            raise ValueError("folder path must not contain null bytes")
        if "\\" in path.as_posix():
            raise ValueError("folder path must use POSIX separators")
        return path


class MemorySettings(BaseSettings):
    """Configuration for the memory persistence layer."""

    model_config = SettingsConfigDict(populate_by_name=True)

    embedding_model: str | None = Field(
        default=None, validation_alias="HLM_EMBEDDING_MODEL"
    )
    short_term_window_hours: int = 24
    short_term_capacity: int = Field(
        default=50, validation_alias="HLM_SHORT_TERM_CAPACITY"
    )
    short_term_ttl_days: int = Field(
        default=14, validation_alias="HLM_SHORT_TERM_TTL_DAYS"
    )
    short_term_max_boost: float = Field(
        default=0.25, validation_alias="HLM_SHORT_TERM_MAX_BOOST"
    )
    adaptive_boost: float = 0.20
    implicit_step: float = 0.05
    explicit_positive: float = 0.15
    explicit_negative: float = 0.10

    @field_validator("short_term_window_hours")
    @classmethod
    def _validate_window(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("short_term_window_hours must be > 0")
        return v

    @field_validator("short_term_capacity", "short_term_ttl_days")
    @classmethod
    def _validate_positive_int(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("value must be > 0")
        return v

    @field_validator("short_term_max_boost")
    @classmethod
    def _validate_short_term_max_boost(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("short_term_max_boost must be in (0, 1]")
        return v

    @field_validator("adaptive_boost")
    @classmethod
    def _validate_boost(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("adaptive_boost must be in (0, 1]")
        return v

    @field_validator("implicit_step")
    @classmethod
    def _validate_implicit(cls, v: float) -> float:
        if not 0 < v <= 0.5:
            raise ValueError("implicit_step must be in (0, 0.5]")
        return v

    @field_validator("explicit_positive")
    @classmethod
    def _validate_explicit_pos(cls, v: float) -> float:
        if not 0 < v <= 0.5:
            raise ValueError("explicit_positive must be in (0, 0.5]")
        return v

    @field_validator("explicit_negative")
    @classmethod
    def _validate_explicit_neg(cls, v: float) -> float:
        if not 0 < v <= 0.5:
            raise ValueError("explicit_negative must be in (0, 0.5]")
        return v


class FrontendSettings(BaseModel):
    """Configuration for the browser frontend (not runtime behavior)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    mode: Literal["loopback", "lan"] = "loopback"
    public_origin: str | None = None
    password_verifier: str | None = None


class ApiSettings(BaseModel):
    """Configuration for the REST bearer API."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class McpSettings(BaseModel):
    """Configuration for the MCP transport."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class NetworkSettings(BaseModel):
    """Network exposure settings reserved for server runtime validation."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    external: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_cidrs: tuple[str, ...] = ()
    tls_cert: str | None = None
    tls_key: str | None = None
    insecure_http: bool = False

    @field_validator("allowed_cidrs")
    @classmethod
    def validate_allowed_cidrs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for cidr in value:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid CIDR allowlist entry: {cidr!r}") from exc
        return value


class Settings(BaseSettings):
    """Effective application settings.

    ``Settings()`` reads environment values in the usual pydantic-settings
    fashion. ``Settings.load()`` additionally merges the persistent TOML
    file, with environment values taking precedence over that file.
    """

    model_config = SettingsConfigDict(populate_by_name=True, extra="forbid")

    vault_path: Path = Field(validation_alias="HLM_VAULT_PATH")
    index_root: PurePosixPath = Field(
        default=PurePosixPath("."), validation_alias="HLM_INDEX_ROOT"
    )
    database_url: str = "sqlite:///data/memory.db"
    server_host: str = "127.0.0.1"
    server_port: int = 8765

    folder_rules: tuple[FolderRule, ...] = Field(
        default=(), validation_alias="HLM_FOLDER_RULES"
    )

    retrieval: QuerySettings = Field(default_factory=QuerySettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    frontend: FrontendSettings = Field(default_factory=FrontendSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)

    @field_validator("index_root", mode="before")
    @classmethod
    def validate_index_root(cls, value: object) -> PurePosixPath:
        if not isinstance(value, (str, PurePosixPath)):
            raise ValueError("index root must be a POSIX path")
        root = PurePosixPath(value)
        if root.is_absolute() or ".." in root.parts:
            raise ValueError("index root must be vault-relative")
        if "\x00" in root.as_posix():
            raise ValueError("index root must not contain null bytes")
        if "\\" in root.as_posix():
            raise ValueError("index root must use POSIX separators")
        return root

    @field_validator("folder_rules", mode="before")
    @classmethod
    def validate_folder_rules(cls, value: object) -> tuple[FolderRule, ...]:
        """Accept both pydantic settings JSON and direct constructor values."""
        return _coerce_folder_rules(value)

    @model_validator(mode="after")
    def validate_scope(self) -> Settings:
        validate_scope_values(self.index_root, self.folder_rules)
        return self

    @property
    def ai_root(self) -> PurePosixPath:
        """Compatibility alias for integrations written before scope config."""
        return self.index_root

    @property
    def effective_read_scope(self) -> str:
        """Describe the configured read policy for status surfaces."""
        root = self.index_root.as_posix()
        denied = sorted(
            rule.path.as_posix()
            for rule in self.folder_rules
            if not rule.access.is_readable
        )
        if not denied:
            return root
        return f"{root} (deny: {', '.join(denied)})"

    @classmethod
    def load(cls) -> Settings:
        """Load effective settings using env > TOML > defaults precedence."""
        return cls.load_from(config_path())

    @classmethod
    def load_from(cls, path: Path) -> Settings:
        """Load settings from an explicit TOML path, then environment."""
        values = _persistent_values(path)
        environment = _environment_values()
        for section in ("memory", "frontend", "api", "mcp", "network"):
            environment_section = environment.pop(section, None)
            if isinstance(environment_section, dict):
                persistent_section = values.get(section, {})
                merged_section = dict(cast(dict[str, Any], persistent_section))
                merged_section.update(cast(dict[str, Any], environment_section))
                values[section] = merged_section
        values.update(environment)
        settings = cls.model_validate(values)
        if not settings.vault_path.expanduser().is_dir():
            raise ValueError("vault path does not exist or is not a directory")
        return settings


def validate_scope_values(
    index_root: PurePosixPath | str,
    folder_rules: tuple[FolderRule, ...] | list[FolderRule],
) -> None:
    """Validate that rules are canonical, in scope, and unambiguous."""
    root = PurePosixPath(index_root)
    rules = tuple(folder_rules)
    seen: dict[PurePosixPath, FolderAccess] = {}
    for rule in rules:
        try:
            rule.path.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"folder rule {rule.path.as_posix()!r} is outside index root "
                f"{root.as_posix()!r}"
            ) from exc
        previous = seen.get(rule.path)
        if previous is not None and previous is not rule.access:
            raise ValueError(f"conflicting folder rules at {rule.path.as_posix()!r}")
        seen[rule.path] = rule.access


def config_path() -> Path:
    """Return the application-owned persistent configuration path."""
    return Path.home() / ".config" / "harbor-ledger-memory" / "config.toml"


def read_persistent_config(path: Path | None = None) -> dict[str, Any]:
    """Read the persistent TOML document, returning an empty document if absent."""
    target = path or config_path()
    try:
        with target.open("rb") as stream:
            loaded = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    return loaded


def update_persistent_config(updates: dict[str, Any], path: Path | None = None) -> Path:
    """Merge validated application settings into the TOML file atomically."""
    target = path or config_path()
    current = read_persistent_config(target)
    merged = dict(current)
    for key, value in updates.items():
        if value is not None:
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**cast(dict[str, Any], merged[key]), **value}
            else:
                merged[key] = value

    index_root = PurePosixPath(merged.get("index_root", "."))
    rules = _coerce_folder_rules(merged.get("folder_rules", ()))
    validate_scope_values(index_root, rules)
    if "vault_path" in merged:
        merged["vault_path"] = str(Path(str(merged["vault_path"])).expanduser())
    merged["index_root"] = index_root.as_posix()
    merged["folder_rules"] = [
        {"path": rule.path.as_posix(), "access": rule.access.value} for rule in rules
    ]
    if "embedding_model" in merged and merged["embedding_model"] == "":
        merged.pop("embedding_model")

    target.parent.mkdir(parents=True, exist_ok=True)
    document = _toml_document(merged)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="config.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
        target.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return target


def config_payload(settings: Settings) -> dict[str, Any]:
    """Return the stable JSON-friendly settings representation."""
    return {
        "vault_path": str(settings.vault_path.expanduser()),
        "index_root": settings.index_root.as_posix(),
        "folder_rules": [
            {"path": rule.path.as_posix(), "access": rule.access.value}
            for rule in settings.folder_rules
        ],
        "embedding_model": settings.memory.embedding_model,
        "effective_read_scope": settings.effective_read_scope,
        "config_path": str(config_path()),
    }


def _persistent_values(path: Path | None = None) -> dict[str, Any]:
    raw = read_persistent_config(path)
    values: dict[str, Any] = {}
    for key in (
        "vault_path",
        "index_root",
        "folder_rules",
        "database_url",
        "server_host",
        "server_port",
    ):
        if key in raw:
            values[key] = raw[key]
    if "folder_rules" in values:
        values["folder_rules"] = _coerce_folder_rules(values["folder_rules"])
    for section in ("memory", "frontend", "api", "mcp", "network"):
        section_values = raw.get(section)
        if isinstance(section_values, dict):
            values[section] = dict(cast(dict[str, Any], section_values))
    if "embedding_model" in raw:
        memory_values = values.get("memory")
        if not isinstance(memory_values, dict):
            memory_values = {}
            values["memory"] = memory_values
        memory_values["embedding_model"] = raw["embedding_model"]
    return values


def _environment_values() -> dict[str, Any]:
    values: dict[str, Any] = {}
    aliases = {
        "HLM_VAULT_PATH": "vault_path",
        "HLM_INDEX_ROOT": "index_root",
        "DATABASE_URL": "database_url",
        "SERVER_HOST": "server_host",
        "SERVER_PORT": "server_port",
    }
    for environment_name, field_name in aliases.items():
        if environment_name in os.environ:
            values[field_name] = os.environ[environment_name]
    if "HLM_FOLDER_RULES" in os.environ:
        try:
            values["folder_rules"] = _coerce_folder_rules(
                json.loads(os.environ["HLM_FOLDER_RULES"])
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("HLM_FOLDER_RULES must be a JSON array") from exc

    memory: dict[str, Any] = {}
    memory_aliases = {
        "HLM_EMBEDDING_MODEL": "embedding_model",
        "HLM_SHORT_TERM_CAPACITY": "short_term_capacity",
        "HLM_SHORT_TERM_TTL_DAYS": "short_term_ttl_days",
        "HLM_SHORT_TERM_MAX_BOOST": "short_term_max_boost",
    }
    for environment_name, field_name in memory_aliases.items():
        if environment_name in os.environ:
            memory[field_name] = os.environ[environment_name]
    if memory:
        values["memory"] = memory
    for section, fields in {
        "frontend": ("enabled", "mode", "public_origin", "password_verifier"),
        "api": ("enabled",),
        "mcp": ("enabled",),
        "network": (
            "enabled", "external", "host", "port", "allowed_cidrs", "tls_cert",
            "tls_key", "insecure_http",
        ),
    }.items():
        section_values: dict[str, Any] = {}
        for field_name in fields:
            for prefix in (f"HLM_{section.upper()}__", f"HLM_{section.upper()}_"):
                environment_name = prefix + field_name.upper()
                if environment_name in os.environ:
                    raw_value = os.environ[environment_name]
                    if section == "network" and field_name == "allowed_cidrs":
                        section_values[field_name] = _coerce_json_array(
                            raw_value, "allowed CIDRs"
                        )
                    else:
                        section_values[field_name] = raw_value
                    break
        if section_values:
            values[section] = section_values
    return values


def _coerce_folder_rules(value: object) -> tuple[FolderRule, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("folder rules must be a JSON array") from exc
    if not isinstance(value, (list, tuple)):
        raise ValueError("folder rules must be a JSON array")
    items = cast(list[Any] | tuple[Any, ...], value)
    return tuple(
        FolderRule.model_validate(_alias_legacy_access(item)) for item in items
    )


def _coerce_json_array(value: str, label: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON array") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"{label} must be a JSON array")
    return cast(list[Any], parsed)


def _alias_legacy_access(item: object) -> object:
    """Map the retired ``deny`` level to ``none`` in dict rule input.

    Directly constructed ``FolderRule`` instances pass through unchanged so
    legacy persisted rules and settings keep validating; dict sources (TOML
    files, ``HLM_FOLDER_RULES`` JSON, settings updates) are normalized to the
    canonical ``none`` level on load.
    """
    if not isinstance(item, dict):
        return item
    rule = cast("dict[str, object]", item)
    if rule.get("access") == FolderAccess.DENY:
        return {**rule, "access": FolderAccess.NONE.value}
    return rule


def _toml_document(values: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in (
        "vault_path",
        "index_root",
        "embedding_model",
        "database_url",
        "server_host",
        "server_port",
    ):
        if key in values and values[key] is not None:
            lines.append(f"{key} = {_toml_string(str(values[key]))}")
    rules = values.get("folder_rules")
    if rules is not None:
        typed_rules = cast(list[dict[str, str]], rules)
        lines.append("folder_rules = [")
        for rule in typed_rules:
            lines.append(
                "  { path = "
                f"{_toml_string(str(rule['path']))}, access = "
                f"{_toml_string(str(rule['access']))} }},"
            )
        lines.append("]")
    for section in ("memory", "frontend", "api", "mcp", "network"):
        section_values = values.get(section)
        if isinstance(section_values, dict):
            lines.append("")
            lines.append(f"[{section}]")
            typed_section = cast(dict[str, Any], section_values)
            for key, value in typed_section.items():
                if value is None:
                    continue
                if isinstance(value, (list, tuple)):
                    items = cast(list[Any] | tuple[Any, ...], value)
                    serialized = json.dumps(list(items), ensure_ascii=False)
                    lines.append(f"{key} = {serialized}")
                elif isinstance(value, bool):
                    lines.append(f"{key} = {str(value).lower()}")
                elif isinstance(value, int):
                    lines.append(f"{key} = {value}")
                else:
                    lines.append(f"{key} = {_toml_string(str(value))}")
    return "\n".join(lines) + "\n"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


__all__ = [
    "FolderAccess",
    "FolderRule",
    "FrontendSettings",
    "ApiSettings",
    "McpSettings",
    "MemorySettings",
    "NetworkSettings",
    "Settings",
    "config_path",
    "config_payload",
    "read_persistent_config",
    "update_persistent_config",
    "validate_scope_values",
]
