"""Durable, bounded operational activity telemetry."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import AsyncGenerator, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import ActivityEvent

ACTIVITY_RETENTION_DAYS = 30
DEFAULT_ACTIVITY_LIMIT = 100
MAX_ACTIVITY_LIMIT = 500


@dataclass(frozen=True)
class ActivityMessage:
    """Detached activity data safe to hand to an HTTP response or stream."""

    id: int
    event_type: str
    created_at: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation used by REST and SSE."""
        return {
            "id": self.id,
            "event_type": self.event_type,
            "created_at": self.created_at,
            "payload": self.payload,
        }


class ActivityService:
    """Persist activity events and fan out newly committed events to streams."""

    def __init__(
        self,
        source: Session | Engine | str,
        *,
        retention_days: int = ACTIVITY_RETENTION_DAYS,
    ) -> None:
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")

        self.retention_days = retention_days
        self._lock = threading.RLock()
        self._subscribers: set[queue.Queue[ActivityMessage]] = set()
        self._owns_engine = isinstance(source, str)
        self._owns_session = isinstance(source, (str, Engine))
        if isinstance(source, str):
            self._engine = create_database(source)
            self._session = CatalogSession(bind=self._engine)
        elif isinstance(source, Engine):
            self._engine = source
            self._session = CatalogSession(bind=source)
        else:
            self._engine = None
            self._session = source

    @property
    def session(self) -> Session:
        """The active database session this service writes through."""
        return self._session

    def record(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        occurred_at: datetime | str | None = None,
        commit: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> ActivityMessage:
        """Store one event, pruning entries older than the retention window.

        ``metadata`` carries the structured correlation fields
        (``operation_id``/``run_id``/``agent_id``/``parent_id``/``graph_refs``)
        that are persisted into the dedicated ``ActivityEvent`` columns.  Any
        of these fields may also be carried inside ``payload``; an explicit
        ``metadata`` value takes priority.  ``payload`` itself is stored
        verbatim, so the historical payload/SSE envelope is unchanged.
        """
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        created_at = _timestamp(occurred_at)
        with self._lock:
            self._cleanup_locked()
            row = _build_event_row(event_type, created_at, payload, metadata)
            self._session.add(row)
            self._session.flush()
            message = _message_from_row(row)
            if commit:
                self._session.commit()
            self._publish(message)
            return message

    def stage(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        occurred_at: datetime | str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ActivityMessage:
        """Stage one event inside the caller's active transaction.

        The event row is added and flushed so it participates in the
        caller's current transaction, but it is neither committed nor
        published.  The caller must commit the transaction and then call
        :meth:`publish` for the event to become durable and visible to
        live subscribers; rolling the transaction back discards the
        staged event along with the state change it audits.

        ``metadata`` carries the structured correlation fields persisted into
        the dedicated ``ActivityEvent`` columns, following the same
        payload/metadata extraction as :meth:`record`; the payload is stored
        verbatim so the historical envelope is unchanged.
        """
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        created_at = _timestamp(occurred_at)
        with self._lock:
            row = _build_event_row(event_type, created_at, payload, metadata)
            self._session.add(row)
            self._session.flush()
            return _message_from_row(row)

    def publish(self, message: ActivityMessage) -> None:
        """Fan out an already-committed event to live subscribers."""
        with self._lock:
            self._publish(message)

    def history(
        self,
        *,
        limit: int = DEFAULT_ACTIVITY_LIMIT,
        after_id: int | None = None,
    ) -> list[ActivityMessage]:
        """Return recent events in chronological order after an optional ID."""
        if not 1 <= limit <= MAX_ACTIVITY_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_ACTIVITY_LIMIT}")
        with self._lock:
            self._cleanup_locked()
            statement = select(ActivityEvent).order_by(ActivityEvent.id.desc())
            if after_id is not None:
                statement = statement.where(ActivityEvent.id > after_id)
            rows = list(self._session.scalars(statement.limit(limit)))
            if self._session.in_transaction():
                self._session.commit()
        return [_message_from_row(row) for row in reversed(rows)]

    def cleanup(self, *, now: datetime | str | None = None) -> int:
        """Delete and commit events older than the configured retention window."""
        reference = _timestamp(now)
        cutoff = _cutoff(reference, self.retention_days)
        with self._lock:
            result = self._session.execute(
                delete(ActivityEvent).where(ActivityEvent.created_at < cutoff)
            )
            self._session.commit()
        return int(cast(Any, result).rowcount or 0)

    def subscribe(self) -> queue.Queue[ActivityMessage]:
        """Subscribe to events committed after this call."""
        subscriber: queue.Queue[ActivityMessage] = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[ActivityMessage]) -> None:
        """Stop delivering events to one stream subscriber."""
        with self._lock:
            self._subscribers.discard(subscriber)

    async def stream(
        self,
        *,
        after_id: int | None = None,
    ) -> AsyncGenerator[str, None]:
        """Yield an SSE history replay followed by live activity messages."""
        events = self.history(after_id=after_id)
        subscriber = self.subscribe()
        last_id = after_id or 0
        try:
            for event in events:
                last_id = max(last_id, event.id)
                yield self.sse_message(event)

            while True:
                try:
                    event = await asyncio.to_thread(subscriber.get, True, 15.0)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if event.id <= last_id:
                    continue
                last_id = event.id
                yield self.sse_message(event)
        finally:
            self.unsubscribe(subscriber)

    @staticmethod
    def sse_message(event: ActivityMessage) -> str:
        """Serialize one activity event as a standards-compatible SSE frame."""
        data = json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True)
        return f"id: {event.id}\nevent: activity\ndata: {data}\n\n"

    def close(self) -> None:
        """Release resources when the service owns its database session."""
        with self._lock:
            self._subscribers.clear()
            if self._owns_session:
                self._session.close()
            if self._owns_engine and self._engine is not None:
                self._engine.dispose()

    def _cleanup_locked(self) -> int:
        cutoff = _cutoff(_timestamp(None), self.retention_days)
        result = self._session.execute(
            delete(ActivityEvent).where(ActivityEvent.created_at < cutoff)
        )
        return int(cast(Any, result).rowcount or 0)

    def _publish(self, message: ActivityMessage) -> None:
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                subscriber.put_nowait(message)


