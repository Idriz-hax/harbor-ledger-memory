"""TokenService storage and lifecycle coverage."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import cast

import pytest
from sqlalchemy import create_engine, text

from harbor_ledger_memory.config import FolderAccess, FolderRule
from harbor_ledger_memory.services.tokens import (
    CreatedToken,
    DuplicateTokenNameError,
    InvalidTokenRequestError,
    TokenService,
    hash_token,
)


def _service(tmp_path: Path) -> TokenService:
    return TokenService(f"sqlite:///{tmp_path / 'tokens.db'}")


def _insert_legacy_row(db: Path, name: str, token_hash: str, scopes: str) -> None:
    """Seed a pre-rules row (``rules IS NULL``) the way old clients left it."""
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(name, token_hash, scopes, created_at, revoked_at) "
                    "VALUES (:name, :token_hash, :scopes, "
                    "'2026-08-31T00:00:00+00:00', NULL)"
                ),
                {"name": name, "token_hash": token_hash, "scopes": scopes},
            )
    finally:
        engine.dispose()


def test_create_returns_record_and_printable_token(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        created = service.create("opencode")
        record, plaintext = created
        assert isinstance(created, CreatedToken)
        assert created.name == "opencode"
        assert created.rules == ()
        assert created.admin is False
        assert created.plaintext == plaintext
        assert plaintext.startswith("hlm_")
        assert record.name == "opencode"
        assert record.rules == ()
        assert record.admin is False
        assert record.revoked_at is None
        assert record.active is True
        assert isinstance(record.created_at, datetime)
        listed = service.list()
        assert [token.name for token in listed] == ["opencode"]
        payload = listed[0].as_dict()
        assert payload["rules"] == []
        assert payload["admin"] is False
        assert "scopes" not in payload
        assert "token_hash" not in payload
        assert "token" not in payload
    finally:
        service.close()


def test_create_without_rules_is_read_only(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        created = service.create("default")
        record, _ = created
        assert created.rules == ()
        assert created.admin is False
        assert record.rules == ()
        payload = record.as_dict()
        assert payload["rules"] == []
        assert payload["admin"] is False
    finally:
        service.close()


def test_token_approval_scope_defaults_false_and_round_trips(tmp_path: Path) -> None:
    service = TokenService(f"sqlite:///{tmp_path / 'catalog.db'}")
    try:
        default, default_plaintext = service.create("default")
        approved, approved_plaintext = service.create(
            "approved", approve_own_proposals=True
        )
        assert default.approve_own_proposals is False
        assert approved.approve_own_proposals is True

        listed = {record.name: record for record in service.list()}
        assert listed["default"].approve_own_proposals is False
        assert listed["approved"].approve_own_proposals is True

        verified_default = service.verify(default_plaintext)
        verified_approved = service.verify(approved_plaintext)
        assert verified_default is not None
        assert verified_default.approve_own_proposals is False
        assert verified_approved is not None
        assert verified_approved.approve_own_proposals is True
    finally:
        service.close()


def test_create_rejects_bad_rules_and_duplicate_active_name(tmp_path: Path) -> None:
    rule = FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ)
    service = _service(tmp_path)
    try:
        with pytest.raises(InvalidTokenRequestError):
            service.create("bad", cast(Sequence[FolderRule], ("read",)))
        with pytest.raises(InvalidTokenRequestError):
            service.create(
                "mixed",
                cast(
                    Sequence[FolderRule],
                    ["read", rule],
                ),
            )
        service.create("opencode", (rule,))
        with pytest.raises(DuplicateTokenNameError):
            service.create("opencode", (rule,))
    finally:
        service.close()


def test_create_rejects_retired_deny_level(tmp_path: Path) -> None:
    """``deny`` is retired for new tokens; ``none`` is the accepted equivalent."""
    service = _service(tmp_path)
    try:
        with pytest.raises(InvalidTokenRequestError) as excinfo:
            service.create(
                "deny",
                (FolderRule(path=PurePosixPath("AI"), access=FolderAccess.DENY),),
            )
        message = str(excinfo.value)
        assert "AI" in message
        assert "'deny'" in message
        for level in ("none", "read", "propose-write", "auto-write"):
            assert level in message
        # The canonical equivalent is accepted.
        created = service.create(
            "none",
            (FolderRule(path=PurePosixPath("AI"), access=FolderAccess.NONE),),
        )
        assert created.rules[0].access is FolderAccess.NONE
    finally:
        service.close()


def test_create_with_folder_rules_and_admin(tmp_path: Path) -> None:
    rules = (
        FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
        FolderRule(path=PurePosixPath("AI/Public"), access=FolderAccess.PROPOSE_WRITE),
    )
    service = _service(tmp_path)
    try:
        created = service.create("agent", rules, admin=True)
        record, plaintext = created
        assert created.name == "agent"
        assert created.rules == rules
        assert created.admin is True
        assert record.rules == rules
        assert record.admin is True
        payload = record.as_dict()
        assert payload["rules"] == [
            {"path": "AI", "access": "read"},
            {"path": "AI/Public", "access": "propose-write"},
        ]
        assert payload["admin"] is True
        assert payload["created_at"] == record.created_at.isoformat()
        assert "scopes" not in payload
        assert "token_hash" not in payload
        assert "token" not in payload
        verified = service.verify(plaintext)
        assert verified is not None
        assert verified.rules == rules
        assert verified.admin is True
    finally:
        service.close()


def test_verify_matches_printed_token_and_throttles_last_used(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        record, plaintext = service.create("agent")
        verified = service.verify(plaintext)
        assert verified is not None
        assert verified.name == record.name
        assert verified.last_used_at is not None
        # Immediate re-verify is within the 60 s throttle: timestamp unchanged.
        again = service.verify(plaintext)
        assert again is not None
        assert again.last_used_at == verified.last_used_at
        assert service.verify("hlm_wrong-token") is None
        assert service.verify("not-a-token") is None
    finally:
        service.close()


def test_plaintext_never_stored_but_hash_is(tmp_path: Path) -> None:
    db = tmp_path / "raw.db"
    service = TokenService(f"sqlite:///{db}")
    try:
        _, plaintext = service.create("raw")
        raw = db.read_bytes()
        assert plaintext.encode("utf-8") not in raw
        assert hash_token(plaintext).encode("ascii") in raw
    finally:
        service.close()


def test_revoke_is_idempotent_and_blocks_verify(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        _, plaintext = service.create("gone")
        assert service.verify(plaintext) is not None
        revoked = service.revoke("gone")
        assert revoked is not None
        assert revoked.revoked_at is not None
        assert service.verify(plaintext) is None
        assert service.revoke("gone") is not None  # idempotent
        assert service.revoke("never-existed") is None
    finally:
        service.close()


def test_revoke_after_name_reuse_revokes_active_row(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        first, _ = service.create("alpha")
        service.revoke("alpha")
        rows = {token.id: token for token in service.list()}
        old_revoked_at = rows[first.id].revoked_at
        assert old_revoked_at is not None
        second, second_token = service.create("alpha")
        assert second.id > first.id
        revoked = service.revoke("alpha")
        assert revoked is not None
        # The new active row is the one revoked, not the old one.
        assert revoked.id == second.id
        rows = {token.id: token for token in service.list()}
        assert rows[first.id].revoked_at == old_revoked_at
        assert rows[second.id].revoked_at is not None
        assert service.verify(second_token) is None
        assert not any(token.active for token in service.list())
    finally:
        service.close()


def test_replace_active_rules_leaves_revoked_tokens_unchanged(tmp_path: Path) -> None:
    service = _service(tmp_path)
    old_rules = (FolderRule(path=PurePosixPath("Old"), access=FolderAccess.NONE),)
    new_rules = (
        FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
    )
    try:
        active, _ = service.create("active", old_rules, admin=True)
        revoked, _ = service.create("revoked", old_rules)
        service.revoke("revoked")

        assert service.replace_active_rules(new_rules) == 1

        records = {record.name: record for record in service.list()}
        assert records["active"].rules == new_rules
        assert records["active"].admin is True
        assert records["revoked"].rules == old_rules
        assert records["revoked"].revoked_at is not None
        assert active.id != revoked.id
    finally:
        service.close()


def test_active_rule_snapshot_restores_each_rule_including_legacy_none(
    tmp_path: Path,
) -> None:
    db = tmp_path / "tokens.db"
    service = TokenService(f"sqlite:///{db}")
    original_rules = (
        FolderRule(path=PurePosixPath("Private"), access=FolderAccess.NONE),
    )
    replacement = (FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),)
    try:
        active, _ = service.create("active", original_rules)
        _insert_legacy_row(db, "legacy", "legacy-hash", "[]")
        snapshot = service.snapshot_active_rules()
        assert snapshot[active.id] is not None
        assert None in snapshot.values()

        service.replace_active_rules(replacement)
        service.restore_active_rules(snapshot)

        assert service.snapshot_active_rules() == snapshot
    finally:
        service.close()


def test_backfill_legacy_tokens_is_idempotent(tmp_path: Path) -> None:
    rules = (FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),)
    db = tmp_path / "tokens.db"
    service = TokenService(f"sqlite:///{db}")
    try:
        _insert_legacy_row(db, "legacy", "hash-1", '["read", "propose"]')
        _insert_legacy_row(db, "legacy-admin", "hash-2", '["read", "admin"]')
        service.create("modern", rules)
        assert service.backfill_legacy_tokens(rules) == 2
        by_name = {record.name: record for record in service.list()}
        assert by_name["legacy"].rules == rules
        assert by_name["legacy"].admin is False
        assert by_name["legacy"].approve_own_proposals is False
        assert by_name["legacy-admin"].rules == rules
        assert by_name["legacy-admin"].admin is True
        assert by_name["legacy-admin"].approve_own_proposals is False
        assert by_name["modern"].rules == rules
        assert by_name["modern"].admin is False
        assert by_name["modern"].approve_own_proposals is False
        assert service.backfill_legacy_tokens(rules) == 0
    finally:
        service.close()


class _RecordingActivity:
    """Minimal ActivityService stand-in: keeps ``(type, payload)`` pairs."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(self, event_type: str, payload: dict[str, object]) -> None:
        self.events.append((event_type, payload))


