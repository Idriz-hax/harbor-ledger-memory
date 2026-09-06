"""Minimal localhost HTTP adapter for vault services and policy-controlled writes."""

from harbor_ledger_memory.api.app import create_app

__all__ = ["create_app"]
