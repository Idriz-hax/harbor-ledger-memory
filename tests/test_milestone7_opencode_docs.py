from pathlib import Path


def test_opencode_remote_mcp_instructions_are_exact_and_reconnect_safe() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    skill = Path(
        "backend/src/harbor_ledger_memory/skills/harbor-ledger-memory/SKILL.md"
    ).read_text(encoding="utf-8")
    assert '"type": "remote"' in readme
    assert '"url": "http://127.0.0.1:8765/mcp/"' in readme
    assert '"oauth": false' in readme
    assert '"Authorization": "Bearer {env:HLM_MCP_TOKEN}"' in readme
    assert "opencode mcp list" in readme
    assert "opencode mcp debug" in readme
    assert "hlm token revoke" in readme
    assert '"Authorization": "Bearer {env:HLM_MCP_TOKEN}"' in skill
    assert '"oauth": false' in skill
