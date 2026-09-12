# Task 2 report: Surface live traversal queue activity

## Implementation

- Added an `onActivityChange` callback to `LiveTraversalController`, emitted only when the active trace count changes.
- Wired the controller into Tide Atlas so the connection indicator reads `LIVE • N active` while traversal traces are queued or glowing.
- Kept scan state independent in the existing Terrain chip; persisted activity timestamps are not used for live status.
- Preserved the existing cyan/amber traversal halos, cartographic layout, and the projection-time `controller.flush()` call.
- Added controller and Tide Atlas coverage for activity start, drain, and the zero-active connected state.

## Validation

- `npm test -- --run src/__tests__/TideAtlas.test.tsx` — passed (35 passed, 6 skipped).
- `npm test -- --run src/__tests__/liveTraversal.test.ts` — passed (9 passed).
- `npm run build` — passed; Vite emitted the existing large-chunk warning.
- `git diff --check` — passed.

The brief's literal `npm test -- --run web/src/__tests__/TideAtlas.test.tsx` path was also attempted from `web/`, but Vitest correctly reported no matching files; the equivalent `src/...` path above passed.

## Review note

Validation owner: reviewer. No unrelated files were changed.

## Review fix: expire unresolved traversal pending state

- Added a 2-second expiry timer for events deferred because their node or edge
  mapping is missing or ambiguous.
- Pending timers are cancelled when `flush()` retries an event or when the
  controller is disposed, so resolved playback and teardown remain unchanged.
- Added regression coverage proving missing and ambiguous events return the
  controller activity count to zero instead of leaving the live indicator stuck.
- Existing Tide Atlas coverage continues to exercise scan/status separation,
  stable Cytoscape instance state, and unchanged viewport/drag behavior.

### Review-fix validation

- `npm test -- --run src/__tests__/liveTraversal.test.ts src/__tests__/TideAtlas.test.tsx` — passed (45 passed, 6 skipped).
- `npm run build` — passed; Vite emitted the existing large-chunk warning.
- Validation owner: reviewer.

## Final review fix: bound unresolved pending state

- Added a global cap of `MAX_PENDING_EVENTS` (32) across unresolved traces.
- When the cap is exceeded, the oldest pending event is evicted and its
  sequence is retained as a watermark so stale or evicted events cannot be
  replayed later.
- Slow-projection buffering remains intact for entries within the cap.
- Added regression coverage for global oldest-first eviction and stale replay
  suppression.

### Final-fix validation

- `npm test -- --run src/__tests__/liveTraversal.test.ts src/__tests__/TideAtlas.test.tsx` — passed (48 passed, 6 skipped).
- `npm run build` — passed; Vite emitted the existing large-chunk warning.
- Validation owner: reviewer.

## Re-review fixes

- Superseded the earlier expiry-only behavior so slow projections can still
  recover their early events.
- Unresolved early events are retained in the pending buffer until a graph
  projection calls `flush()`; pending events are excluded from the displayed
  active count rather than expiring before a slow projection can resolve them.
- Write traversal edges now use a separate amber
  `traversal-forward-write` class and style; read edges retain the cyan class.
- Queue overflow now keeps the newest renderable event when the newest raw
  event cannot be mapped, while still bounding the queue.
- Added slow-projection, amber-edge, and unmappable-newest overflow tests.

### Re-review validation

- `npm test -- --run src/__tests__/liveTraversal.test.ts src/__tests__/TideAtlas.test.tsx` — passed (47 passed, 6 skipped).
- `npm run build` — passed; Vite emitted the existing large-chunk warning.
- Validation owner: reviewer.
