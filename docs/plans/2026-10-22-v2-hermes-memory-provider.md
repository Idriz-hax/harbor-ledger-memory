# Hermes Shared Memory Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make HLM selectable as Hermes' shared long-term memory provider while preserving HLM policy and approval controls.

**Architecture:** Add a Hermes profile-directory memory provider at `$HERMES_HOME/plugins/hlm/`, verified against Hermes v0.18.1 (`v2026.7.7`), which provides both the released `MemoryProvider` and profile-scoped secret APIs. It calls the HLM REST service and never imports HLM internals or reads vault files. `hlm_get_note` is blocked until the note-content API plan passes; proposal/list calls use bounded agent-write routes and are blocked until `docs/plans/2026-10-22-v2-bounded-agent-writes.md` passes. Do not rely on the newer `hermes_agent.memory_providers` entry point, which is not present in the pinned release.

**Tech Stack:** Python 3.12, Hermes v0.18.1 `MemoryProvider`, stdlib `urllib`, pytest.

---

## File map

- Create `integrations/hermes/hlm/__init__.py` and `client.py`; copy this directory to `$HERMES_HOME/plugins/hlm/` for native installation.
- Create `integrations/hermes/tests/test_client.py` and `test_provider.py`; `README.md` documents install, config, host versions, local/remote modes, and approval fallback.

## Task 1: Fix the host contract and write failing tests

- [ ] Pin Hermes v0.18.1 / tag `v2026.7.7`; verify the tagged `agent/memory_provider.py`, `agent/memory_manager.py`, `agent/secret_scope.py`, and `plugins/memory/__init__.py` signatures/discovery behavior before implementation. The supported install is a user directory provider under `$HERMES_HOME/plugins/<name>/` containing `__init__.py`.
- [ ] Write client tests using an injected transport or mocked stdlib `urlopen`: bearer auth on every request; query maps to `POST /api/v1/queries`; full note get maps to `GET /api/v1/notes/content`; writes map to `POST /api/v1/agent/writes`; pending review maps to `GET /api/v1/agent/writes/pending`; approval resolution, if exposed through a confirmed MCP gate, uses no unbounded provider route; API errors raise/report failures rather than return empty success.
- [ ] Test provider with the pinned Hermes `MemoryManager`: `is_available()` makes no network request; `initialize()` uses `get_secret()` and profile-scoped `hermes_home`; `prefetch()` formats HLM-selected context; registered `hlm_query`, `hlm_get_note`, `hlm_propose_write`, `hlm_propose_folder`, and `hlm_pending_proposals` tools return JSON strings.
- [ ] Run the fast stub suite with `uv run pytest integrations/hermes/tests -q` and the pinned host suite with `uv run --with hermes-agent==0.18.1 pytest integrations/hermes/tests -q`; confirm new behavior tests fail before implementation.

## Task 2: Implement the provider package

- [ ] Implement `HLMClient` using stdlib `urllib.request`, configured local/remote service URL and Hermes `get_secret("HLM_TOKEN")`; set a finite timeout, require HTTPS remotely and loopback-only HTTP, refuse redirects, cap response bytes before JSON parsing, and never log the token.
- [ ] Before implementing or merging `hlm_get_note`, require `docs/plans/2026-10-22-v2-note-content-api.md` and its security tests to pass; before proposal/list calls, require `docs/plans/2026-10-22-v2-bounded-agent-writes.md` and its security tests to pass. Keep mocks aligned to exact routes, summary shapes, and 401/404/413/422 errors.
- [ ] Implement `HLMProvider(MemoryProvider)` with `name`, local-only `is_available`, `initialize`, schemas, handlers for `hlm_query`, `hlm_get_note`, `hlm_propose_write`, `hlm_propose_folder`, and `hlm_pending_proposals`, plus query-only `prefetch`; do not ingest each `sync_turn` automatically.
- [ ] Bind each provider instance to the `hermes_home` passed to `initialize`; do not use global mutable client/profile state.
- [ ] Cap provider proposal content at 64 KiB UTF-8 before serialization and use only the bounded agent write/list routes. Do not expose raw vault writes or provider-level approve/reject tools. Pending results include a web review link/instruction. Optional legacy MCP chat approval must be configured as `trust: untrusted`, use the same creator token with `approve_own_proposals`, and be documented as outside the new bounded response contract.
- [ ] Export the discoverable `HLMProvider(MemoryProvider)` class from the directory's `__init__.py`; do not register a Python package entry point.

## Task 3: Verify install and commit

- [ ] Run unit tests and the v0.18.1 Hermes `MemoryManager` lifecycle tests using both the stub suite and the `--with hermes-agent==0.18.1` suite (initialize, directory discovery, query/prefetch, tool dispatch, shutdown).
- [ ] Run `uv run ruff check integrations/hermes` and `uv run pyright integrations/hermes`; test both local service URL and authenticated remote URL against the HLM API test server.
- [ ] Verify failed HLM reads are visible to the agent; ensure no silent fallback to Hermes' separate long-term store. Verify proposal stays pending until HLM UI or confirmed host action resolves it.
- [ ] Document exact profile-directory install/config commands, `HLM_TOKEN` setup, same-token MCP trust/approval setup, local/remote modes, supported Hermes v0.18.1 release and web-approval fallback in `integrations/hermes/README.md`. Commit as `feat: add Hermes shared memory provider`.
