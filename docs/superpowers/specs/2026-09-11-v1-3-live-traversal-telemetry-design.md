# v1.3.0 Live Traversal Telemetry Design

## Goal

Make Tide Atlas show current server activity as it happens: the catalog indexes, nodes, groups, and directed links traversed by reads and writes glow in traversal order. The display is live-only, never a persisted activity log or a replay. Harden MCP proposal approval so ordinary tokens cannot grant themselves approval authority.

## Behavior

### Live graph activity

Every authenticated server operation emits short-lived traversal telemetry after its existing policy checks. This includes MCP and REST requests, scans, watcher work, proposals, approvals, rejections, direct auto-writes, filesystem mutation, and follow-up indexing.

Each operation has a trace ID and an operation mode (`read` or `write`). As it navigates the graph, it emits ordered events for the node or group reached and the directed edge used to reach the next target. Events identify only graph-safe node/group identifiers and omit any target not visible under the graph viewer's effective token policy.

An authenticated server-sent events endpoint streams events to Tide Atlas. The endpoint has bounded per-client buffering and treats slow or disconnected clients as telemetry loss only: reads, writes, scans, rollback, and indexing must never wait for a browser. Events are not saved, are not included in graph snapshots, and are not sent to clients that connect after they occurred.

Tide Atlas overlays live activity on the existing policy-filtered graph projection without changing node layout, selection, pagination, or zoom rules:

- read activity uses a cool cyan/blue glow;
- write activity uses a warm amber glow;
- active directed links animate from source to destination;
- nodes, groups, and paths fade after a short configured display interval;
- simultaneous operations remain independently visible; and
- the UI shows a small live/disconnected status and reconnects automatically without replaying missed events.

### Token-scoped proposal approval

Token creation and editing expose an unchecked **Approve own proposals** control. Tokens without this explicit permission cannot call `approve_proposal` through MCP.

When permission is enabled, the MCP token may approve only a pending proposal whose recorded creator is that same token. Approval also re-evaluates the token's current folder policy for every affected path, preserving the existing version-safe write, scan-after-write, and rollback protections. A token cannot approve a proposal created by another token, even if its folder policy would otherwise allow the affected paths.

Existing administrative approval surfaces keep their current administrator boundary; they do not confer MCP approval permission to ordinary tokens.

## Architecture

- Add a small in-process live-activity publisher with an explicit event model, trace context, bounded subscribers, and a no-op-safe publish path.
- Instrument the existing read, traversal, write, proposal lifecycle, scan, watcher, and graph projection seams. Instrumentation observes successful, policy-permitted steps and must not become part of the data or mutation control flow.
- Add a policy-aware SSE route beside the existing authenticated graph routes. Filter emitted targets using the same graph/view access rules before serialization.
- Add a Tide Atlas activity-layer controller that subscribes to SSE, maps events onto Cytoscape node/edge classes, owns timers/fade cleanup, and exposes connection state.
- Extend the stored token scope model, token-management API/UI, MCP scope guard, and proposal metadata with a token creator identity needed for ownership enforcement.
- Update v1.3.0 release metadata and documentation consistently across backend packaging, release notes, artifact checks, and public usage guidance.

## Error Handling

- An SSE authentication or authorization failure returns the existing API error shape and creates no stream.
- Stream disconnects, queue overflow, malformed events, and client rendering errors are isolated from the request that generated activity.
- Missing, hidden, or non-projectable traversal targets are not emitted to the viewer.
- A disabled approval scope or ownership mismatch returns an authorization failure before a proposal is mutated.
- Changed or revoked token folder rules are evaluated again at approval time, as they are for existing proposal safety.

## Verification

Test first and establish:

- ordered read and write telemetry emits the expected node/group and directed-edge sequence;
- all supported server activity surfaces are instrumented without changing their functional result;
- SSE requires authentication, filters hidden targets, handles disconnects/overflow, and never persists or replays events;
- Tide Atlas correctly applies read/write glows, directional animation, fade cleanup, concurrent traces, and reconnect state;
- a token cannot approve without the opt-in scope, cannot approve another token's proposal, and can approve only its own permitted proposal;
- approval still revalidates folder policy and preserves existing mutation rollback behavior; and
- existing graph, token, MCP, access-policy, mutation, backend, web-build, and release-artifact checks pass with version 1.3.0 metadata.
