# Changelog

## 1.3.0 - 2026-09-12

- Added authenticated, live-only graph traversal telemetry with bounded,
  lossy delivery, token policy filtering, and no replay support.
- Added token-scoped approval of a caller's own write proposals when explicitly
  enabled with `approve_own_proposals`.
- Released the API and package metadata as version 1.3.0.

## 1.2.1 - 2026-09-11

- Recover the filesystem observer after an unexpected stop, such as macOS
  sleep/wake.
- Recover Tide Atlas to the whole vault when a stale scoped graph selection
  becomes unavailable.
- Continue multi-page level-2 graph views when only edges remain.

## 1.2.0 - 2026-09-08

- Added durable Tide Atlas graph snapshots with policy-bound opaque pagination
  handles and SQL-level aggregate graph views.
- Added the interactive Tide Atlas web experience.

## Unreleased

## 1.0.0 - 2026-09-05

- CI mock-encoder tests now explicitly select the available runtime branch.
- Synthetic user-home test fixture paths are neutralized.

### Config-gated server
- All server surfaces are disabled by default: `frontend`, `api`, `mcp`, and
  `network`. `hlm serve` refuses to bind until the frontend is explicitly
  enabled (`hlm config set --frontend-enabled`). Configuration changes require a
  service restart.
- Frontend access modes: `loopback` (authenticated cookie session, no bearer
  token) and `lan` (HTTPS with an Argon2id password verifier + TLS, or explicit
  insecure HTTP on a trusted network with CIDR allow-listing).
- Bearer REST API and MCP are independent switches; both are bearer-token-only.

### API tokens
- Tokens are a set of vault-relative folder rules plus an admin flag, matched by
  longest-prefix with `read` as the default access. Tokens are immutable; create
  a new token and revoke the old one to change rules.
- `hlm token create|list|revoke` manage tokens; the authenticated Web UI can also
  create and revoke them. Plaintext tokens are printed once; only the SHA-256 hash
  is stored.

### MCP (Model Context Protocol)
- Streamable-HTTP MCP endpoint at `/mcp`, bearer-only, and independent of the
  browser UI. Enable with `hlm config set --mcp-enabled`.
- Every `/mcp` request requires a valid, non-revoked bearer token; missing or
  revoked tokens return `401` with an actionable `hint`. The service never falls
  back to open mode.
- `GET /health` now reports `mcp: {enabled, active_tokens}` so a disabled or
  tokenless MCP surface is diagnosable at a glance; startup logs a warning when
  MCP is enabled with zero active tokens.

### Embeddings
- Semantic search degrades gracefully when the optional `sentence-transformers`
  extra is not installed: the server starts and serves lexical/graph retrieval,
  logging a one-time notice instead of failing on model load.
- Keep the `embeddings` extra on reinstall so semantic search survives updates.

### Database
- Relative SQLite `DATABASE_URL` values resolve from the stable application data
  directory (`~/.config/harbor-ledger-memory/data`); absolute URLs are
  unchanged.
- Test fixtures no longer write API tokens into the live application database, so
  running the suite leaves the live token store untouched.

## 0.1.3 - 2026-09-02

- Ship all Alembic migrations, including the token-rule migration, in the
  package wheel.

## 0.1.2 - 2026-08-29

- Replaced insecure temporary-file creation in SQLite test fixtures with
  pytest-managed temporary paths.

## 0.1.0 - 2026-08-21

- Embeddings are now regenerated and persisted during scans, so configured
  semantic retrieval uses the scan-maintained embedding projection.
- Semantic candidate discovery is global by default; `--project` and
  `active_project` prioritize matching paths without excluding other relevant
  notes.
- `hlm serve` now performs an initial scan, maintains the local projection with
  a debounced vault watcher, and stops that watcher with the service lifecycle.
