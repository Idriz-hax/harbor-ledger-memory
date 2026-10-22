# Hermes Shared Memory Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make HLM selectable as Hermes' shared long-term memory provider while preserving HLM policy and approval controls.

**Architecture:** Add a separately installable Python Hermes provider that calls the HLM REST service; it never imports HLM internals or reads vault files. It uses the shared note-content API plan, query and proposal endpoints, and Hermes' `MemoryProvider` lifecycle. The HLM API plan is a prerequisite; this provider can be developed independently from OpenClaw after that contract is fixed.

**Tech Stack:** Python 3.12, Hermes `MemoryProvider`, HTTPX, pytest.

---

## File map

- Create `integrations/hermes/pyproject.toml`: independent plugin package and `hermes_agent.memory_providers` entry point.
- Create `integrations/hermes/src/hlm_hermes/provider.py`: provider hooks/tools; `client.py`: REST/auth/error mapping.
- Create `integrations/hermes/tests/test_client.py` and `test_provider.py`; `README.md` documents install, config, host versions, local/remote modes, and approval fallback.

## Task 1: Fix the host contract and write failing tests

- [ ] Select and pin the Hermes release/commit supported by v2.0; verify its `MemoryProvider`, `MemoryManager`, tool-schema, profile-secret, and approval APIs against the pinned source before implementation.
- [ ] Test with HTTPX `MockTransport`: bearer auth on every request; query maps to `POST /api/v1/queries`; full note get maps to `GET /api/v1/notes/content`; writes map to `POST /api/v1/writes`; pending review maps to `GET /api/v1/writes`; API errors raise/report failures rather than return empty success.
- [ ] Test provider with `MemoryManager`: `is_available()` makes no network request; `initialize()` creates a profile-scoped client; `prefetch()` formats HLM-selected context; registered `hlm_query`, `hlm_get_note`, `hlm_propose_write`, `hlm_propose_folder`, and `hlm_pending_proposals` tools return JSON strings.
- [ ] Run `uv run --project integrations/hermes pytest -q`; confirm failures before implementation.

## Task 2: Implement the provider package

- [ ] Implement `HLMClient` with HTTPX, configured service URL and Hermes profile-scoped secret; validate `http`/`https` base URL and never log the token.
- [ ] Implement `HLMProvider(MemoryProvider)` with `name`, local-only `is_available`, `initialize`, schemas, handlers for `hlm_query`, `hlm_get_note`, `hlm_propose_write`, `hlm_propose_folder`, and `hlm_pending_proposals`, plus query-only `prefetch`; do not ingest each `sync_turn` automatically.
- [ ] Bind each provider instance to the `hermes_home` passed to `initialize`; do not use global mutable client/profile state.
- [ ] Use the same HLM API token for read/propose access; do not expose raw vault writes. Only expose `hlm_approve_proposal` and `hlm_reject_proposal` if the pinned Hermes host confirms a per-action user-confirmed gate that cannot be bypassed; otherwise return the web review route for pending proposals.
- [ ] Register via `register(ctx): ctx.register_memory_provider(HLMProvider())` and the `hermes_agent.memory_providers` entry point.

## Task 3: Verify install and commit

- [ ] Run unit tests and the pinned Hermes `MemoryManager` lifecycle tests (initialize, query/prefetch, tool dispatch, session end, shutdown).
- [ ] Run `uv run ruff check integrations/hermes` and `uv run pyright integrations/hermes`; test both local service URL and authenticated remote URL against the HLM API test server.
- [ ] Verify failed HLM reads are visible to the agent; ensure no silent fallback to Hermes' separate long-term store. Verify proposal stays pending until HLM UI or confirmed host action resolves it.
- [ ] Document exact install/config commands, permissions, secret setup, supported Hermes release and web-approval fallback in `integrations/hermes/README.md`. Commit as `feat: add Hermes shared memory provider`.
