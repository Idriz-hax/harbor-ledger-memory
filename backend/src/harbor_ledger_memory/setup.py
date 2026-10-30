"""Interactive, local-only first-run configuration."""

from __future__ import annotations

import importlib.util
from pathlib import Path, PurePosixPath
from typing import Callable

import typer
from pydantic import ValidationError

from harbor_ledger_memory.config import (
    FolderAccess, FolderRule, Settings, config_path, read_persistent_config,
    update_persistent_config,
)
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.services.tokens import TokenService


def setup() -> None:
    """Ask all setup questions before persisting configuration or a token."""
    raw = read_persistent_config()
    try:
        settings = Settings.load_from(config_path()) if raw else Settings()
    except (ValidationError, ValueError) as exc:
        raise typer.BadParameter(f"Existing vault configuration is invalid: {exc}") from exc
    # Validate the actual vault before changing settings or creating credentials.
    ScanService.from_settings(settings).full_scan()

    ui = typer.confirm("Enable Web UI?", default=False)
    mcp = typer.confirm("Enable MCP?", default=False)
    api = typer.confirm("Enable API?", default=False)
    local = typer.confirm("Keep access localhost only?", default=True)
    if not local:
        raise typer.BadParameter(
            "Setup stopped without changes. For nonlocal access, manually configure "
            "network exposure, allowed CIDRs, TLS, and frontend authentication with `hlm config set`."
        )
    embeddings = typer.confirm("Enable optional embeddings?", default=False)
    model = None
    if embeddings:
        if importlib.util.find_spec("sentence_transformers") is None:
            raise typer.BadParameter(
                "sentence_transformers is not installed; install the embeddings extra and rerun setup."
            )
        model = typer.prompt("Embedding model", default="all-MiniLM-L6-v2")
    make_token = typer.confirm("Create a read-only API token?", default=False)
    token_name = folders_text = ""
    folders: list[PurePosixPath] = []
    if make_token:
        token_name = typer.prompt("Token name")
        folders_text = typer.prompt("Vault-relative folders (comma-separated; empty skips token)")
        folders = [PurePosixPath(part.strip()) for part in folders_text.split(",") if part.strip()]
        root = settings.index_root
        for folder in folders:
            try:
                if folder.is_absolute() or ".." in folder.parts:
                    raise ValueError
                folder.relative_to(root)
            except ValueError as exc:
                raise typer.BadParameter(f"Invalid folder '{folder}': must be within index root '{root}'") from exc

    updates = {"frontend": {"enabled": ui, "mode": "loopback"},
               "mcp": {"enabled": mcp}, "api": {"enabled": api},
               "network": {"enabled": False, "external": False, "host": "127.0.0.1"}}
    if model:
        updates["memory"] = {"embedding_model": model}
    update_persistent_config(updates)
    if make_token and folders:
        service = TokenService(settings.database_url)
        try:
            created = service.create(token_name, [FolderRule(path=PurePosixPath("."), access=FolderAccess.NONE), *(FolderRule(path=folder, access=FolderAccess.READ) for folder in folders)])
        except Exception as exc:
            raise typer.BadParameter("Configuration saved but token creation failed. Rerun `hlm setup` to create the token.") from exc
        finally:
            service.close()
        typer.echo(created.plaintext)
        typer.echo("Save this token now; it is shown once. Update: `hlm token update NAME --rule .=none --rule FOLDER=read`; revoke: `hlm token revoke NAME`.")
    if ui:
        typer.echo("Start the Web UI with `hlm serve` (localhost only).")
    typer.echo(f"Setup saved {config_path()}")
