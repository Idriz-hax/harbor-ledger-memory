"""Hermes native provider for shared Harbor Ledger Memory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider
from agent.secret_scope import (
    build_profile_secret_scope,
    get_secret,
    reset_secret_scope,
    set_secret_scope,
)

from .client import HLMClient


class HLMProvider(MemoryProvider):
    @property
    def name(self) -> str:
        return "hlm"

    def __init__(self) -> None:
        self._client: HLMClient | None = None
        self._review_url = "http://127.0.0.1:8765"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        del session_id
        home = kwargs.get("hermes_home")
        if home is None:
            raise ValueError("Hermes profile hermes_home is required")
        config_path = Path(home) / "plugins" / "hlm" / "config.json"
        config: dict[str, Any] = {}
        if config_path.is_file():
            config = json.loads(config_path.read_text(encoding="utf-8"))
        base_url = str(config.get("base_url") or "http://127.0.0.1:8765")
        self._review_url = str(config.get("review_url") or base_url)
        secret_scope = build_profile_secret_scope(Path(home))
        scope_token = set_secret_scope(secret_scope)
        try:
            token = get_secret("HLM_TOKEN")
        finally:
            reset_secret_scope(scope_token)
        if not token:
            raise RuntimeError("Hermes profile secret HLM_TOKEN is not configured")
        self._client = HLMClient(base_url, token)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        def tool(
            name: str, description: str, properties: dict[str, Any], required: list[str]
        ) -> dict[str, Any]:
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            }

        return [
            tool(
                "hlm_query",
                "Search approved shared memories in HLM.",
                {"query": {"type": "string"}},
                ["query"],
            ),
            tool(
                "hlm_get_note",
                "Read a full Markdown note from HLM.",
                {"path": {"type": "string"}},
                ["path"],
            ),
            tool(
                "hlm_propose_write",
                "Propose a Markdown note through HLM review.",
                {"path": {"type": "string"}, "content": {"type": "string"}},
                ["path", "content"],
            ),
            tool(
                "hlm_propose_folder",
                "Propose a folder through HLM review.",
                {"path": {"type": "string"}},
                ["path"],
            ),
            tool(
                "hlm_pending_proposals",
                "List a page of HLM proposals awaiting review.",
                {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "before_id": {"type": "integer", "minimum": 1},
                },
                [],
            ),
        ]

    def handle_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if self._client is None:
            raise RuntimeError("HLM provider is not initialized")
        handlers = {
            "hlm_query": lambda: self._client.query(arguments["query"]),
            "hlm_get_note": lambda: self._client.get_note(arguments["path"]),
            "hlm_propose_write": lambda: self._client.propose_write(
                arguments["path"], arguments["content"]
            ),
            "hlm_propose_folder": lambda: self._client.propose_folder(
                arguments["path"]
            ),
            "hlm_pending_proposals": lambda: self._client.pending_proposals(
                arguments.get("limit", 20), arguments.get("before_id")
            ),
        }
        if tool_name not in handlers:
            raise ValueError(f"Unknown HLM tool: {tool_name}")
        result = handlers[tool_name]()
        if tool_name == "hlm_pending_proposals" and isinstance(result, dict):
            result = {
                **result,
                "review_url": self._review_url,
                "review_instructions": (
                    "Open HLM and select Approvals in the navigation."
                ),
            }
        return json.dumps(result, ensure_ascii=False)

    def prefetch(self, query: str, **_kwargs: Any) -> str:
        if self._client is None:
            raise RuntimeError("HLM provider is not initialized")
        result = self._client.query(query)
        memories = result["selected_memories"]
        return "\n\n".join(
            f"## {memory['title']}\n{memory['excerpt']}\nSource: {memory['path']}"
            for memory in memories
        )


__all__ = ["HLMProvider"]
