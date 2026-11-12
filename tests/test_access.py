"""AccessPolicy prefix-matching coverage."""

from __future__ import annotations

from pathlib import PurePosixPath

from sqlalchemy import column, select, union_all
from sqlalchemy.dialects import sqlite

from harbor_ledger_memory.config import FolderAccess, FolderRule
from harbor_ledger_memory.services.access import AccessPolicy


def _policy(*rules: FolderRule, admin: bool = False) -> AccessPolicy:
    return AccessPolicy(rules, admin)


def test_default_policy_is_read_only() -> None:
    policy = _policy()

    assert policy.is_admin is False
    assert policy.access_for("AI/note.md") is FolderAccess.READ
    assert policy.can_read("AI/note.md")
    assert not policy.can_propose("AI/note.md")
    assert not policy.can_auto_write("AI/note.md")
    assert not policy.has_any_write()


def test_root_rule_applies_to_every_path() -> None:
    policy = _policy(
        FolderRule(path=PurePosixPath("."), access=FolderAccess.PROPOSE_WRITE)
    )

    assert policy.access_for("AI") is FolderAccess.PROPOSE_WRITE
    assert policy.access_for("AI/deep/note.md") is FolderAccess.PROPOSE_WRITE
    assert policy.can_propose("AI/deep/note.md")
    assert not policy.can_auto_write("AI/deep/note.md")
    assert policy.has_any_write()


def test_deepest_matching_rule_wins() -> None:
    policy = _policy(
        FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
        FolderRule(path=PurePosixPath("AI/sub"), access=FolderAccess.NONE),
    )

    assert policy.access_for("AI/note.md") is FolderAccess.PROPOSE_WRITE
    assert policy.access_for("AI/sub/note.md") is FolderAccess.NONE
    assert policy.can_propose("AI/note.md")
    assert not policy.can_read("AI/sub/note.md")


def test_child_rule_overrides_parent() -> None:
    policy = _policy(
        FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
        FolderRule(path=PurePosixPath("AI/auto"), access=FolderAccess.AUTO_WRITE),
    )

    assert policy.access_for("AI/auto/note.md") is FolderAccess.AUTO_WRITE
    assert policy.can_propose("AI/auto/note.md")
    assert policy.can_auto_write("AI/auto/note.md")
    assert not policy.can_auto_write("AI/other.md")


def test_none_rule_shadows_writable_parent() -> None:
    policy = _policy(
        FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE),
        FolderRule(path=PurePosixPath("Private"), access=FolderAccess.NONE),
    )

    assert policy.access_for("Public/note.md") is FolderAccess.AUTO_WRITE
    assert policy.access_for("Private/note.md") is FolderAccess.NONE
    assert policy.can_read("Public/note.md")
    assert not policy.can_read("Private/note.md")
    assert policy.has_any_write()


def test_matching_is_segment_boundaried() -> None:
    policy = _policy(
        FolderRule(path=PurePosixPath("notes"), access=FolderAccess.PROPOSE_WRITE)
    )

    assert policy.access_for("notes/note.md") is FolderAccess.PROPOSE_WRITE
    assert policy.access_for("notes2/note.md") is FolderAccess.READ


def test_admin_does_not_bypass_rules() -> None:
    """Admin may manage tokens/settings; folder rules still gate reads/writes."""
    policy = _policy(
        FolderRule(path=PurePosixPath("."), access=FolderAccess.NONE), admin=True
    )

    assert policy.is_admin is True
    assert not policy.can_read("AI/note.md")
    assert not policy.can_propose("AI/note.md")
    assert not policy.can_auto_write("AI/note.md")
    assert not policy.has_any_write()


def test_admin_with_writable_rule_can_write() -> None:
    policy = _policy(
        FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
        admin=True,
    )

    assert policy.is_admin is True
    assert policy.can_read("AI/note.md")
    assert policy.can_propose("AI/note.md")
    assert policy.has_any_write()
    # Unmatched paths default to read; the rule only grants writes under AI.
    assert policy.can_read("Private/note.md")
    assert not policy.can_propose("Private/note.md")
    assert not policy.can_auto_write("Private/note.md")


