# Task 7 Report: v1.3.0 release and system verification

## Status

Implemented the v1.3.0 release metadata and documentation updates. Validation
owner: reviewer.

## Changes

- Bumped the Python package and FastAPI application versions to `1.3.0`.
- Added an application-version regression test.
- Documented authenticated, policy-filtered, lossy, live-only, non-replayable
  traversal telemetry and explicit own-proposal approval.
- Added the v1.3.0 changelog entry.
- Extended release verification to check the packaged app version and to run a
  source/Web UI verification when invoked without artifact arguments.
- Updated `uv.lock` to match the project version.

## Verification

- `uv run pytest`: **failed** — 589 passed, 6 failed. The failures are in
  existing Task 6/earlier integration expectations: watcher fakes do not accept
  `live_traversal`, write-listing expectations omit `creator_token_id`, MCP
  approval fixtures lack own-approval permission, and the offline SQLite
  migration still uses reflection-dependent batch mode.
- `uv run ruff check backend/src tests && uv run pyright backend/src`:
  **failed** — ruff reported 137 existing branch-wide lint errors, so the
  chained pyright command was short-circuited. Standalone `uv run pyright
  backend/src` also failed with 47 existing diagnostics. No unrelated cleanup
  was attempted.
- `npm test -- --run && npm run build` from `web/`: **passed** — 93 passed and
  6 skipped; production build completed.
- `uv run python scripts/verify_release_artifacts.py`: **passed** — source
  metadata and built Web UI verified for `1.3.0`.
- `git diff --check`: **passed**.

## Review notes

Only Task 7 release metadata, documentation, artifact verification, the release
version test, lock metadata, and this report were changed. Frontend package
version remains independent as required. No credentials or generated release
archives were added.

## Review-fix follow-up

Validation owner: Task 7 reviewer.

- Corrected the README to identify `approve_own_proposals` as an authenticated
  Web UI and REST token-creation API field; it no longer claims CLI support.
- Source-mode artifact verification now checks supplied `--version`/`--tag`
  values against `pyproject.toml` metadata and rejects mismatches.
- Focused tests: `2 passed, 16 deselected`.
- Focused artifact checks passed for no arguments, matching `--version`, and
  matching `--tag`; mismatched version and tag values were both rejected.
