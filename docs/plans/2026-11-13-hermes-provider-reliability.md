# Hermes provider reliability implementation plan

> **For agentic execution:** Execute tasks in order. Keep the existing uncommitted user changes untouched. Work only against Hermes v0.18.1 (`v2026.7.7`); do not alter or use a live Hermes profile.

**Goal:** Prove and harden HLM's query-only Hermes provider lifecycle against the pinned host, while ensuring provider failures and missing context never block a conversation.

**Design:** `docs/specs/2026-11-13-hermes-provider-reliability-design.md` (approved).

**Architecture:** Keep `HLMProvider` and its REST client minimal. Use existing profile-scoped secret handling, HLM limits, and Hermes' provider/manager lifecycle. Stub host dependencies in ordinary tests; make the optional pinned-host test use a temporary Hermes home and mocked HLM calls. No live health request, capture/write hook, schema change, or new dependency.

## Scope map

- `integrations/hermes/hlm/__init__.py`: availability, initialization, prefetch and shutdown behavior.
- `integrations/hermes/tests/test_provider.py`: hermetic lifecycle and recall behavior.
- `integrations/hermes/tests/test_hermes_host.py`: optional actual v0.18.1 loader and `MemoryManager` integration.
- `integrations/hermes/README.md`: only document behavior established by pinned source/tests.
- `integrations/hermes/hlm/client.py`: inspect existing query/result caps; change only if a failing test demonstrates rendered-context output is not bounded.

Do not modify unrelated root tests or the user's dirty edits in `backend/src/harbor_ledger_memory/migrate.py`, `tests/conftest.py`, `tests/test_cli.py`, or `tests/test_migration_startup.py`.

## Task 1 — Pin the host lifecycle contract in tests

**Files:** `integrations/hermes/tests/test_hermes_host.py`

1. Confirm the test imports the intended pinned Hermes API and is explicitly skipped when that optional host package is absent. Do not let ambient live profile paths/secrets be discovered.
2. Use a temporary `hermes_home`, temporary plugin directory, and monkeypatched/fake HLM client. Assert provider loading, initialization, query prefetch, and shutdown occur through the real loader/`MemoryManager`.
3. Add cases where initialization raises and where prefetch raises. Assert `initialize_all`/`prefetch_all` do not propagate failures to the caller and that failed or empty provider output is omitted.
4. Verify call order and `is_available()` semantics from tagged v0.18.1 source: discovery calls availability before initialization; availability must not issue network requests. Keep the host test independent of network and credentials.

**Focused check:** run `uv run pytest integrations/hermes/tests/test_hermes_host.py -q` both with the pinned host available (integration cases execute) and, when practical, without it (skip is clean).

## Task 2 — Cover provider failure and bounded recall with stubs

**Files:** `integrations/hermes/tests/test_provider.py`, and `integrations/hermes/hlm/__init__.py` only if a test exposes a defect.

1. Add/adjust tests for absent profile home/config, absent token, invalid URL/config, and ensure no token value appears in raised/logged errors.
2. Verify two isolated profile homes resolve their own settings and secret scope; a failure in one profile cannot reuse the other profile's client/token.
3. Verify the exact `query` string provided to `prefetch` is sent to HLM unchanged (no assumed project/workspace context); session ID must not widen retrieval scope.
4. Verify successful selected memories include source paths, empty/no-match results return `""`, and client exceptions propagate to the host manager rather than being converted into misleading memory text.
5. Exercise a large selection/excerpt and assert rendered output is bounded by the existing HLM selection/excerpt limits. Only if this fails, add the smallest provider-side cap and a corresponding test; do not add options or a new abstraction.
6. Assert `is_available()` is a local, network-free readiness/discovery hook. Do not make it a remote health check. Change its return behavior only if pinned host call-order tests and setup cases establish a concrete correctness defect.

**Focused check:** `uv run pytest integrations/hermes/tests/test_provider.py integrations/hermes/tests/test_client.py -q`.

## Task 3 — Document verified behavior and run integration checks

**Files:** `integrations/hermes/README.md`

1. Document the tested v0.18.1 lifecycle and failure behavior, including that prefetch is query-only and receives no project/workspace argument in this release.
2. Describe optional host-test requirements and invocation; state clearly that the ordinary suite uses stubs and does not alter a live Hermes profile.
3. Avoid claims about newer Hermes APIs or setup/disable commands unless verified against the pinned tag.
4. Run focused Hermes tests, lint/type checks configured for the integration, then the full repository test suite once after changes reconcile.

**Checks:**

```sh
uv run pytest integrations/hermes/tests -q
uv run ruff check integrations/hermes
uv run mypy integrations/hermes/hlm
uv run pytest -q
git diff --check
```

If a configured command is unavailable or optional pinned Hermes is absent, report that accurately; do not install Hermes into or inspect/use the live profile to make the test pass.

## Completion criteria

- Real pinned-host loader/manager tests show initialization and prefetch failures are non-fatal and no/empty context is omitted.
- Stub tests demonstrate profile-secret isolation, unchanged query pass-through, path attribution, empty/failure behavior, and bounded results.
- `is_available()` remains network-free; no project context is invented.
- README only claims verified v0.18.1 behavior.
- Full suite and relevant focused checks pass, or any unavailable optional-host coverage is clearly disclosed.
- No conversation capture, user profile mutation, authorization widening, or unrelated dirty-file changes.
