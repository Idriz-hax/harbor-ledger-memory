"""Ephemeral, bounded fan-out for live vault traversal events."""

from dataclasses import dataclass
from queue import Full, Queue
from threading import RLock
from typing import Callable, Literal


@dataclass(frozen=True)
class TraversalEvent:
    """A single node or edge observed during a traversal."""

    trace_id: str
    sequence: int
    mode: Literal["read", "write"]
    node_path: str
    source_path: str | None = None
    target_path: str | None = None
    edge_type: str | None = None


class TraversalSubscription:
    """A consumer's bounded event queue."""

    def __init__(
        self,
        queue_size: int,
        on_close: Callable[["TraversalSubscription"], None] | None = None,
    ) -> None:
        self._queue: Queue[TraversalEvent] = Queue(maxsize=queue_size)
        self._dropped = 0
        self._closed = False
        self._on_close = on_close
        self._lock = RLock()

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def get_nowait(self) -> TraversalEvent:
        return self._queue.get_nowait()

    def get(self, block: bool = True, timeout: float | None = None) -> TraversalEvent:
        return self._queue.get(block=block, timeout=timeout)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._on_close is not None:
            self._on_close(self)

    def offer(self, event: TraversalEvent) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._queue.put_nowait(event)
            except Full:
                self._dropped += 1


class LiveTraversalPublisher:
    """Thread-safe, in-memory, lossy publisher for traversal events."""

    def __init__(self, queue_size: int = 100) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._queue_size = queue_size
        self._lock = RLock()
        self._subscribers: set[TraversalSubscription] = set()

    def publish(self, event: TraversalEvent) -> None:
        with self._lock:
            for subscriber in tuple(self._subscribers):
                subscriber.offer(event)

    def subscribe(self) -> TraversalSubscription:
        with self._lock:
            subscription = TraversalSubscription(self._queue_size, self._remove)
            self._subscribers.add(subscription)
            return subscription

    def _remove(self, subscription: TraversalSubscription) -> None:
        with self._lock:
            self._subscribers.discard(subscription)


class NullLiveTraversalPublisher:
    """Publisher-shaped no-op for callers that do not need live traversal."""

    def publish(self, event: TraversalEvent) -> None:
        del event

    def subscribe(self) -> TraversalSubscription:
        return TraversalSubscription(1)


__all__ = [
    "LiveTraversalPublisher",
    "NullLiveTraversalPublisher",
    "TraversalEvent",
    "TraversalSubscription",
]
