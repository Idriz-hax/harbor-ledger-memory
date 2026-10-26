# HLM 2.0.0 Release Consolidation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the shared note API, bounded agent-write APIs, and both native providers together as HLM 2.0.0 without regressing current HLM behavior.

**Architecture:** Execute only after the note-content API, bounded agent-write API, Hermes provider, and OpenClaw provider plans pass their individual checks. Consolidate versioning, top-level documentation, and cross-client acceptance tests; do not add another runtime abstraction or migration.

**Tech Stack:** Python package metadata, Markdown docs, pytest, Ruff, Pyright, npm/OpenClaw package checks.

---

## File map

- Modify root `pyproject.toml`, `uv.lock`, `README.md`, and `CHANGELOG.md`; root `pyproject.toml` is authoritative for the HLM package version and `uv.lock` mirrors the local project version.
- Create `RELEASE_NOTES-2.0.0.md` and `docs/upgrading-to-2.0.0.md`.
- Run the note-content API, bounded agent-write API, Hermes, OpenClaw, and existing HLM test/build commands; no runtime source changes belong in this plan.

## Task 1: Consolidate public docs and version

- [x] Confirm the note-content and bounded agent-write APIs and both provider-specific README files describe matching route names, auth, limits, local/remote modes, proposal behavior, approval fallback, and tested host versions.
- [x] Update root `README.md` to frame HLM as one Markdown-backed shared memory for existing tools and connected agents; link the Hermes/OpenClaw installation guides and preserve current HLM installation/use docs.
- [x] Add a dated `2.0.0` section to `CHANGELOG.md` describing the read endpoint, bounded agent-write contract, both native providers, approval behavior, and explicitly stating that existing vaults require no migration; preserve any still-unreleased 2.x work under `Unreleased`.
- [x] Change `[project].version` in root `pyproject.toml` and its local package entry in `uv.lock` from `1.3.5` to `2.0.0`; run `uv lock` without upgrading unrelated dependencies, then `uv sync`; verify the runtime API version still derives from package metadata.
- [x] Create `RELEASE_NOTES-2.0.0.md` and `docs/upgrading-to-2.0.0.md` with the same no-data-migration statement and optional integration setup steps.

## Task 2: Run release acceptance checks

- [x] Run `uv run pytest -v`, `uv run ruff check backend tests`, `uv run ruff format --check .`, and `uv run pyright backend/src`.
- [x] Run `uv run --with hermes-agent==0.18.1 pytest integrations/hermes/tests -q` against the pinned Hermes provider harness; from `integrations/openclaw/`, run `npm test && npm run typecheck`.
- [x] Build HLM distributions with `uv build`; inspect wheel and source archive to confirm the HLM application package and migration assets remain present.
- [x] Run pinned-host package smoke tests against a shared local HLM vault. Hermes provider and OpenClaw npm package installed under pinned host versions; OpenClaw config validated and plugin activated with five tools. Cross-client smoke verified shared query/note reads, proposal submission, shared pending summaries, and approved-note visibility. **Not run:** authenticated remote HLM service smoke; no service was configured.
- [x] Verify provider approval gates through host/harness checks and local smoke. OpenClaw resolution is gated by host one-shot approval; Hermes exposes no approval tool. **Not run:** exhaustive live-host denied/unavailable approval scenarios; web review fallback remains documented.
- [x] Commit release docs, APIs, and integrations as `release: prepare v2.0.0` after checks pass. GitHub release/tag remains unpublished until the tag workflow completes.

### Verification note (2026-10-27)

- `uv run pytest -q`: 691 passed (5 SQLAlchemy cache-key warnings).
- CLI/REST/MCP compatibility subset (`tests/test_cli.py`, `tests/test_api.py`, `tests/test_mcp.py`, `tests/test_mcp_tokens.py`): 106 passed.
- `uv run --with hermes-agent==0.18.1 pytest integrations/hermes/tests -q`: 43 passed.
- OpenClaw: 20 npm tests passed, `npm run typecheck` passed, and `npm pack --dry-run` built and inspected `harbor-ledger-memory-openclaw-2.0.0.tgz`.
- `npm ci && npm run build` passed for the web UI (Vite emitted its existing large-chunk warning); `npm audit --omit=dev` found 0 production dependency vulnerabilities in both npm packages.
- `uv build` produced the 2.0.0 sdist and wheel; archive inspection confirmed application modules and migration assets in both. Runtime API version and installed package metadata both report 2.0.0.
- Full Ruff check and format check still report pre-existing repository diagnostics in migrations, tests, and existing API code. New agent API/auth additions were reformatted; targeted Ruff check now has only the pre-existing `app.py` E501. Pyright reports 7 existing `reportUnusedFunction` diagnostics in API/MCP route functions.
- Node 24.16.0 OpenClaw host install/inspect smoke and local cross-client smoke passed. No authenticated remote-service smoke was run. Ruff and Pyright have baseline diagnostics as noted above.