def _stored_hash(db: Path, name: str) -> str:
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT token_hash FROM api_tokens WHERE name = :name"),
                {"name": name},
            ).first()
        assert row is not None
        return str(row[0])
    finally:
        engine.dispose()


def test_update_changes_permissions_in_place_and_keeps_hash(
    tmp_path: Path,
) -> None:
    db = tmp_path / "tokens.db"
    service = TokenService(f"sqlite:///{db}")
    try:
        record, plaintext = service.create(
            "agent",
            (FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),),
        )
        old_hash = _stored_hash(db, "agent")
        new_rules = (
            FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
            FolderRule(path=PurePosixPath("Inbox"), access=FolderAccess.NONE),
        )
        updated = service.update(
            "agent", rules=new_rules, admin=True, approve_own_proposals=True
        )
        assert updated is not None
        assert updated.name == "agent"
        assert updated.rules == new_rules
        assert updated.admin is True
        assert updated.approve_own_proposals is True
        assert updated.created_at == record.created_at
        assert updated.active is True
        # The stored hash is untouched: the original plaintext still verifies.
        assert _stored_hash(db, "agent") == old_hash
        verified = service.verify(plaintext)
        assert verified is not None
        assert verified.rules == new_rules
        assert verified.admin is True
        assert verified.approve_own_proposals is True
    finally:
        service.close()


