"""Scoped, explicit memory marks; legacy adaptive state is intentionally inert."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.models import MemoryMark, Note, QueryTrace

_KINDS = frozenset({"pin", "relevant", "irrelevant"})


class MarkError(ValueError):
    """Base error for explicit mark operations."""


class MarkCapacityError(MarkError):
    """The scope has reached its active mark capacity."""


class TraceScopeError(MarkError):
    """The trace is legacy or belongs to another scope."""


@dataclass(frozen=True)
class MemoryMarkScope:
    scope_kind: str
    scope_id: str

    def __post_init__(self) -> None:
        if not self.scope_kind or not self.scope_id or len(self.scope_id) > 128:
            raise ValueError("invalid memory mark scope")
        if any(secret in self.scope_id.lower() for secret in ("secret", "token=")):
            raise ValueError("memory mark scope must not contain secret material")


def token_scope(token_id: int | None, ui_session: str | None = None) -> MemoryMarkScope:
    """Derive a stable non-secret scope identifier for an authenticated caller."""
    if token_id is not None:
        return MemoryMarkScope("token", str(token_id))
    if ui_session:
        return MemoryMarkScope("ui", sha256(ui_session.encode()).hexdigest())
    return MemoryMarkScope("cli", "local")


class MemoryMarkService:
    """Own mark lifecycle, policy checks, scope isolation, and bounded pin boosts."""

    def __init__(
        self, session: Session, *, max_marks: int = 100, pin_boost: float = 0.20
    ):
        self._session = session
        self._max_marks = max_marks
        self._pin_boost = min(max(pin_boost, 0.0), 0.20)

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _active(self, scope: MemoryMarkScope, now: datetime | None = None):
        current = (now or self._now()).isoformat()
        return self._session.scalars(
            select(MemoryMark).where(
                MemoryMark.scope_kind == scope.scope_kind,
                MemoryMark.scope_id == scope.scope_id,
                MemoryMark.revoked_at.is_(None),
                (MemoryMark.expires_at.is_(None) | (MemoryMark.expires_at > current)),
            )
        ).all()

    def _check_trace(self, scope: MemoryMarkScope, trace_uuid: str) -> None:
        trace = self._session.scalar(
            select(QueryTrace).where(QueryTrace.trace_uuid == trace_uuid)
        )
        if (
            trace is None
            or trace.scope_kind is None
            or trace.scope_id is None
            or trace.scope_kind != scope.scope_kind
            or trace.scope_id != scope.scope_id
        ):
            raise TraceScopeError("trace is not owned by this scope")

    def validate_trace(self, scope: MemoryMarkScope, trace_uuid: str) -> None:
        self._check_trace(scope, trace_uuid)

    def create_mark(
        self,
        scope: MemoryMarkScope,
        trace_uuid: str,
        path: str,
        kind: str,
        can_read: Callable[[str], bool],
        *,
        expires_at: datetime | None = None,
    ) -> MemoryMark:
        if kind not in _KINDS:
            raise MarkError("invalid memory mark kind")
        if (
            not can_read(path)
            or self._session.scalar(select(Note.path).where(Note.path == path)) is None
        ):
            raise MarkError("memory mark path is not readable and indexed")
        self._check_trace(scope, trace_uuid)
        self.expire()
        if len(self._active(scope)) >= self._max_marks:
            raise MarkCapacityError("memory mark capacity reached")
        mark = MemoryMark(
            scope_kind=scope.scope_kind,
            scope_id=scope.scope_id,
            trace_uuid=trace_uuid,
            path=path,
            kind=kind,
            created_at=self._now().isoformat(),
            expires_at=expires_at.isoformat() if expires_at else None,
        )
        self._session.add(mark)
        self._session.flush()
        return mark

    def expire(self) -> int:
        now = self._now().isoformat()
        self._session.execute(
            update(MemoryMark)
            .where(MemoryMark.expires_at.is_not(None), MemoryMark.expires_at <= now)
            .values(revoked_at=now)
        )
        return len(
            self._session.scalars(
                select(MemoryMark).where(MemoryMark.revoked_at == now)
            ).all()
        )

    def list_marks(
        self, scope: MemoryMarkScope, can_read: Callable[[str], bool]
    ) -> list[MemoryMark]:
        self.expire()
        return [mark for mark in self._active(scope) if can_read(mark.path)]

    def pin_boosts(
        self, scope: MemoryMarkScope, can_read: Callable[[str], bool]
    ) -> dict[str, float]:
        return {
            mark.path: self._pin_boost
            for mark in self.list_marks(scope, can_read)
            if mark.kind == "pin"
        }

    def reset(self, scope: MemoryMarkScope, can_read: Callable[[str], bool]) -> int:
        now = self._now().isoformat()
        paths = [mark.path for mark in self._active(scope) if can_read(mark.path)]
        if not paths:
            return 0
        self._session.execute(
            update(MemoryMark)
            .where(
                MemoryMark.scope_kind == scope.scope_kind,
                MemoryMark.scope_id == scope.scope_id,
                MemoryMark.path.in_(paths),
                MemoryMark.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        self._session.flush()
        return len(paths)
