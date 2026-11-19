"""Persistent API tokens for source-level access control.

Mirrors ``ActivityService`` construction (database URL + RLock-guarded
session). Tokens are stored as SHA-256 digests; the plaintext is minted once
and handed back to the caller, who prints/returns it exactly one time.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.engine import Engine

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import ApiToken
from harbor_ledger_memory.config import FolderAccess, FolderRule

TOKEN_PREFIX = "hlm_"
LAST_USED_THROTTLE_SECONDS = 60.0
_AGENT_PRINCIPAL_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")


def _validate_agent_principal(value: str | None) -> str | None:
    if value is not None and not _AGENT_PRINCIPAL_RE.fullmatch(value):
        raise InvalidTokenRequestError(
            "agent_principal must be 1-128 lowercase ASCII letters, digits, "
            "'.', '_', ':', or '-'."
        )
    return value


class DuplicateTokenNameError(Exception):
    """A token with the same name is already active."""


class InvalidTokenRequestError(Exception):
    """Invalid token request: empty name, bad rule value, or retired level."""


def encode_token() -> str:
    """Mint a fresh plaintext token."""
    return TOKEN_PREFIX + secrets.token_urlsafe(24)


def hash_token(plaintext: str) -> str:
    """The SHA-256 hex digest stored for a plaintext token."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TokenRecord:
    """Detached token data safe to hand to a response (no hash)."""

    name: str
    rules: tuple[FolderRule, ...]
    admin: bool
    approve_own_proposals: bool
    created_at: datetime
    id: int = 0
    last_used_at: str | None = None
    revoked_at: str | None = None
    agent_principal: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rules": [rule.model_dump(mode="json") for rule in self.rules],
            "admin": self.admin,
            "approve_own_proposals": self.approve_own_proposals,
            "created_at": self.created_at.isoformat(),
            "last_used_at": self.last_used_at,
            "revoked_at": self.revoked_at,
            "agent_principal": self.agent_principal,
        }


TokenSummary = TokenRecord


class CreatedToken(tuple[TokenRecord, str]):
    """A newly created token plus its one-time plaintext.

    Subclassing the historical ``(record, plaintext)`` tuple keeps legacy
    unpacking working while exposing explicit attributes for new callers.
    """

    _name: str
    _rules: tuple[FolderRule, ...]
    _admin: bool
    _approve_own_proposals: bool
    _agent_principal: str | None

    def __new__(
        cls,
        record: TokenRecord,
        plaintext: str,
        name: str,
        rules: tuple[FolderRule, ...],
        admin: bool,
        approve_own_proposals: bool,
        agent_principal: str | None,
    ) -> CreatedToken:
        instance = super().__new__(cls, (record, plaintext))
        instance._name = name
        instance._rules = rules
        instance._admin = admin
        instance._approve_own_proposals = approve_own_proposals
        instance._agent_principal = agent_principal
        return instance

    def __init__(
        self,
        record: TokenRecord,
        plaintext: str,
        name: str,
        rules: tuple[FolderRule, ...],
        admin: bool,
        approve_own_proposals: bool,
        agent_principal: str | None,
    ) -> None:
        self._name = name
        self._rules = rules
        self._admin = admin
        self._approve_own_proposals = approve_own_proposals
        self._agent_principal = agent_principal

    @property
    def record(self) -> TokenRecord:
        return self[0]

    @property
    def plaintext(self) -> str:
        return self[1]

    @property
    def name(self) -> str:
        return self._name

    @property
    def rules(self) -> tuple[FolderRule, ...]:
        return self._rules

    @property
    def admin(self) -> bool:
        return self._admin

    @property
    def approve_own_proposals(self) -> bool:
        return self._approve_own_proposals

    @property
    def agent_principal(self) -> str | None:
        return self._agent_principal