def test_update_partial_keeps_unspecified_fields(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        record, _ = service.create(
            "agent",
            (FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),),
            admin=True,
            approve_own_proposals=True,
        )
        updated = service.update("agent", admin=False)
        assert updated is not None
        assert updated.admin is False
        # Unspecified fields stay as they were.
        assert updated.rules == record.rules
        assert updated.approve_own_proposals is True
        assert updated.created_at == record.created_at
    finally:
        service.close()


def test_update_rejects_missing_and_revoked_tokens(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        _, plaintext = service.create("gone")
        assert service.update("never-existed", admin=True) is None
        service.revoke("gone")
        assert service.update("gone", admin=True) is None
        # An update attempt never revives a revoked token.
        assert service.verify(plaintext) is None
        assert all(record.revoked_at is not None for record in service.list())
    finally:
        service.close()


def test_update_reuses_create_validation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        service.create("agent")
        with pytest.raises(InvalidTokenRequestError):
            service.update("agent")  # nothing to update
        with pytest.raises(InvalidTokenRequestError):
            service.update("agent", rules=cast(Sequence[FolderRule], ("read",)))
        with pytest.raises(InvalidTokenRequestError):
            service.update(
                "agent",
                rules=(
                    FolderRule(path=PurePosixPath("AI"), access=FolderAccess.DENY),
                ),
            )
        # Failed updates leave the row untouched.
        listed = service.list()
        assert listed[0].rules == ()
        assert listed[0].admin is False
        assert listed[0].approve_own_proposals is False
    finally:
        service.close()


def test_update_records_activity_event(tmp_path: Path) -> None:
    service = _service(tmp_path)
    activity = _RecordingActivity()
    try:
        service.create("agent")
        service.update("agent", admin=True, activity=activity)
        assert activity.events == [
            (
                "token_updated",
                {
                    "name": "agent",
                    "rules": [],
                    "admin": True,
                    "approve_own_proposals": False,
                },
            )
        ]
        # Rejected (unknown) updates record nothing.
        activity.events.clear()
        assert service.update("ghost", admin=True, activity=activity) is None
        assert activity.events == []
    finally:
        service.close()
