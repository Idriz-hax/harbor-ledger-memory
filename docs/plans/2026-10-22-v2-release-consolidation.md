# HLM 2.0.0 Release Consolidation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the shared note API and both native providers together as HLM 2.0.0 without regressing current HLM behavior.

**Architecture:** Execute only after the note-content API, Hermes provider, and OpenClaw provider plans pass their individual checks. Consolidate versioning, top-level documentation, and cross-client acceptance tests; do not add another runtime abstraction or migration.

**Tech Stack:** Python package metadata, Markdown docs, pytest, Ruff, Pyright, npm/OpenClaw package checks.

---

## File map

- Modify root `pyproject.toml`, `README.md`, and `CHANGELOG.md`; root `pyproject.toml` is authoritative for the HLM package version.
- Create `RELEASE_NOTES-2.0.0.md` and `docs/upgrading-to-2.0.0.md`.
- Run the API, Hermes, OpenClaw, and existing HLM test/build commands; no runtime source changes belong in this plan.

## Task 1: Consolidate public docs and version

- [ ] Confirm the note-content API and both provider-specific README files describe matching route names, auth, local/remote modes, proposal behavior, approval fallback, and tested host versions.
- [ ] Update root `README.md` to frame HLM as one Markdown-backed shared memory for existing tools and connected agents; link the Hermes/OpenClaw installation guides and preserve current HLM installation/use docs.
- [ ] Add a dated `2.0.0` section to `CHANGELOG.md` describing the read endpoint, both native providers, approval behavior, and explicitly stating that existing vaults require no migration; preserve any still-unreleased 2.x work under `Unreleased`.
- [ ] Change only `[project].version` in root `pyproject.toml` from `1.3.5` to `2.0.0`; verify the runtime API version still derives from package metadata.
- [ ] Create `RELEASE_NOTES-2.0.0.md` and `docs/upgrading-to-2.0.0.md` with the same no-data-migration statement and optional integration setup steps.

## Task 2: Run release acceptance checks

- [ ] Run `uv run pytest -v`, `uv run ruff check backend tests`, `uv run ruff format --check .`, and `uv run pyright backend/src`.
- [ ] From `integrations/hermes/`, run `uv run pytest -q` against the pinned Hermes provider harness; from `integrations/openclaw/`, run `npm test && npm run typecheck`.
- [ ] Build HLM distributions with `uv build`; inspect wheel and source archive to confirm the HLM application package and migration assets remain present.
- [ ] Run pinned-host package smoke tests: install the Hermes entry point and OpenClaw npm tarball, select each as the native provider, and verify query, note-get, proposal listing, pending approval, and approved visibility against the same local HLM vault. Repeat against an authenticated remote HLM service.
- [ ] Verify chat approval is user-confirmed and one-shot where supported; verify denied/unavailable approval paths leave proposals pending and expose the web review path.
- [ ] Commit docs/version changes as `release: prepare v2.0.0` only after every acceptance check passes.