- Added optional local sentence-transformer embeddings for semantic similarity
  scoring. Enabled by setting `HLM_EMBEDDING_MODEL=all-MiniLM-L6-v2`. Notes are
  embedded during scan; queries use cosine similarity as an additional retrieval
  signal. Install with `uv sync --extra embeddings`.
- Added hybrid lexical retrieval with FTS, tag, path, title, summary, and project scoring
- Added bounded graph spreading activation with decay and weight multiplication
- Added smallest-sufficient context package builder with excerpt selection and token budgets
- Added query orchestration with trace persistence and replay endpoints
- Added SQL short-term memory cache with TTL cleanup and bounded LRU eviction
- Added `hlm query` CLI command with JSON and human-readable output
- Added `POST /api/v1/queries`, `GET /api/v1/traces/{trace_id}`, and `POST /api/v1/feedback` routes
- Added web UI with query, graph, and status screens plus self-updating banner
- Added feedback CLI command and adaptive edge weight adjustments
- Documented the standalone local-client direction, configurable canonical
  folder permissions, controlled writable-area growth, safe mode, and future
  approval and rollback requirements.
- Added a Phase 2 retrieval, spreading-activation, context-package, and query-trace implementation plan.
- Added the Python 3.12 project scaffold and process-backed settings.
- Added Phase 0 architecture, security, and observed vault-convention
  documentation.
- Added the initial configuration test and development quality commands.
- Added a canonical, read-only `VaultBoundary` for bounded enumeration of
  regular Markdown files under the configured `AI/` root.
- Added immutable domain models and deterministic parsing for safe leading YAML
  frontmatter, ATX headings, and unresolved Obsidian wikilinks, including typed
  malformed-frontmatter diagnostics.
- Hardened parser diagnostics for duplicate YAML keys, field-level metadata
  errors, invalid YAML structures, empty wikilink fragments, deterministic
  ordering, and Markdown fenced-code exclusions, including indented and
  list-nested fences.
- Added a rebuildable SQLite catalog for parsed notes, outgoing link
  identities, scan metadata, and diagnostics, with transactional FTS5
  synchronization and an Alembic initial migration.
- Added deterministic full scans restricted to admitted `AI/` Markdown files,
  SHA-256 content hashes, explicit/folder-relative link resolution, and
  persisted broken or ambiguous link states.
- Added a conservative typed NetworkX graph with explicit wikilink,
  filesystem-containment, and resolved parent edges, plus deterministic graph
  neighbour queries.
- Added safe plain-token FTS search and read-only validation findings for link,
  routing, frontmatter, index, parent, orphan, duplicate-path, and
  `SHORT-TERM` consistency checks.
- Hardened scans to hash and parse one verified immutable byte snapshot per
  admitted identity, and require `graph_color` for active-note frontmatter.
- Added a typed read-only `hlm` CLI for scanning, status, search, validation,
  node inspection, and graph-neighbour inspection.
- Added a localhost-only FastAPI app factory with read-only health and catalog
  status routes.
- Added defensive debounced watching with boundary admission, temporary-file
  filtering, duplicate coalescing, and one retry for transient scan failures.
- Added temporary-fixture CLI, API, watcher, and end-to-end Phase 1 tests.
- Added `.gitignore` rules for local runtime files, generated artifacts,
  application data, and task-tool state.

No vault-write behavior is included in this release. Catalog persistence is
derived-only and never accesses a vault;
`rebuild_catalog()` clears only application-owned projection tables. Scans,
graph construction, search, and validation consume only this derived catalog
and the admitted `AI/` files. The parser preserves wikilink targets, aliases,
headings, and block IDs before the scan resolves them. The boundary only
enumerates admitted regular Markdown paths, rejects file-symlink aliases, never
traverses symlinked directories, and fails closed if the canonical root is
replaced.

The documentation contract remains ongoing: `README.md` is the setup and
command guide, and this changelog records repository changes. Update them when
the documented surface or layout changes; `.gitignore` records the local and
generated state excluded from the repository.
