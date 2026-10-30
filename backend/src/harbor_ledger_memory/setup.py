"""Interactive, local-only first-run configuration."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import typer
from pydantic import ValidationError

from harbor_ledger_memory.config import (
    FolderAccess,
    FolderRule,
    Settings,
    config_path,
    read_persistent_config,
    update_persistent_config,
)
from harbor_ledger_memory.services.tokens import TokenService


class SetupError(Exception):
    """Actionable setup error safe to display without a traceback."""


def setup(
    *,
    confirm: Callable[..., bool] = typer.confirm,
    prompt: Callable[..., str] = typer.prompt,
    output: Callable[[str], None] = typer.echo,
) -> None:
    """Collect and validate all choices before writing configuration."""
    existing = read_persistent_config()
    default_vault = str(existing.get("vault_path", ""))
    vault_text = prompt(
        "Vault directory", default=default_vault, show_default=bool(default_vault)
    )
    vault = Path(vault_text).expanduser()
    if not vault.is_dir():
        raise SetupError(
            f"Invalid vault directory: '{vault}' is not an existing directory."
        )

    ui = confirm("Enable Web UI?", default=False)
    mcp = confirm("Enable MCP?", default=False)
    host = "skip"
    if mcp:
        host = prompt("MCP host (opencode/openclaw/hermes/skip)", default="skip")
        host = host.strip().lower()
        if host not in {"opencode", "openclaw", "hermes", "skip"}:
            raise SetupError("Choose opencode, openclaw, hermes, or skip.")
    api = confirm("Enable API?", default=False)
    if not confirm("Keep access localhost only?", default=True):
        raise SetupError(
            "Setup stopped without changes. Configure nonlocal access manually "
            "with `hlm config set` (network, CIDRs, TLS, and frontend authentication)."
        )

    embeddings = confirm("Enable optional embeddings?", default=False)
    model = None
    if embeddings:
        if importlib.util.find_spec("sentence_transformers") is None:
            raise SetupError(
                "sentence_transformers is not installed. Install the embeddings "
                "extra and rerun `hlm setup`."
            )
        model = prompt("Embedding model", default="all-MiniLM-L6-v2")

    create_token = host == "skip" and confirm(
        "Create an optional read-only token?", default=False
    )
    name = ""
    folders: list[FolderRule] = []
    index_root = PurePosixPath(str(existing.get("index_root", ".")))
    if create_token:
        name = prompt("Token name").strip()
        folder_text = prompt(
            "Vault-relative folders (comma-separated; empty skips token)"
        )
        for item in (part.strip() for part in folder_text.split(",")):
            if not item:
                continue
            try:
                rule = FolderRule(path=item, access=FolderAccess.READ)
                rule.path.relative_to(index_root)
            except (ValidationError, ValueError) as exc:
                raise SetupError(
                    f"Invalid folder '{item}': must be a valid vault-relative "
                    f"folder within index root '{index_root}'."
                ) from exc
            folders.append(rule)
        if folders and not name:
            raise SetupError("Token name must not be empty.")

    if host != "skip":
        name = prompt("Host token name").strip()
        folder_text = prompt("Vault-relative folders for host (comma-separated)")
        folders = []
        for item in (part.strip() for part in folder_text.split(",")):
            if not item:
                continue
            try:
                rule = FolderRule(path=item, access=FolderAccess.READ)
                rule.path.relative_to(index_root)
            except (ValidationError, ValueError) as exc:
                raise SetupError(
                    f"Invalid folder '{item}': must be a valid vault-relative "
                    f"folder within index root '{index_root}'."
                ) from exc
            folders.append(rule)
        if not name or not folders:
            raise SetupError(
                "Host token requires a name and at least one scoped folder."
            )

    updates: dict[str, object] = {
        "vault_path": str(vault),
        "frontend": {"enabled": ui, "mode": "loopback"},
        "mcp": {"enabled": mcp},
        "api": {"enabled": api},
        "network": {"enabled": False, "external": False, "host": "127.0.0.1"},
    }
    if model is not None:
        updates["memory"] = {"embedding_model": model}
    update_persistent_config(
        updates
    )  # all prompts/validation finish before the first write

    created = None
    if folders:
        service = None
        cleanup_failed = False
        try:
            settings = Settings.load_from(config_path())
            service = TokenService(settings.database_url)
            created = service.create(
                name,
                [FolderRule(path=".", access=FolderAccess.NONE), *folders],
                admin=False,
            )
        except Exception as exc:
            raise SetupError(
                "Configuration saved, but token creation failed. Rerun "
                "`hlm setup` to create the token."
            ) from exc
        finally:
            if service is not None:
                try:
                    service.close()
                except Exception:
                    cleanup_failed = True
        if host != "opencode":
            output(created.plaintext)
            output(
                f"Save this token now; it is shown once. Update folder access with "
                f"`hlm token update {name} --rule .=none --rule FOLDER=read`; "
                "revoke it "
                f"with `hlm token revoke {name}`."
            )
        if cleanup_failed:
            output("Warning: token was created, but cleanup failed.")
    elif create_token:
        output(
            "No folders selected; token creation skipped. Create a folder-scoped "
            "token later with `hlm token create --name NAME --rule .=none "
            "--rule FOLDER=read`."
        )
    if host != "skip" and created is not None:
        from harbor_ledger_memory.host_setup import setup_host

        try:
            instructions = setup_host(
                host,
                token=created.plaintext,
                confirm_replace=lambda: confirm(
                    "Replace existing OpenCode MCP config?", default=False
                ),
            )
        except Exception as exc:
            recovery = (
                f"Host setup failed. Token '{name}' was created; save the one-time "
                f"token shown above and configure {host} manually, or revoke it "
                f"with `hlm token revoke {name}`."
                if host != "opencode"
                else f"OpenCode setup failed. Token '{name}' was created; revoke it "
                f"with `hlm token revoke {name}` and rerun setup."
            )
            raise SetupError(recovery) from exc
        if instructions:
            output(instructions)
        if host in {"openclaw", "hermes"}:
            output(
                f"Use the one-time token shown above as the {host} secret; "
                "do not put it in config files."
            )
    if ui:
        output(
            "Web UI enabled for localhost only. Start it with `hlm serve`. "
            "Manage token folder permissions in Settings > Tokens."
        )
    output(f"Setup saved {config_path()}")
