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
