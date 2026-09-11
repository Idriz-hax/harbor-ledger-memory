# v1.3.0 Live Traversal Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream real-time, policy-filtered graph traversal activity to Tide Atlas and let MCP tokens approve only their own proposals when expressly permitted.

**Architecture:** Create a process-local, lossy event publisher for ordered traversal events. Inject it into server-owned query, scan, watcher, and mutation services, then provide a separately authenticated SSE reader that applies the viewer's read policy. The frontend subscribes directly and layers temporary directed Cytoscape styles over the existing graph view.

**Tech Stack:** Python, FastAPI, SQLAlchemy/Alembic, pytest, React, TypeScript, Cytoscape, Vitest.

## Global Constraints

- Traversal activity is live-only: never persist, recover, replay, or source it from `activity_events`.
- Publishing is non-blocking and must not alter read/write, scan, watcher, rollback, or indexing outcomes.
- An SSE event is emitted only if the viewer can read every path it contains.
- Do not alter Tide Atlas layout, selection, pagination, zoom, or drag semantics.
- Read nodes/edges glow cyan/blue; write nodes/edges glow amber; paths animate source-to-target.
- A token requires `approve_own_proposals=true` plus matching `creator_token_id` to use MCP approval.
- Existing rows default `approve_own_proposals` to false; admin does not imply this scope.
- Package and API version must be `1.3.0`.

---

### Task 1: Store approval scope and proposal creator token

**Files:**
- Create: `backend/migrations/versions/0016_live_traversal_and_token_approval.py`
- Modify: `backend/src/harbor_ledger_memory/catalog/models.py:364-453`
- Modify: `backend/src/harbor_ledger_memory/services/tokens.py:48-174,236-288`
- Test: `tests/test_tokens.py`

**Interfaces:** `TokenRecord.approve_own_proposals: bool`; `ApiToken.approve_own_proposals`; `MemoryWriteProposal.creator_token_id: int | None`; `TokenService.create(..., approve_own_proposals=False, ...)`.

- [ ] **Step 1: Write the failing token persistence test**

```python
def test_token_approval_scope_defaults_false_and_round_trips(tmp_path: Path) -> None:
    service = TokenService(f"sqlite:///{tmp_path / 'catalog.db'}")
    default, _ = service.create("default")
    approved, _ = service.create("approved", approve_own_proposals=True)
    assert default.approve_own_proposals is False
    assert approved.approve_own_proposals is True
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_tokens.py::test_token_approval_scope_defaults_false_and_round_trips -v`
Expected: FAIL because `TokenService.create` has no scope parameter.

- [ ] **Step 3: Add the forward migration and matching models**

```python
revision = "0016_live_traversal_and_token_approval"
down_revision = "0015_snapshot_handle_level"
op.add_column("api_tokens", sa.Column("approve_own_proposals", sa.Boolean(), nullable=False, server_default=sa.text("0")))
op.add_column("memory_write_proposals", sa.Column("creator_token_id", sa.Integer(), nullable=True))
```

Use the idempotent inspection pattern in `0012_token_rules.py`, add a `SET NULL` foreign key to `api_tokens.id`, and reverse each item in downgrade. Thread the boolean through `TokenRecord`, `_record_from_row`, `CreatedToken`, `as_dict`, creation, and activity payloads. Do not use `admin` as a fallback.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/test_tokens.py -v`
Expected: PASS.

Run: `git add backend/migrations/versions/0016_live_traversal_and_token_approval.py backend/src/harbor_ledger_memory/catalog/models.py backend/src/harbor_ledger_memory/services/tokens.py tests/test_tokens.py && git commit -m "feat: persist token proposal approval scope"`

### Task 2: Enforce the MCP approval boundary

**Files:**
- Modify: `backend/src/harbor_ledger_memory/api/app.py:380-415,802-987`
- Modify: `backend/src/harbor_ledger_memory/api/mcp_server.py:291-401`
- Modify: `backend/src/harbor_ledger_memory/services/vault_mutations.py:110-168`
- Test: `tests/test_mcp_tokens.py:318-390`
- Test: `tests/test_policy_writes.py`, `tests/test_token_auth.py`

**Interfaces:** `VaultMutationService.request(..., creator_token_id: int | None = None)`; `TokenCreateBody.approve_own_proposals`; safe token/write response fields.

- [ ] **Step 1: Write the failing security test**

```python
def test_mcp_approval_requires_opt_in_and_creator_match(tmp_path: Path) -> None:
    creator, _ = service.create("creator", rules=rules, approve_own_proposals=True)
    other, _ = service.create("other", rules=rules, approve_own_proposals=True)
    plain, _ = service.create("plain", rules=rules)
    proposal_id = _propose_as(server, creator, "AI/x.md")
    assert _approval_error(server, plain, proposal_id) == "token lacks approve-own-proposals permission"
    assert _approval_error(server, other, proposal_id) == "proposal was created by a different token"
    assert _approval_status(server, creator, proposal_id) == "applied"
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_mcp_tokens.py::test_mcp_approval_requires_opt_in_and_creator_match -v`
Expected: FAIL because any writable token currently approves proposals.

- [ ] **Step 3: Capture creator identity and check before service approval**

```python
def request(..., creator_token_id: int | None = None) -> MemoryWriteProposal:
    proposal = self._build_proposal(identity, content, access, operation)
    proposal.creator_token_id = creator_token_id