def test_readable_paths_filters_in_order() -> None:
    policy = _policy(
        FolderRule(path=PurePosixPath("Private"), access=FolderAccess.NONE)
    )

    assert policy.readable_paths(["AI/a.md", "Private/a.md", "AI/b.md", "AI/a.md"]) == [
        "AI/a.md",
        "AI/b.md",
        "AI/a.md",
    ]


def test_agent_private_policy_requires_owner_and_existing_grant() -> None:
    rules = (FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE),)
    owner = AccessPolicy(
        rules, agent_principal="agent:alpha", agent_private_enabled=True
    )
    other = AccessPolicy(
        rules, agent_principal="agent:beta", agent_private_enabled=True, admin=True
    )
    unbound = AccessPolicy(rules, agent_private_enabled=True)
    assert owner.can_auto_write("AgentPrivate/agent:alpha/note.md")
    assert not owner.can_read("AgentPrivate/agent:beta/note.md")
    assert not other.can_read("AgentPrivate/agent:alpha/note.md")
    assert not unbound.can_read("AgentPrivate/agent:alpha/note.md")
    assert not owner.can_read("AgentPrivate")
    assert not unbound.can_read("AgentPrivate")
    assert owner.can_read("AgentPrivateX/agent:beta/note.md")
    assert owner.fingerprint() != AccessPolicy(
        rules, agent_principal="agent:beta", agent_private_enabled=True
    ).fingerprint()


def test_agent_private_requires_folder_grant() -> None:
    policy = AccessPolicy(
        (FolderRule(path=PurePosixPath("AgentPrivate"), access=FolderAccess.NONE),),
        agent_principal="a",
        agent_private_enabled=True,
    )
    assert not policy.can_read("AgentPrivate/a/note.md")


def test_agent_private_namespace_stays_reserved_when_feature_disabled() -> None:
    policy = AccessPolicy(
        (FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE),)
    )
    assert not policy.can_read("AgentPrivate/a/note.md")
    assert not policy.can_read("agentprivate/a/note.md")
    compiled = select(column("path")).where(
        policy.readable_clause(column("path"))
    ).compile(dialect=sqlite.dialect())
    assert "agentprivate" in compiled.params.values()


def test_agent_private_readable_clause_reserves_root_and_filters_owner() -> None:
    policy = AccessPolicy(
        (FolderRule(path=PurePosixPath("."), access=FolderAccess.READ),),
        agent_principal="a",
        agent_private_enabled=True,
    )
    compiled = select(column("path")).where(
        policy.readable_clause(column("path"))
    ).compile(dialect=sqlite.dialect())
    assert "agentprivate" in compiled.params.values()
    assert "AgentPrivate/a" in compiled.params.values()


def test_agent_private_sql_matches_case_sensitive_python_prefix_policy() -> None:
    from sqlalchemy import create_engine, literal

    policy = AccessPolicy(
        (),
        agent_principal="agent",
        agent_private_enabled=True,
        agent_private_root="AI/AgentPrivate",
    )
    paths = (
        "AI/AgentPrivate",
        "AI/AgentPrivate/agent",
        "AI/AgentPrivate/agent/note.md",
        "AI/AgentPrivate/agent2/note.md",
        "AI/AgentPrivate/AGENT/note.md",
        "AI/agentprivate/other/note.md",
    )
    statement = union_all(
        *(select(literal(path).label("path")) for path in paths)
    ).subquery()
    with create_engine("sqlite://").connect() as connection:
        sql_readable = {
            row.path
            for row in connection.execute(
                select(statement.c.path).where(policy.readable_clause(statement.c.path))
            )
        }
    assert sql_readable == {path for path in paths if policy.can_read(path)}


def test_agent_private_path_traversal_fails_closed() -> None:
    policy = AccessPolicy(
        (FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE),),
        agent_principal="a",
        agent_private_enabled=True,
    )
    assert not policy.can_read("AgentPrivate/a/../b.md")
    assert not policy.can_propose("AgentPrivate/a/../b.md")


def test_agent_private_root_can_be_nested_under_index_root() -> None:
    policy = AccessPolicy(
        (FolderRule(path=PurePosixPath("."), access=FolderAccess.READ),),
        agent_principal="alpha",
        agent_private_enabled=True,
        agent_private_root=PurePosixPath("AI/AgentPrivate"),
    )
    assert policy.can_read("AI/AgentPrivate/alpha/note.md")
    assert not policy.can_read("AI/AgentPrivate/beta/note.md")
