"""Short-term memory service for recent query activity."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.models import Note, ShortTermEntry, ShortTermEvent
from harbor_ledger_memory.domain.memory import CacheRefresh


class MemoryService:
    """Manages short-term memory events and recent path boosting."""

    def __init__(self, session: Session, settings: Any) -> None:
        self._session = session
        self._settings = settings
        self._last_cleanup: float = 0.0
        self._cleanup_interval: float = 300.0  # 5 minutes

    def record_query(
        self,
        trace_uuid: str,
        query_text: str,
        selected_paths: list[str],
    ) -> None:
        """Record a query event for short-term memory."""
        now = datetime.now(UTC)
        expires = now + timedelta(hours=self._settings.short_term_window_hours)
        event = ShortTermEvent(
            trace_uuid=trace_uuid,
            query_text=query_text,
            selected_paths=json.dumps(selected_paths, ensure_ascii=False),
            created_at=now.isoformat(),
            expires_at=expires.isoformat(),
        )
        self._session.add(event)

    def refresh_selected(self, paths: list[str]) -> CacheRefresh:
        """Refresh selected notes in the bounded short-term cache."""
        self.cleanup_cache()
        selected_paths = tuple(dict.fromkeys(paths))
        if not selected_paths:
            return CacheRefresh(refreshed_paths=(), evicted_paths=())

        known_paths = set(
            self._session.scalars(
                select(Note.path).where(Note.path.in_(selected_paths))
            )
        )
        selected_paths = tuple(path for path in selected_paths if path in known_paths)
        if not selected_paths:
            return CacheRefresh(refreshed_paths=(), evicted_paths=())

        entries = {
            entry.path: entry
            for entry in self._session.scalars(
                select(ShortTermEntry).where(ShortTermEntry.path.in_(selected_paths))
            )
        }
        now = datetime.now(UTC).isoformat()
        for path in selected_paths:
            entry = entries.get(path)
            if entry is None:
                self._session.add(
                    ShortTermEntry(
                        path=path,
                        last_selected_at=now,
                        selection_count=1,
                    )
                )
            else:
                entry.last_selected_at = now
                entry.selection_count += 1

        self._session.flush()
        ordered_entries = self._session.scalars(
            select(ShortTermEntry).order_by(
                ShortTermEntry.last_selected_at.asc(), ShortTermEntry.path.asc()
            )
        ).all()
        overflow = ordered_entries[
            : max(0, len(ordered_entries) - self._settings.short_term_capacity)
        ]
        for entry in overflow:
            self._session.delete(entry)

        return CacheRefresh(
            refreshed_paths=selected_paths,
            evicted_paths=tuple(entry.path for entry in overflow),
        )

    def cleanup_cache(self) -> tuple[int, int]:
        """Delete expired cache entries and entries without a current note."""
        cutoff = (
            datetime.now(UTC) - timedelta(days=self._settings.short_term_ttl_days)
        ).isoformat()
        expired = _rowcount(
            self._session.execute(
                delete(ShortTermEntry).where(ShortTermEntry.last_selected_at < cutoff)
            )
        )
        missing = _rowcount(
            self._session.execute(
                delete(ShortTermEntry).where(
                    ~ShortTermEntry.path.in_(select(Note.path))
                )
            )
        )
        return expired, missing

    def cache_candidates(self) -> dict[str, float]:
        """Return valid short-term cache paths with decayed recency boosts."""
        self.cleanup_cache()
        now = datetime.now(UTC)
        ttl_seconds = timedelta(days=self._settings.short_term_ttl_days).total_seconds()
        candidates: dict[str, float] = {}
        for entry in self._session.scalars(
            select(ShortTermEntry).order_by(ShortTermEntry.path.asc())
        ):
            selected_at = datetime.fromisoformat(entry.last_selected_at)
            age_seconds = (now - selected_at).total_seconds()
            recency = min(
                1.0,
                max(0.0, 1.0 - age_seconds / ttl_seconds),
            )
            candidates[entry.path] = self._settings.short_term_max_boost * recency
        return candidates

    def recent_paths(self, min_hours_ago: float | None = None) -> dict[str, float]:
        """Query recent events and return path boost scores.

        Args:
            min_hours_ago: Only consider events within this many hours.
                          Defaults to short_term_window_hours.

        Returns:
            Dictionary mapping path to boost score.
        """
        hours = (
            min_hours_ago
            if min_hours_ago is not None
            else self._settings.short_term_window_hours
        )
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()

        stmt = select(ShortTermEvent).where(ShortTermEvent.expires_at >= cutoff)
        events = self._session.execute(stmt).scalars().all()

        path_counts: dict[str, int] = {}
        for event in events:
            paths = json.loads(event.selected_paths)
            for path in paths:
                path_counts[path] = path_counts.get(path, 0) + 1

        boost: dict[str, float] = {}
        for path, count in path_counts.items():
            boost[path] = self._settings.adaptive_boost * min(count, 3)
        return boost

    def cleanup_expired(self) -> int:
        """Delete expired short-term events.

        Returns the number of deleted events.
        """
        now = datetime.now(UTC).isoformat()
        stmt = delete(ShortTermEvent).where(ShortTermEvent.expires_at < now)
        result = self._session.execute(stmt)
        return _rowcount(result)

    def maybe_cleanup(self) -> int:
        """Conditionally run cleanup if enough time has passed.

        Returns the number of deleted events, or 0 if cleanup was skipped.
        """
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval:
            return 0
        self._last_cleanup = now
        return self.cleanup_expired()


def _rowcount(result: Any) -> int:
    """Return a typed SQLAlchemy row count."""
    return int(result.rowcount)