if not caller.approve_own_proposals:
    raise ToolError("token lacks approve-own-proposals permission")
if existing.creator_token_id != caller.id:
    raise ToolError("proposal was created by a different token")
```

Pass the active MCP token ID on requests. Retain `_mutation_affected_paths` and `policy.can_propose()` on approval, then call existing `service.approve(..., policy=policy)` for current-rule revalidation. UI-session/admin REST approval is unchanged. Include the boolean in `TokenCreateBody` and token summaries; never reveal hashes or plaintext.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/test_mcp_tokens.py tests/test_policy_writes.py tests/test_token_auth.py -v`
Expected: PASS.

Run: `git add backend/src/harbor_ledger_memory/api/app.py backend/src/harbor_ledger_memory/api/mcp_server.py backend/src/harbor_ledger_memory/services/vault_mutations.py tests/test_mcp_tokens.py tests/test_policy_writes.py tests/test_token_auth.py && git commit -m "feat: limit MCP approvals to proposal owners"`

### Task 3: Add a non-persistent traversal publisher

**Files:**
- Create: `backend/src/harbor_ledger_memory/services/live_traversal.py`
- Test: `tests/test_live_traversal.py`

**Interfaces:** `TraversalEvent(trace_id, sequence, mode, node_path, source_path=None, target_path=None, edge_type=None)`; `LiveTraversalPublisher.publish`; `subscribe`; `TraversalSubscription.close`.

- [ ] **Step 1: Write failing publisher isolation tests**

```python
def test_publisher_is_ordered_lossy_and_nonblocking() -> None:
    publisher = LiveTraversalPublisher(queue_size=1)
    subscriber = publisher.subscribe()
    publisher.publish(TraversalEvent("trace", 1, "read", "AI/INDEX.md"))
    publisher.publish(TraversalEvent("trace", 2, "read", "AI/child.md"))
    assert subscriber.get_nowait().sequence == 1
    assert subscriber.dropped == 1
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_live_traversal.py -v`
Expected: FAIL because the module is absent.

- [ ] **Step 3: Implement bounded in-memory fan-out**

```python
@dataclass(frozen=True)
class TraversalEvent:
    trace_id: str
    sequence: int
    mode: Literal["read", "write"]
    node_path: str
    source_path: str | None = None
    target_path: str | None = None
    edge_type: str | None = None

def publish(self, event: TraversalEvent) -> None:
    with self._lock:
        for subscriber in tuple(self._subscribers):
            subscriber.offer(event)  # Queue.put_nowait; drop overflow
```

Use `threading.RLock` plus per-subscriber `queue.Queue`. The module imports neither SQLAlchemy nor `ActivityService`; provide a `NullLiveTraversalPublisher` for direct construction/tests.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/test_live_traversal.py -v`
Expected: PASS.

Run: `git add backend/src/harbor_ledger_memory/services/live_traversal.py tests/test_live_traversal.py && git commit -m "feat: add ephemeral traversal publisher"`

### Task 4: Instrument the actual traversal paths

**Files:**
- Modify: `backend/src/harbor_ledger_memory/services/query.py:41-220`
- Modify: `backend/src/harbor_ledger_memory/services/scan.py:103-287`
- Modify: `backend/src/harbor_ledger_memory/watcher.py:48-194`
- Modify: `backend/src/harbor_ledger_memory/services/vault_mutations.py:110-376`
- Modify: `backend/src/harbor_ledger_memory/api/app.py:493-580,783-930`
- Test: `tests/test_query.py`, `tests/test_scan.py`, `tests/test_watcher.py`, `tests/test_vault_mutations.py`

**Interfaces:** one app-owned publisher; each public request/job gets a UUID trace ID and monotonically ascending sequence.

- [ ] **Step 1: Write the failing ordered query test**

```python
def test_query_publishes_seed_then_activation_edges(query_service, publisher) -> None:
    query_service.query(QueryRequest(query="harbor"))
    events = publisher.drain()
    assert [(e.node_path, e.source_path, e.target_path) for e in events] == [
        ("AI/INDEX.md", None, None),
        ("AI/child.md", "AI/INDEX.md", "AI/child.md"),
    ]
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_query.py::test_query_publishes_seed_then_activation_edges -v`
Expected: FAIL because `QueryService` has no publisher.

- [ ] **Step 3: Inject and publish after policy filtering**

```python
for sequence, node in enumerate(sorted(activated, key=lambda item: (item.hop, item.path)), 1):
    self._live_traversal.publish(TraversalEvent(
        trace_id, sequence, "read", node.path,
        node.edge_source, node.edge_target, node.edge_type))
