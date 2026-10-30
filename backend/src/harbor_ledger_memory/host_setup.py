"""Dispatch host-specific setup adapters."""

from collections.abc import Callable

from harbor_ledger_memory.hosts import opencode


def setup_host(
    name: str,
    *,
    token: str | None = None,
    confirm_replace: Callable[[], bool] | None = None,
) -> None:
    """Configure a supported host by its explicit name."""
    if name == "opencode":
        if token is None:
            raise ValueError("OpenCode setup requires a token.")
        opencode.setup(token=token, confirm_replace=confirm_replace)
        return
    raise ValueError(f"Unsupported host '{name}'. Supported hosts: opencode.")


__all__ = ["setup_host"]