def graph_refs(paths: Iterable[str | None]) -> list[str]:
    """Return a deterministic, de-duplicated list of graph node paths.

    Activity payloads use this to expose the vault graph nodes an operation
    touched — the notes a scan added, changed, or deleted, or the notes a
    query selected/activated — in a stable, order-independent form.  Input
    order is ignored, duplicates collapse to a single entry, and empty or
    missing paths are dropped, so the same touched set always serializes to
    the same list regardless of how it was accumulated.
    """
    return sorted({path for path in paths if path})


def _extract_graph_metadata(
    payload: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Extract structured correlation fields for the activity columns.

    Each of ``operation_id``, ``run_id``, ``agent_id``, and ``parent_id`` is
    read from an explicit ``metadata`` mapping first, then from the event
    ``payload`` so callers that already carry these fields in their payload
    (the historical scan/query convention) populate the columns without any
    signature change.  ``graph_refs`` — from either source — serializes into
    ``graph_refs_json``.  A field present in neither source stays ``None``
    so legacy events keep their columns NULL.
    """
    source: dict[str, Any] = dict(payload or {})
    if metadata is not None:
        source.update(metadata)
    values: dict[str, Any] = {
        field: source.get(field)
        for field in ("operation_id", "run_id", "agent_id", "parent_id")
    }
    graph_refs = source.get("graph_refs")
    values["graph_refs_json"] = (
        json.dumps(graph_refs, ensure_ascii=False, sort_keys=True, default=str)
        if graph_refs is not None
        else None
    )
    return values


def _build_event_row(
    event_type: str,
    created_at: str,
    payload: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
) -> ActivityEvent:
    """Build one ``ActivityEvent`` with its payload and correlation metadata."""
    values = dict(payload or {})
    return ActivityEvent(
        event_type=event_type,
        created_at=created_at,
        payload_json=json.dumps(
            values, ensure_ascii=False, sort_keys=True, default=str
        ),
        **_extract_graph_metadata(values, metadata),
    )


def _message_from_row(row: ActivityEvent) -> ActivityMessage:
    payload = json.loads(row.payload_json)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    payload = cast(dict[str, Any], payload)
    return ActivityMessage(
        id=row.id,
        event_type=row.event_type,
        created_at=row.created_at,
        payload=payload,
    )


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    return value if isinstance(value, str) else value.astimezone(UTC).isoformat()


def _cutoff(timestamp: str, retention_days: int) -> str:
    try:
        reference = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValueError("activity timestamps must be ISO-8601 values") from exc
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return (reference - timedelta(days=retention_days)).astimezone(UTC).isoformat()


__all__ = [
    "ACTIVITY_RETENTION_DAYS",
    "ActivityMessage",
    "ActivityService",
    "DEFAULT_ACTIVITY_LIMIT",
    "MAX_ACTIVITY_LIMIT",
    "graph_refs",
]