```

Inject the app-owned publisher into query, scan, watcher, and mutation services—no globals. Query events follow existing filtered activation ordering. Mutation events cover request, applying, rejection, terminal write state, and post-write scan. Watcher and scan jobs create unique traces. Missing/unprojectable targets emit nothing.

- [ ] **Step 4: Add non-interference coverage**

```python
def test_full_subscriber_queue_does_not_change_approved_write(publisher, proposal) -> None:
    publisher.subscribe(queue_size=1)
    assert service.approve(proposal.id, policy=policy).status == "applied"
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/test_query.py tests/test_scan.py tests/test_watcher.py tests/test_vault_mutations.py -v`
Expected: PASS.

Run: `git add backend/src/harbor_ledger_memory/services/query.py backend/src/harbor_ledger_memory/services/scan.py backend/src/harbor_ledger_memory/watcher.py backend/src/harbor_ledger_memory/services/vault_mutations.py backend/src/harbor_ledger_memory/api/app.py tests/test_query.py tests/test_scan.py tests/test_watcher.py tests/test_vault_mutations.py && git commit -m "feat: publish live traversal activity"`

### Task 5: Expose filtered live activity with SSE

**Files:**
- Modify: `backend/src/harbor_ledger_memory/api/app.py:989-1059`
- Test: `tests/test_live_traversal_api.py`

**Interfaces:** `GET /api/v1/graph/traversal/stream`, SSE event `traversal`, authenticated `AuthContext`.

- [ ] **Step 1: Write failing route tests**

```python
def test_traversal_stream_requires_auth_and_hides_denied_paths(client, publisher) -> None:
    assert client.get("/api/v1/graph/traversal/stream").status_code == 401
    publisher.publish(TraversalEvent("t", 1, "read", "Secret/hidden.md"))
    publisher.publish(TraversalEvent("t", 2, "read", "AI/visible.md"))
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_live_traversal_api.py -v`
Expected: FAIL with 404.

- [ ] **Step 3: Implement an authenticated no-replay generator**

```python
@application.get("/api/v1/graph/traversal/stream")
def traversal_stream(auth: AuthContext = Depends(authenticated)) -> StreamingResponse:
    subscription = live_traversal.subscribe()
    def iterator() -> Iterator[str]:
        try:
            while True:
                event = subscription.get(timeout=15)
                if event and _event_visible_to_policy(event, auth.policy):
                    yield f"event: traversal\ndata: {json.dumps(event.as_dict())}\n\n"
                elif event is None:
                    yield ": keepalive\n\n"
        finally:
            subscription.close()
    return StreamingResponse(iterator(), media_type="text/event-stream")
