"""Dispatch host-specific setup adapters."""

from collections.abc import Callable

from harbor_ledger_memory.hosts import opencode


def setup_host(name: str, **options: object) -> None:
    """Configure a supported host by its explicit name."""
    if name == "opencode":
        opencode.setup(**options)  # type: ignore[arg-type]
        return
    raise ValueError(f"Unsupported host '{name}'. Supported hosts: opencode.")


__all__ = ["Callable", "setup_host"]
