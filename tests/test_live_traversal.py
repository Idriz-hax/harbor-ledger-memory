from queue import Empty

import pytest

from harbor_ledger_memory.services.live_traversal import (
    LiveTraversalPublisher,
    TraversalEvent,
)


def test_publisher_is_ordered_lossy_and_nonblocking() -> None:
    publisher = LiveTraversalPublisher(queue_size=1)
    subscriber = publisher.subscribe()
    publisher.publish(TraversalEvent("trace", 1, "read", "AI/INDEX.md"))
    publisher.publish(TraversalEvent("trace", 2, "read", "AI/child.md"))
    assert subscriber.get_nowait().sequence == 1
    assert subscriber.dropped == 1


def test_subscribers_have_independent_queues_and_overflow() -> None:
    publisher = LiveTraversalPublisher(queue_size=1)
    slow = publisher.subscribe()
    active = publisher.subscribe()

    publisher.publish(TraversalEvent("trace", 1, "read", "AI/INDEX.md"))
    assert active.get_nowait().sequence == 1
    publisher.publish(TraversalEvent("trace", 2, "read", "AI/child.md"))

    assert slow.get_nowait().sequence == 1
    assert slow.dropped == 1
    assert active.get_nowait().sequence == 2
    assert active.dropped == 0


def test_close_is_idempotent_removes_subscription_and_stops_delivery() -> None:
    publisher = LiveTraversalPublisher(queue_size=1)
    closed = publisher.subscribe()
    remaining = publisher.subscribe()

    closed.close()
    closed.close()
    publisher.publish(TraversalEvent("trace", 1, "write", "AI/INDEX.md"))

    with pytest.raises(Empty):
        closed.get_nowait()
    assert remaining.get_nowait().sequence == 1