```

Require `can_read` for node, source, and target when present; otherwise drop the event. Add `Cache-Control: no-cache` and `X-Accel-Buffering: no`; do not accept query tokens, cursors, or `Last-Event-ID`.

- [ ] **Step 4: Add disconnect and no-persistence checks, then commit**

Run: `uv run pytest tests/test_live_traversal_api.py tests/test_graph_api.py -v`
Expected: PASS; tests prove disconnect removes subscription, overflow is lossy, and no `ActivityEvent` is created.

Run: `git add backend/src/harbor_ledger_memory/api/app.py tests/test_live_traversal_api.py tests/test_graph_api.py && git commit -m "feat: stream policy-filtered graph traversals"`

### Task 6: Render concurrent directed glows and token control

**Files:**
- Create: `web/src/liveTraversal.ts`
- Modify: `web/src/TideAtlas.tsx:1-145,243-546,623-680`
- Modify: `web/src/main.tsx:83-180,1130-1510`
- Test: `web/src/__tests__/liveTraversal.test.ts`
- Test: `web/src/__tests__/TideAtlas.test.tsx`, `web/src/__tests__/token-auth.test.tsx`

**Interfaces:** input event `{ trace_id, sequence, mode, node_path, source_path?, target_path?, edge_type? }`; `LiveTraversalController.apply(event)` and `dispose()`.

- [ ] **Step 1: Write failing UI/controller tests**

```ts
it('keeps concurrent read and write traces independent', () => {
  controller.apply({ trace_id: 'a', sequence: 1, mode: 'read', node_path: 'one' })
  controller.apply({ trace_id: 'b', sequence: 1, mode: 'write', node_path: 'two', source_path: 'one', target_path: 'two' })
  expect(cytoscapeMock.classesFor('one')).toContain('traversal-read')
  expect(cytoscapeMock.classesFor('two')).toContain('traversal-write')
  expect(cytoscapeMock.classesFor('lane')).toContain('traversal-forward')
})
```

Add a token-form test that the `Approve own proposals` checkbox is unchecked by default and sends `approve_own_proposals: true` only after selection.

- [ ] **Step 2: Run them**

Run: `npm test -- --run web/src/__tests__/liveTraversal.test.ts web/src/__tests__/TideAtlas.test.tsx web/src/__tests__/token-auth.test.tsx` from `web/`
Expected: FAIL because controller, stream, and checkbox do not exist.

- [ ] **Step 3: Build a trace-owned visual layer**

```ts
apply(event: LiveTraversalEvent) {
  const node = this.core.getElementById(event.node_path)
  node.addClass(event.mode === 'read' ? 'traversal-read' : 'traversal-write')
  this.matchDirectedEdge(event.source_path, event.target_path, event.edge_type)
    ?.addClass('traversal-forward')
  this.scheduleCleanup(event.trace_id, node)
}
```

Maintain timers per trace so a fade cannot clear another trace. Missing bounded-view nodes/edges are no-ops. Add cyan `traversal-read`, amber `traversal-write`, and arrowed/dashed `traversal-forward` Cytoscape styles; reduced-motion retains color but skips dash animation.

- [ ] **Step 4: Subscribe in Tide Atlas and retain history stream separately**

```tsx
const stream = new EventSource('/api/v1/graph/traversal/stream')
stream.onopen = () => setTraversalConnected(true)
stream.onerror = () => setTraversalConnected(false)
stream.addEventListener('traversal', event => controllerRef.current?.apply(JSON.parse((event as MessageEvent).data)))
```

Create/dispose the controller with Cytoscape. Keep `/api/v1/activity/stream` for the activity radar only. Drive Tide Atlas `LIVE`/`RECONNECTING` from the traversal stream, not scan status. Add the new boolean to `TokenSummary` and the create form; no edit route is needed because revoke/recreate preserves least privilege.

- [ ] **Step 5: Verify and commit**

Run: `npm test -- --run web/src/__tests__/liveTraversal.test.ts web/src/__tests__/TideAtlas.test.tsx web/src/__tests__/token-auth.test.tsx` from `web/`
Expected: PASS.

Run: `git add web/src/liveTraversal.ts web/src/TideAtlas.tsx web/src/main.tsx web/src/__tests__/liveTraversal.test.ts web/src/__tests__/TideAtlas.test.tsx web/src/__tests__/token-auth.test.tsx && git commit -m "feat: visualize live graph traversals"`

### Task 7: Release v1.3.0 and run system verification

**Files:**
- Modify: `pyproject.toml:5-8`
- Modify: `backend/src/harbor_ledger_memory/api/app.py:573-578`
- Modify: `CHANGELOG.md`, `README.md`, `scripts/verify_release_artifacts.py`

- [ ] **Step 1: Write a failing release version test**

```python
def test_application_version_matches_release() -> None:
    assert create_app(settings).version == "1.3.0"
```

Run: `uv run pytest tests/test_token_auth.py -v`
Expected: FAIL while FastAPI advertises `1.2.1`.

- [ ] **Step 2: Update version and documentation**

```toml
[project]
version = "1.3.0"
```

Set FastAPI version to `1.3.0`. Document telemetry as authenticated, lossy, policy-filtered, live-only, and non-replayable; document own-proposal approval. Add changelog entries. Keep frontend package version independent unless release scripts explicitly require lockstep.

- [ ] **Step 3: Run all verification**

Run: `uv run pytest`
Expected: PASS.

Run: `uv run ruff check backend/src tests && uv run pyright backend/src`
Expected: PASS.

Run: `npm test -- --run && npm run build` from `web/`
Expected: PASS.

Run: `uv run python scripts/verify_release_artifacts.py`
Expected: PASS.

- [ ] **Step 4: Commit**

Run: `git add pyproject.toml backend/src/harbor_ledger_memory/api/app.py CHANGELOG.md README.md scripts/verify_release_artifacts.py tests && git commit -m "feat: release harbor ledger memory 1.3.0"`

## Plan self-review

- **Spec coverage:** Tasks 3-5 implement ephemeral filtered SSE; Tasks 4 and 6 implement actual ordered traversal, directional glow, concurrency, cleanup, and reconnect visibility; Tasks 1-2 and 6 implement token opt-in/ownership; Task 7 covers release/docs/verification.
- **Completeness scan:** no deferred requirement remains.
- **Type consistency:** `approve_own_proposals`, `creator_token_id`, `TraversalEvent`, and `/api/v1/graph/traversal/stream` are consistent across all tasks.
