"""AccessPolicy prefix-matching coverage."""

from __future__ import annotations

from pathlib import PurePosixPath

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

    assert policy.readable_paths(
        ["AI/a.md", "Private/a.md", "AI/b.md", "AI/a.md"]
    ) == ["AI/a.md", "AI/b.md", "AI/a.md"]