def _scopes_from_json(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    raw: object = json.loads(value)
    if not isinstance(raw, list):
        return ()
    return tuple(str(scope) for scope in cast(list[object], raw))


def _rules_from_json(value: str | None) -> tuple[FolderRule, ...]:
    if value is None:
        return ()
    raw: object = json.loads(value)
    if not isinstance(raw, list):
        return ()
    return tuple(FolderRule(**item) for item in cast(list[dict[str, Any]], raw))


def _rules_to_json(rules: Sequence[FolderRule]) -> str:
    return json.dumps([rule.model_dump(mode="json") for rule in rules])


def _created_at_from_value(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(UTC)


def _record_from_row(row: ApiToken) -> TokenRecord:
    return TokenRecord(
        name=row.name,
        rules=_rules_from_json(row.rules),
        admin=bool(row.admin),
        approve_own_proposals=bool(row.approve_own_proposals),
        created_at=_created_at_from_value(row.created_at),
        id=row.id,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        agent_principal=row.agent_principal,
    )


# Levels accepted for new token rules. The retired ``deny`` level is an alias
# of ``none`` kept for persisted settings and legacy token rows only.
TOKEN_RULE_LEVELS: tuple[str, ...] = (
    FolderAccess.NONE.value,
    FolderAccess.READ.value,
    FolderAccess.PROPOSE_WRITE.value,
    FolderAccess.AUTO_WRITE.value,
)


def _parse_create_rules(
    rules: Sequence[FolderRule] | Sequence[str] | None,
) -> tuple[FolderRule, ...]:
    """Validate create-time rules; plain scope strings are no longer accepted."""
    if rules is None:
        return ()
    if isinstance(rules, str):
        raise InvalidTokenRequestError("rules must be a sequence, not a string")
    for value in tuple(rules):
        if not isinstance(value, FolderRule):
            raise InvalidTokenRequestError(
                f"rules must be FolderRule values, not {type(value).__name__}"
            )
        if value.access is FolderAccess.DENY:
            raise InvalidTokenRequestError(
                f"rule for {value.path.as_posix()!r} uses the retired level "
                f"'deny'; valid levels: {', '.join(TOKEN_RULE_LEVELS)}"
            )
    return cast(tuple[FolderRule, ...], rules)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


class TokenService:
    """Create, list, verify, and revoke API tokens in the catalog DB."""

    def __init__(self, source: str | Engine) -> None:
        self._lock = threading.RLock()
        self._owns_engine = isinstance(source, str)
        if isinstance(source, str):
            # Idempotent: skips already-applied migrations. Keeps CLI and
            # service startup both safe on a fresh database file.
            from harbor_ledger_memory.catalog.migrate import upgrade_to_head

            upgrade_to_head(source)
            engine = create_database(source)
        else:
            engine = source
        self._engine = engine
        self._session = CatalogSession(bind=engine)

    def close(self) -> None:
        with self._lock:
            self._session.close()
            if self._owns_engine:
                self._engine.dispose()

    def create(
        self,
        name: str,
        rules: Sequence[FolderRule] | None = None,
        admin: bool = False,
        approve_own_proposals: bool = False,
        agent_principal: str | None = None,
        *,
        activity: Any = None,
        actor: dict[str, str | None] | None = None,
    ) -> CreatedToken:
        """Create a token; returns the record and the one-time plaintext."""
        name = name.strip()
        if not name:
            raise InvalidTokenRequestError("token name must not be empty")
        parsed_rules = _parse_create_rules(rules)
        agent_principal = _validate_agent_principal(agent_principal)
        scopes_json = "[]"
        rules_json = _rules_to_json(parsed_rules)
        with self._lock:
            existing = self._session.scalar(
                select(ApiToken).where(
                    ApiToken.name == name, ApiToken.revoked_at.is_(None)
                )
            )
            if existing is not None:
                raise DuplicateTokenNameError(
                    f"an active token named '{name}' already exists"
                )
            plaintext = encode_token()
            row = ApiToken(
                name=name,
                token_hash=hash_token(plaintext),
                scopes=scopes_json,
                rules=rules_json,
                admin=admin,
                approve_own_proposals=approve_own_proposals,
                agent_principal=agent_principal,
                created_at=_timestamp(),
            )
            self._session.add(row)
            self._session.commit()
            record = _record_from_row(row)
        if activity is not None:
            activity.record(
                "token_created",
                {
                    "name": name,
                    "actor": actor,
                    "target": {
                        "name": record.name,
                        "agent_principal": record.agent_principal,
                    },
                    "rules": [rule.model_dump(mode="json") for rule in parsed_rules],
                    "admin": admin,
                    "approve_own_proposals": approve_own_proposals,
                },
            )
        return CreatedToken(
            record=record,
            plaintext=plaintext,
            name=name,
            rules=parsed_rules,
            admin=admin,
            approve_own_proposals=approve_own_proposals,
            agent_principal=agent_principal,
        )

    def update(
        self,
        name: str,
        *,
        rules: Sequence[FolderRule] | None = None,
        admin: bool | None = None,
        approve_own_proposals: bool | None = None,
        agent_principal: str | None = None,
        set_agent_principal: bool = False,
        activity: Any = None,
        actor: dict[str, str | None] | None = None,
    ) -> TokenRecord | None:
        """Update an active token's permission metadata in place.

        ``None`` fields stay unchanged; rules reuse the create-time
        validation. Returns the updated record, or ``None`` when no active
        token has the name (unknown or revoked). The token hash is never
        touched, so the existing plaintext stays valid.
        """
        if (
            rules is None
            and admin is None
            and approve_own_proposals is None
            and not set_agent_principal
        ):
            raise InvalidTokenRequestError(
                "nothing to update: provide rules, admin, or approve_own_proposals"
            )
        if rules is not None:
            rules = _parse_create_rules(rules)
        if set_agent_principal:
            agent_principal = _validate_agent_principal(agent_principal)
        with self._lock:
            row = self._session.scalar(
                select(ApiToken).where(
                    ApiToken.name == name, ApiToken.revoked_at.is_(None)
                )
            )
            if row is None:
                return None
            if (
                set_agent_principal
                and row.agent_principal is not None
                and row.agent_principal != agent_principal
            ):
                raise InvalidTokenRequestError(
                    "agent principal is immutable once assigned; revoke and "
                    "create a new token"
                )
            if rules is not None:
                row.rules = _rules_to_json(rules)
            if admin is not None:
                row.admin = admin
            if approve_own_proposals is not None:
                row.approve_own_proposals = approve_own_proposals
            if set_agent_principal:
                row.agent_principal = agent_principal
            self._session.commit()
            record = _record_from_row(row)
        if activity is not None:
            activity.record(
                "token_updated",
                {
                    "name": record.name,
                    "actor": actor,
                    "target": {
                        "name": record.name,
                        "agent_principal": record.agent_principal,
                    },
                    "rules": [rule.model_dump(mode="json") for rule in record.rules],
                    "admin": record.admin,
                    "approve_own_proposals": record.approve_own_proposals,
                },
            )
        return record

    def list(self) -> list[TokenRecord]:
        with self._lock:
            rows = self._session.scalars(select(ApiToken).order_by(ApiToken.id)).all()
            return [_record_from_row(row) for row in rows]

    def count_active(self) -> int:
        """Number of non-revoked tokens (0 means /mcp locks every caller)."""
        with self._lock:
            return len(
                self._session.scalars(
                    select(ApiToken).where(ApiToken.revoked_at.is_(None))
                ).all()
            )

    def revoke(
        self,
        name: str,
        *,
        activity: Any = None,
        actor: dict[str, str | None] | None = None,
    ) -> TokenRecord | None:
        """Soft-revoke by name; idempotent. None when the name is unknown.

        A name can match several rows once it is reused after revocation, so
        prefer the active row and fall back to the newest row.
        """
        with self._lock:
            rows = self._session.scalars(
                select(ApiToken)
                .where(ApiToken.name == name)
                .order_by(ApiToken.id.desc())
            ).all()
            if not rows:
                return None
            row = next((r for r in rows if r.revoked_at is None), rows[0])
            was_active = row.revoked_at is None
            if was_active:
                row.revoked_at = _timestamp()
                self._session.commit()
            record = _record_from_row(row)
        if activity is not None and was_active:
            activity.record(
                "token_revoked",
                {
                    "name": name,
                    "actor": actor,
                    "target": {
                        "name": record.name,
                        "agent_principal": record.agent_principal,
                    },
                },
            )
        return record

    def verify(self, plaintext: str) -> TokenRecord | None:
        """Resolve a plaintext token to its active record, else None."""
        if not plaintext.startswith(TOKEN_PREFIX):
            return None
        digest = hash_token(plaintext)
        with self._lock:
            row = self._session.scalar(
                select(ApiToken).where(
                    ApiToken.token_hash == digest, ApiToken.revoked_at.is_(None)
                )
            )
            if row is None:
                return None
            self._touch_last_used(row)
            self._session.commit()
            return _record_from_row(row)

    def backfill_legacy_tokens(
        self,
        folder_rules: Sequence[FolderRule],
    ) -> int:
        """Copy global folder rules into legacy rows; returns rows updated."""
        rules_json = _rules_to_json(folder_rules)
        with self._lock:
            rows = self._session.scalars(
                select(ApiToken).where(ApiToken.rules.is_(None))
            ).all()
            for row in rows:
                scopes = _scopes_from_json(row.scopes)
                row.rules = rules_json
                row.admin = True if "admin" in scopes else bool(row.admin)
            self._session.commit()
            return len(rows)

    def replace_active_rules(self, rules: Sequence[FolderRule]) -> int:
        """Replace rules on active tokens only; returns rows updated."""
        rules_json = _rules_to_json(rules)
        with self._lock:
            try:
                rows = self._session.scalars(
                    select(ApiToken).where(ApiToken.revoked_at.is_(None))
                ).all()
                for row in rows:
                    row.rules = rules_json
                self._session.commit()
            except Exception:
                self._session.rollback()
                raise
            return len(rows)

    def snapshot_active_rules(self) -> dict[int, str | None]:
        """Return the raw rules value for every active token."""
        with self._lock:
            rows = self._session.execute(
                select(ApiToken.id, ApiToken.rules).where(ApiToken.revoked_at.is_(None))
            ).all()
            return {id: rules for id, rules in rows}

    def restore_active_rules(self, snapshot: dict[int, str | None]) -> int:
        """Restore snapshotted rules on their still-active tokens only."""
        with self._lock:
            try:
                rows = self._session.scalars(
                    select(ApiToken).where(
                        ApiToken.id.in_(snapshot), ApiToken.revoked_at.is_(None)
                    )
                ).all()
                for row in rows:
                    row.rules = snapshot[row.id]
                self._session.commit()
            except Exception:
                self._session.rollback()
                raise
            return len(rows)

    def _touch_last_used(self, row: ApiToken) -> None:
        if row.last_used_at is None:
            row.last_used_at = _timestamp()
            return
        try:
            previous = datetime.fromisoformat(row.last_used_at)
        except ValueError:
            row.last_used_at = _timestamp()
            return
        age = (datetime.now(UTC) - previous).total_seconds()
        if age >= LAST_USED_THROTTLE_SECONDS:
            row.last_used_at = _timestamp()
