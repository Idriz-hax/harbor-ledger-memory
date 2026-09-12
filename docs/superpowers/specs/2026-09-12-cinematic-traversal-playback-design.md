# Cinematic Traversal Playback Design

## Goal

Make live graph activity legible as an ambient background display. Events remain real-time and live-only, but each operation advances visibly through the graph rather than flashing all hops at once.

## Playback

- SSE events enter a client-side per-trace FIFO immediately; no event history is stored or replayed after reconnect.
- Each trace advances one renderable hop every 350ms.
- Read/write node halos remain visible for 1.2 seconds, overlapping the following hop to form a moving current.
- Directed edges animate source-to-target. Node-only events still render when no resolved edge exists.
- Active traces use round-robin scheduling, preventing a large query from starving scans or writes.
- Each trace has a bounded queue. On overflow, unresolved intermediate events collapse into a single pulse at the newest renderable node; the UI never grows an unbounded backlog.
- Reduced-motion mode preserves ordered color transitions but disables moving edge dashes.

## UI

Tide Atlas exposes a compact `LIVE • N active` status derived from current queued/visible traces. This is separate from the persisted historical activity feed, whose last-activity timestamp must not be presented as live traversal state.

Read activity is cyan/blue; write activity is amber. Existing graph layout, zoom, selection, pagination, and drag interactions remain unchanged.

## Reliability and tests

- Events arriving before a graph view resolves remain buffered until their node/edge can be mapped or the bounded queue discards them.
- Missing nodes and ambiguous aggregate edges are no-ops, not arbitrary visual matches.
- Test ordering at 350ms, per-trace fairness, queue overflow collapse, 1.2s cleanup, reduced-motion behavior, and status counts.
- Add Tide Atlas integration coverage for a live burst arriving before and after a graph view loads.
