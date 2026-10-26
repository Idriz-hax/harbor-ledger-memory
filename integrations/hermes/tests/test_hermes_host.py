import importlib
import json
from pathlib import Path

import pytest

try:
    importlib.import_module("agent.memory_manager")
    HAS_HERMES = True
except ImportError:
    HAS_HERMES = False


@pytest.mark.skipif(
    not HAS_HERMES,
    reason="requires Hermes v0.18.1 host API",
)
def test_real_memory_manager_loads_and_runs_hlm_provider(monkeypatch, tmp_path):
    memory_manager_module = importlib.import_module("agent.memory_manager")
    loader = importlib.import_module("plugins.memory")
    assert memory_manager_module.MemoryManager is not None

    home = tmp_path / "hermes-profile"
    plugin_dir = home / "plugins" / "hlm"
    plugin_dir.mkdir(parents=True)
    source = Path(__file__).parents[1] / "hlm"
    (plugin_dir / "__init__.py").symlink_to(source / "__init__.py")
    (plugin_dir / "client.py").symlink_to(source / "client.py")
    monkeypatch.setattr(loader, "_get_user_plugins_dir", lambda: home / "plugins")
    provider = loader.load_memory_provider("hlm")
    monkeypatch.setattr(
        provider_module(provider),
        "build_profile_secret_scope",
        lambda path: {"HLM_TOKEN": f"host-token:{path}"},
    )

    class FakeClient:
        def query(self, query):
            return {
                "selected_memories": [
                    {"title": "Host test", "excerpt": query, "path": "host.md"}
                ]
            }

        def propose_write(self, path, content):
            return {"id": "host-proposal", "status": "pending", "path": path}

        def pending_proposals(self):
            return {"proposals": [{"id": "host-proposal", "status": "pending"}]}

    monkeypatch.setattr(
        provider_module(provider), "HLMClient", lambda *_args: FakeClient()
    )
    manager = memory_manager_module.MemoryManager()
    manager.add_provider(provider)
    manager.initialize_all("host-session", hermes_home=str(home))
    assert manager.prefetch_all("verified context", session_id="host-session")
    result = manager.handle_tool_call(
        "hlm_propose_write",
        {"path": "Host.md", "content": "proposal"},
    )
    assert json.loads(result)["status"] == "pending"
    manager.shutdown_all()


def provider_module(provider):
    return importlib.import_module(provider.__class__.__module__)
