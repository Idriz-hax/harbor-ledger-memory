"""Process-local, expiring browser sessions for the Web UI."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from harbor_ledger_memory.config import FolderAccess, FolderRule
from harbor_ledger_memory.services.access import AccessPolicy

UI_SESSION_COOKIE = "hlm_ui_session"
UI_CSRF_COOKIE = "hlm_ui_csrf"
UI_CSRF_HEADER = "X-HLM-CSRF"
UI_SESSION_MAX = 1024


class UiSessionCapacityError(RuntimeError):
    """Raised when no bounded UI session slot is available."""


@dataclass
class _Session:
    created: float
    last_seen: float
    csrf: str


class UiSessionService:
    """Issue opaque sessions, retaining only SHA-256 identifiers in memory."""

    def __init__(
        self,
        *,
        idle_seconds: float = 1800,
        absolute_seconds: float = 86400,
        max_sessions: int = UI_SESSION_MAX,
        clock: object = time.monotonic,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        self.idle_seconds = idle_seconds
        self.absolute_seconds = absolute_seconds
        self.max_sessions = max_sessions
        self._clock = clock  # injectable for deterministic expiry tests
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("ascii")).hexdigest()

    def new_session(self) -> str:
        now = self._now()
        with self._lock:
            self._prune_expired(now)
            if len(self._sessions) >= self.max_sessions:
                raise UiSessionCapacityError("UI session capacity reached")
            raw = secrets.token_urlsafe(32)
            self._sessions[self._digest(raw)] = _Session(
                now, now, secrets.token_urlsafe(32)
            )
        return raw

    def _prune_expired(self, now: float) -> None:
        expired = [
            key
            for key, record in self._sessions.items()
            if now - record.last_seen > self.idle_seconds
            or now - record.created > self.absolute_seconds
        ]
        for key in expired:
            self._sessions.pop(key, None)

    def authenticate(self, session: str | None) -> bool:
        return self.get(session) is not None

    def is_valid(self, session: str | None) -> bool:
        """Check expiry/revocation without extending idle lifetime."""
        if not session:
            return False
        record = self._sessions.get(self._digest(session))
        if record is None:
            return False
        now = self._now()
        if now - record.last_seen > self.idle_seconds or now - record.created > (
            self.absolute_seconds
        ):
            self.revoke(session)
            return False
        return True

    def get(self, session: str | None) -> _Session | None:
        if not session:
            return None
        record = self._sessions.get(self._digest(session))
        if record is None:
            return None
        now = self._now()
        if now - record.last_seen > self.idle_seconds or now - record.created > (
            self.absolute_seconds
        ):
            self.revoke(session)
            return None
        record.last_seen = now
        return record

    def revoke(self, session: str | None) -> None:
        if session:
            with self._lock:
                self._sessions.pop(self._digest(session), None)

    def csrf(self, session: str | None) -> str | None:
        record = self.get(session)
        return record.csrf if record else None

    @staticmethod
    def policy() -> AccessPolicy:
        return AccessPolicy(
            rules=(
                FolderRule(path=PurePosixPath("."), access=FolderAccess.PROPOSE_WRITE),
            ),
            admin=True,
        )
