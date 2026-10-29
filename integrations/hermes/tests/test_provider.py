import json
import sys

import pytest


def _install_host(monkeypatch):
    del monkeypatch
    return sys.modules["agent.memory_provider"].MemoryProvider


def test_provider_profile_lifecycle_and_tools(monkeypatch, tmp_path):
    home = tmp_path / "profile-a"
    (home / "plugins" / "hlm").mkdir(parents=True)
    host_base = _install_host(monkeypatch)
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))
    from hlm import HLMProvider

    monkeypatch.setattr(
        "hlm.build_profile_secret_scope",
        lambda path: {"HLM_TOKEN": f"token:{path}"},
    )

    provider = HLMProvider()
    calls = []

    class Client:
        def __getattr__(self, method):
            def invoke(*args, **kwargs):
                calls.append((method, args, kwargs))
                return {
                    "selected_memories": [
                        {"title": "context", "excerpt": "context", "path": "context.md"}
                    ]
                }

            return invoke

    monkeypatch.setattr("hlm.HLMClient", lambda *args: Client())
    assert isinstance(provider, host_base)
    assert provider.name == "hlm"
    assert provider.is_available() is True
    provider.initialize("session", hermes_home=str(home))
    assert provider._client is not None
    assert provider.prefetch("topic") == "## context\ncontext\nSource: context.md"
    names = {schema["function"]["name"] for schema in provider.get_tool_schemas()}
    assert names == {
        "hlm_query",
        "hlm_get_note",
        "hlm_get_notes_batch",
        "hlm_propose_write",
        "hlm_propose_folder",
        "hlm_pending_proposals",
    }
    pending_schema = next(
        schema["function"]["parameters"]
        for schema in provider.get_tool_schemas()
        if schema["function"]["name"] == "hlm_pending_proposals"
    )
    assert pending_schema["properties"]["before_id"] == {
        "type": "integer",
        "minimum": 1,
    }
    result = json.loads(provider.handle_tool_call("hlm_query", {"query": "q"}))
    assert result["selected_memories"][0]["title"] == "context"
    pending = json.loads(provider.handle_tool_call("hlm_pending_proposals", {}))
    assert calls[-1] == ("pending_proposals", (20, None), {})
    assert pending["review_url"] == "http://127.0.0.1:8765"
    assert (
        pending["review_instructions"]
        == "Open HLM and select Approvals in the navigation."
    )
    assert all("approve" not in name and "reject" not in name for name in names)


def test_batch_note_tool_passes_paths_and_returns_per_path_results():
    from hlm import HLMProvider

    paths = ["A.md", "missing.md"]
    results = [
        {"path": "A.md", "ok": True, "content": "full note", "revision": "r1"},
        {"path": "missing.md", "ok": False, "error": "unavailable"},
    ]

    class Client:
        def get_notes_batch(self, requested_paths):
            assert requested_paths == paths
            return results

    provider = HLMProvider()
    provider._client = Client()
    result = json.loads(
        provider.handle_tool_call("hlm_get_notes_batch", {"paths": paths})
    )
    assert result == results


def test_availability_does_not_make_network_call(monkeypatch):
    _install_host(monkeypatch)
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))
    from hlm import HLMProvider

    assert HLMProvider().is_available() is True


def test_profiles_use_profile_scoped_credentials_and_config(monkeypatch, tmp_path):
    _install_host(monkeypatch)
    from hlm import HLMProvider

    monkeypatch.setattr(
        "hlm.build_profile_secret_scope",
        lambda path: {"HLM_TOKEN": f"token:{path}"},
    )

    clients = []

    class CapturingClient:
        def __init__(self, base_url, token):
            clients.append((base_url, token))

    monkeypatch.setattr("hlm.HLMClient", CapturingClient)
    homes = [tmp_path / "one", tmp_path / "two"]
    for index, home in enumerate(homes):
        plugin = home / "plugins" / "hlm"
        plugin.mkdir(parents=True)
        (plugin / "config.json").write_text(
            json.dumps({"base_url": f"https://hlm-{index}.example"}), encoding="utf-8"
        )
        HLMProvider().initialize("session", hermes_home=str(home))

    assert clients == [
        ("https://hlm-0.example", f"token:{homes[0]}"),
        ("https://hlm-1.example", f"token:{homes[1]}"),
    ]


def test_missing_profile_token_fails_closed(monkeypatch, tmp_path):
    _install_host(monkeypatch)
    from hlm import HLMProvider

    monkeypatch.setattr("hlm.build_profile_secret_scope", lambda _home: {})
    home = tmp_path / "empty"
    (home / "plugins" / "hlm").mkdir(parents=True)
    provider = HLMProvider()
    assert provider.is_available() is True
    with pytest.raises(RuntimeError, match="HLM_TOKEN"):
        provider.initialize("session", hermes_home=str(home))


def test_prefetch_formats_only_selected_memory_context(monkeypatch):
    from hlm import HLMProvider

    provider = HLMProvider()

    class Client:
        def query(self, _query):
            return {
                "selected_memories": [
                    {
                        "title": "Decision",
                        "excerpt": "Keep it simple.",
                        "path": "Notes/decision.md",
                    }
                ],
                "trace": {"private": "trace"},
                "excluded": [{"path": "hidden.md"}],
            }

    provider._client = Client()
    context = provider.prefetch("decision")
    assert context == "## Decision\nKeep it simple.\nSource: Notes/decision.md"
    assert "trace" not in context
    assert "excluded" not in context


def test_prefetch_empty_selection_is_empty_but_failures_raise():
    from hlm import HLMProvider

    provider = HLMProvider()

    class EmptyClient:
        def query(self, _query):
            return {"selected_memories": []}

    provider._client = EmptyClient()
    assert provider.prefetch("none") == ""

    class FailedClient:
        def query(self, _query):
            raise RuntimeError("HLM unavailable")

    provider._client = FailedClient()
    with pytest.raises(RuntimeError, match="HLM unavailable"):
        provider.prefetch("none")


def test_proposal_tool_returns_pending_status_without_approval_tools():
    from hlm import HLMProvider

    provider = HLMProvider()
    seen_cursors = []

    class Client:
        def propose_write(self, path, content):
            return {
                "id": 1,
                "status": "pending",
                "path": path,
                "operation": "create",
                "requested_at": "now",
            }

        def pending_proposals(self, limit=20, before_id=None):
            assert limit == 20
            seen_cursors.append(before_id)
            return {
                "proposals": [
                    {
                        "id": 1,
                        "status": "pending",
                        "path": "New.md",
                        "operation": "create",
                        "requested_at": "now",
                    }
                ],
                "next_before_id": None,
            }

    provider._client = Client()
    schemas = provider.get_tool_schemas()
    names = {schema["function"]["name"] for schema in schemas}
    assert not any("approve" in name or "reject" in name for name in names)
    submitted = json.loads(
        provider.handle_tool_call(
            "hlm_propose_write", {"path": "New.md", "content": "body"}
        )
    )
    assert submitted["status"] == "pending"
    pending = json.loads(provider.handle_tool_call("hlm_pending_proposals", {}))
    assert pending["proposals"][0]["status"] == "pending"
    pending = json.loads(
        provider.handle_tool_call("hlm_pending_proposals", {"before_id": 42})
    )
    assert pending["proposals"][0]["status"] == "pending"
    assert seen_cursors == [None, 42]
    assert (
        pending["review_instructions"]
        == "Open HLM and select Approvals in the navigation."
    )
