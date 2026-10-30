"""Dispatch host-specific setup adapters."""

from collections.abc import Callable
from pathlib import Path

from harbor_ledger_memory.hosts import hermes, openclaw, opencode


def setup_host(
    name: str,
    *,
    token: str | None = None,
    confirm_replace: Callable[[], bool] | None = None,
    hermes_home: Path | None = None,
) -> str | None:
    """Configure a supported host by its explicit name."""
    if name == "opencode":
        if token is None:
            raise ValueError("OpenCode setup requires a token.")
        opencode.setup(token=token, confirm_replace=confirm_replace)
        return None
    if name == "openclaw":
        return openclaw.setup()
    if name == "hermes":
        return str(hermes.setup(hermes_home=hermes_home))
    raise ValueError(
        f"Unsupported host '{name}'. Supported hosts: opencode, openclaw, hermes."
    )


__all__ = ["setup_host"]
