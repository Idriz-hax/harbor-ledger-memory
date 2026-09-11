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
