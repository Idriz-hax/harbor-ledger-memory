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
    default, _ = service.create("default")
    approved, _ = service.create("approved", approve_own_proposals=True)
    assert default.approve_own_proposals is False
    assert approved.approve_own_proposals is True


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
                (
                    FolderRule(path=PurePosixPath("AI"), access=FolderAccess.DENY),
                ),
            )
        message = str(excinfo.value)
        assert "AI" in message
        assert "'deny'" in message
        for level in ("none", "read", "propose-write", "auto-write"):
            assert level in message
        # The canonical equivalent is accepted.
        created = service.create(
            "none",
            (
                FolderRule(path=PurePosixPath("AI"), access=FolderAccess.NONE),
            ),
        )
        assert created.rules[0].access is FolderAccess.NONE
    finally:
        service.close()


def test_create_with_folder_rules_and_admin(tmp_path: Path) -> None:
    rules = (
        FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
        FolderRule(path=PurePosixPath("AI/Public"),
                   access=FolderAccess.PROPOSE_WRITE),
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


def test_backfill_legacy_tokens_is_idempotent(tmp_path: Path) -> None:
    rules = (
        FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
    )
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
        assert by_name["legacy-admin"].rules == rules
        assert by_name["legacy-admin"].admin is True
        assert by_name["modern"].rules == rules
        assert by_name["modern"].admin is False
        assert service.backfill_legacy_tokens(rules) == 0
    finally:
        service.close()
