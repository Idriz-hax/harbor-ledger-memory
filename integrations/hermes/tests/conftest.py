import importlib
import sys
import types
from contextvars import ContextVar
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

try:
    importlib.import_module("agent.memory_provider")
    importlib.import_module("agent.secret_scope")
except ImportError:
    agent = types.ModuleType("agent")
    memory_provider = types.ModuleType("agent.memory_provider")
    secret_scope = types.ModuleType("agent.secret_scope")
    _scope = ContextVar("test_secret_scope", default={})

    class MemoryProvider:
        pass

    memory_provider.MemoryProvider = MemoryProvider
    secret_scope.build_profile_secret_scope = lambda home: {
        "home": str(home),
        "HLM_TOKEN": f"token:{home}",
    }
    secret_scope.set_secret_scope = lambda mapping: _scope.set(mapping)
    secret_scope.reset_secret_scope = lambda token: _scope.reset(token)
    secret_scope.get_secret = lambda name, default=None: _scope.get().get(name, default)
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", memory_provider)
    sys.modules.setdefault("agent.secret_scope", secret_scope)
