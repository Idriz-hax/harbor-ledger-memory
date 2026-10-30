"""Safe OpenCode V2 remote MCP configuration."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

SERVER = {
    "type": "remote",
    "url": "http://127.0.0.1:8765/mcp/",
    "oauth": False,
    "headers": {"Authorization": "Bearer {file:%s}"},
}


class OpenCodeSetupError(Exception):
    """Actionable error that leaves host configuration untouched."""


class OpenCodeSetupCancelled(OpenCodeSetupError):
    """User declined replacing an existing OpenCode MCP entry."""


def _paths() -> tuple[Path, Path]:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "opencode", root / "harbor-ledger-memory"


def _atomic_write(path: Path, content: str | bytes, mode: int) -> None:
    path.parent.mkdir(
        mode=0o700 if path.parent.name == "harbor-ledger-memory" else 0o755,
        parents=True,
        exist_ok=True,
    )
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if isinstance(content, bytes):
            stream = os.fdopen(fd, "wb")
        else:
            stream = os.fdopen(fd, "w", encoding="utf-8")
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def setup(
    *, token: str, confirm_replace: Callable[[], bool] | None = None
) -> Path:
    """Store a bearer token privately and merge the OpenCode V2 JSON config."""
    config_dir, secret_dir = _paths()
    json_path, jsonc_path = config_dir / "opencode.json", config_dir / "opencode.jsonc"
    config_path = json_path
    if jsonc_path.exists():
        raise OpenCodeSetupError(
            f"OpenCode JSONC config detected at {jsonc_path}; manually merge the "
            "remote MCP server into mcp.servers."
        )
    if not token or "\n" in token:
        raise OpenCodeSetupError("Token must be non-empty and contain no newline.")
    secret_path = secret_dir / "opencode-token"
    if config_path.exists():
        try:
            config = json.loads(
                config_path.read_text(encoding="utf-8"), object_pairs_hook=_object
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise OpenCodeSetupError(
                f"Cannot safely edit {config_path} (malformed JSON or duplicate keys); "
                "manually merge mcp.servers."
            ) from exc
        if not isinstance(config, dict):
            raise OpenCodeSetupError(
                f"{config_path} must contain a JSON object; manually merge mcp.servers."
            )
    else:
        config = {"$schema": "https://opencode.ai/config.json"}
    mcp = config.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise OpenCodeSetupError(
            f"{config_path}: mcp must be an object; manually merge mcp.servers."
        )
    servers = mcp.setdefault("servers", {})
    if not isinstance(servers, dict):
        raise OpenCodeSetupError(
            f"{config_path}: mcp.servers must be an object; manually merge."
        )
    entry = dict(SERVER)
    entry["headers"] = {"Authorization": f"Bearer {{file:{secret_path}}}"}
    current = servers.get("harbor-ledger-memory")
    if current is not None and current != entry:
        if confirm_replace is None or not confirm_replace():
            raise OpenCodeSetupCancelled(
                "Existing harbor-ledger-memory server differs; confirm replacement "
                "or manually merge."
            )
    servers["harbor-ledger-memory"] = entry
    updated_config = json.dumps(config, indent=2) + "\n"

    prior_secret = secret_path.read_bytes() if secret_path.exists() else None
    prior_mode = (
        secret_path.stat().st_mode & 0o777 if secret_path.exists() else None
    )
    secret_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(secret_path.parent, 0o700)
    try:
        _atomic_write(secret_path, token, 0o600)
        _atomic_write(config_path, updated_config, 0o600)
    except Exception:
        if prior_secret is None:
            secret_path.unlink(missing_ok=True)
        else:
            _atomic_write(secret_path, prior_secret, prior_mode or 0o600)
        raise
    return config_path
