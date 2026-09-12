# Harbor Ledger Memory

Harbor Ledger Memory is local neural memory for AI agents that works with an existing Markdown vault. Obsidian is a compatible vault application, not a product requirement. It builds a rebuildable index with lexical search, optional semantic search, knowledge-graph context, and Model Context Protocol (MCP) tools. Your Markdown files remain authoritative: the service never rewrites vault content outside its policy-controlled proposal and approval workflow.

<p align="center">
  <img src="docs/assets/harbor-ledger-memory.png" alt="Harbor Ledger Memory map showing connected agent context routes across a local-first ledger." width="960" />
</p>

## Contents

- [Quickstart](#quickstart)
- [What it does](#what-it-does)
- [Use cases](#use-cases)
- [Security and privacy](#security-and-privacy)
- [Server configuration](#server-configuration)
  - [API tokens](#api-tokens)
  - [MCP clients](#mcp-clients)
- [Development from a checkout](#development-from-a-checkout)
- [CLI and API reference](#cli-and-api-reference)
- [Limitations and troubleshooting](#limitations-and-troubleshooting)
- [Verification](#verification)
- [Project links](#project-links)

## Quickstart

This flow builds the UI, installs `hlm`, points it at a vault, builds the index, and starts the local service.

Requirements:

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) — an isolated Python toolchain; no `sudo`
  and no system-wide package changes
- Node.js 18 or newer — required to build the bundled web UI

The global install packages the web UI, so the npm build step is required. If
you'd rather run the CLI without the browser UI, use
[Development from a checkout](#development-from-a-checkout) and skip the npm
build — every `hlm` command still works, only the web UI is unavailable.

### Install the CLI

This installs the `hlm` command globally for the current user. Everything stays
under `~/.local`; nothing is installed system-wide.

```text
# 1. Get the source
git clone https://github.com/Idriz-hax/harbor-ledger-memory.git
cd harbor-ledger-memory

# 2. Build the web UI (the wheel packages this, so it is required)
(cd web && npm ci && npm run build)

# 3. Install the CLI globally (user-isolated, no sudo)
uv tool install .

# 4. Point it at an existing Obsidian vault directory
hlm config set --vault-path /path/to/your/obsidian/vault

# 5. Enable the surfaces you want. The server is config-gated and refuses to
#    start until the frontend is enabled. Localhost UI + MCP:
hlm config set --frontend-enabled --frontend-mode loopback --mcp-enabled

# 6. Verify the index builds, then start the service
hlm scan
hlm serve  # open http://127.0.0.1:8765
```

`uv tool install` puts `hlm` in `~/.local/bin` and keeps its dependencies in a
private runtime under `~/.local/share/uv/tools`. If `hlm` is not found,
open a new terminal or add `~/.local/bin` to your `PATH`. Remove it later with
`uv tool uninstall hlm`.

To update later, pull the changes and reinstall:

```text
git pull
(cd web && npm ci && npm run build)
uv tool install --reinstall .
```

If you use semantic search, keep the `embeddings` extra on reinstall so the
model stays available (`uv tool install --reinstall '.[embeddings]'`). Without
it the server still runs but degrades to lexical/graph retrieval until you
reinstall the extra.

### Optional semantic search

For semantic search, install with the `embeddings` extra first (a large
download — it pulls PyTorch):

```text
uv tool install '.[embeddings]'
hlm config set --embedding-model all-MiniLM-L6-v2
```

If the `sentence-transformers` extra is missing, the server still starts and
serves lexical/graph retrieval; it logs a one-time notice and skips semantic
similarity instead of failing on model load. Reinstall with the extra to
re-enable semantic search.

## What it does

- **Indexes local Markdown**: builds a deterministic SQLite projection from an admitted vault scope
- **Finds relevant notes**: combines lexical search, optional semantic similarity, and graph-based context
- **Serves local agents**: exposes bounded MCP and REST operations behind explicit configuration and bearer-token rules
- **Keeps vault ownership clear**: rebuilds derived data from Markdown and requires policy-controlled write approval

## Use cases

- **Search a local knowledge base**: retrieve notes from an Obsidian vault without uploading the vault to a hosted service
- **Give an AI agent bounded context**: connect a local MCP client to retrieve and cite relevant Markdown notes
- **Run an auditable memory service**: rebuild the catalog from Markdown, inspect its health, and control write access with folder rules

## Security and privacy

- **Vault access is policy-controlled.** Read-only vault commands read Markdown
  and build a disposable index in a local SQLite database (default
  `~/.config/harbor-ledger-memory/data/memory.db`, overridable with
  `DATABASE_URL`; relative SQLite URLs resolve from that stable application
  directory, while absolute URLs are unchanged). Vault writes are exposed
  only through the API and MCP lifecycle tools: proposal/approval rules require approval, while
  `auto-write` applies automatically only to eligible managed updates under the
  configured folder rules; creates, unmanaged files, and conflicts remain
  proposals for approval. MCP exposes `propose_write`, `approve_proposal`, and
  `reject_proposal` with the same policy-controlled lifecycle.
- **Operational telemetry is restricted.** Activity history and streams require
  authentication and are filtered to the caller's readable paths. Graph
  traversal telemetry is live-only: it is intentionally lossy under backpressure,
  is policy-filtered, is not persisted, and cannot be replayed after connecting.
  Reconnects provide only newly published traversal events.
- **The install is isolated.** `uv tool install` uses a private virtual
  runtime; there is no `sudo`, no global Python packages, and no `npm install -g`.
- **The scope is bounded.** By default the whole vault is indexed. Narrow it
  with `--index-root` and `--folder-rules` (below).

## Server configuration

`hlm config set` writes to `~/.config/harbor-ledger-memory/config.toml` and
prints that a running service must be restarted to apply the change:

```text
hlm config set --index-root AI
hlm config set --folder-rules '[{"path":"AI/Private","access":"none"}]'
hlm config show
```

Process environment variables override the TOML file. The global CLI reads these
variables and does not load a `.env` file on its own; to use one, export it
first (`set -a; source .env; set +a`) or run `uv run --env-file .env hlm ...`
from a checkout.

All server features are disabled by default. `hlm serve` refuses to bind until
the frontend is explicitly enabled. Configuration changes take effect after a
service restart.

Each setting persists with `hlm config set` or via the `HLM_*` process variables
shown here; process variables take precedence over the TOML file. The examples
below use process variables to be explicit and one-off.

Loopback UI access needs only the frontend switch:

```text
HLM_FRONTEND__ENABLED=true HLM_FRONTEND__MODE=loopback hlm serve  # http://127.0.0.1:8765
```

For HTTPS LAN access, set the frontend origin, the Argon2id verifier, and
owner-readable certificate and key files. Create the verifier without exposing
the password:

```text
hlm config set-password
HLM_FRONTEND__ENABLED=true HLM_FRONTEND__MODE=lan \
HLM_NETWORK__ENABLED=true HLM_FRONTEND__PUBLIC_ORIGIN=https://memory.example:8443 \
HLM_NETWORK__TLS_CERT=/path/to/cert.pem HLM_NETWORK__TLS_KEY=/path/to/key.pem \
HLM_NETWORK__ALLOWED_CIDRS='["192.168.1.0/24"]' \
hlm serve --host 0.0.0.0 --port 8443
```

In a trusted network, insecure LAN HTTP requires an explicit acknowledgement
and no TLS files:

```text
HLM_FRONTEND__ENABLED=true HLM_FRONTEND__MODE=lan \
HLM_NETWORK__ENABLED=true HLM_NETWORK__INSECURE_HTTP=true \
HLM_FRONTEND__PUBLIC_ORIGIN=http://memory.local \
hlm serve --host 0.0.0.0
```

REST bearer endpoints and MCP are independent switches. Enable either or both
for external clients; use `Authorization: Bearer <token>` for those clients:

```text
HLM_API__ENABLED=true   # bearer REST API
HLM_MCP__ENABLED=true   # bearer-only MCP at /mcp
```

The browser UI uses its HTTP-only session cookie (plus CSRF protection for
mutations); that cookie does not authenticate MCP, and external REST/MCP use
bearer tokens. The UI session does not turn on the bearer REST or MCP
transports. Restart `hlm serve` after changing these settings.

### API tokens

External/integration REST and MCP traffic requires a valid bearer token, even
while zero tokens exist. When started with `hlm serve`, the browser Web UI uses
its authenticated cookie session and does not need a bearer token. This cookie
session exception applies to the UI's REST calls only; MCP remains bearer-only.
Create tokens for external REST and MCP clients from the CLI:

```text
hlm token create -n <name> --admin --rule AI=auto-write
```

A token is a set of **folder rules** plus an **admin** flag. Rules are
vault-relative `PATH=LEVEL` pairs where LEVEL is one of
`none | read | propose-write | auto-write`. Matching is longest-prefix: the
deepest rule covering a path wins, a child rule overrides its parent, and an
unmatched path defaults to `read`. A `none` rule hides a subtree from the
token (no read, no write). `--admin` allows managing tokens and settings but
does not bypass folder rules; without any `--rule` a token is read-only
everywhere.

Tokens are immutable — to change a rule or the admin flag, create a new token
and revoke the old one.

The authenticated Web UI and REST token-creation API expose the
`approve_own_proposals` boolean field. Set it to `true` to explicitly permit
that token to approve proposals it created itself; without it, own-proposal
approval is rejected even when the token can otherwise write to the path. The
CLI does not currently expose this field.

It prints the plaintext `hlm_…` token **once** (only its SHA-256 hash is
stored). Send it as `Authorization: Bearer hlm_…` on external REST/MCP calls;
the authenticated local Web UI can also create and revoke these tokens.

| Condition | Response |
| --- | --- |
| Missing or invalid token (even when zero tokens exist) | `401` with `detail`, `token_required: true`, and an actionable `hint` to create a token with `hlm token create` and set it as the client's bearer |
| Path not writable by the token | `403` `{"detail": "path '<path>' not writable by this token"}` |
| Write call with no write access at all | `403` `{"detail": "token has no write access"}` |
| Admin-only route (token/settings management) | `403` `{"detail": "token missing admin access"}` |

On MCP, denials surface as tool errors (for example
`path 'AI/x.md' not writable by this token`) instead of HTTP `403` responses,
while an unauthenticated MCP request still gets the HTTP `401` body from the
middleware.

Manage tokens with `hlm token list` and `hlm token revoke <name>` (or the
settings page). Revoking a token takes effect immediately; the service never
falls back to open mode.

### MCP clients

MCP is a streamable-HTTP endpoint at `/mcp`, bearer-only, and independent of
the browser UI. Enable it with `hlm config set --mcp-enabled` (or
`HLM_MCP__ENABLED=true`), create a token as above, and point a client at
`http://127.0.0.1:8765/mcp/` with that token. In OpenCode's `opencode.json`:

```json
{
  "mcp": {
    "memory": {
      "type": "remote",
      "url": "http://127.0.0.1:8765/mcp/",
      "enabled": true,
      "headers": { "Authorization": "Bearer hlm_…" }
    }
  }
}
```

MCP sessions are per-process: after `hlm serve` restarts, a client's cached
session goes stale (`404 Session not found`) until the client reconnects.

If MCP appears disabled to a client, check `GET /health`: the
`mcp.active_tokens` count is the number of non-revoked bearer tokens. A count of
`0` means every token has been revoked, so every `/mcp` request returns `401`
until a token is recreated with `hlm token create`.

## Development from a checkout

To edit the code or serve the UI straight from this checkout (no global
install):

```text
uv sync                          # Python + dev dependencies
uv sync --extra embeddings       # optional: semantic search (large download)
(cd web && npm ci && npm run build)
cp .env.example .env             # set HLM_VAULT_PATH to an existing vault
uv run --env-file .env hlm serve # opens an authenticated local Web UI session
```

The dev server behaves identically to the global CLI; it simply serves the web
UI from this checkout's `web/dist`. The service runs an initial scan before
serving, then watches admitted Markdown files under the index root and rescans
after debounced changes. The watcher and scan services never write vault files.

Queries are global by default: candidates are discovered across the whole
admitted projection. `hlm query "..." --project PROJECT_PATH` and the API's
`active_project` value prioritize paths matching that project, but do not filter
out other relevant notes.

The default application boundary is the full configured vault. Set
`HLM_INDEX_ROOT` to narrow it to a vault-relative subtree. `HLM_FOLDER_RULES`
accepts JSON rules such as
`[{"path":"Private","access":"none"}]`; the most-specific rule wins,
reads default to allow, and policy-controlled vault writes use proposal/approval
or `auto-write` under the configured folder rules. Rules must remain inside the
index root. Read-only vault commands and API writes use the same boundary, which
rejects traversal, encoded traversal, symlink escapes, file-symlink aliases, and
sibling-prefix escapes such as `AI-evil`; a replaced root fails closed before
enumeration.

## CLI and API reference

After configuring `HLM_VAULT_PATH`, the CLI exposes bounded read/query operations
and local application management:

```text
hlm scan
HLM_EMBEDDING_MODEL=all-MiniLM-L6-v2 uv run hlm scan
hlm query "vault safety" --format json
HLM_EMBEDDING_MODEL=all-MiniLM-L6-v2 uv run hlm query "knowledge management"
hlm query "recent migration work" --project local-obsidian-brain
hlm status
hlm search "recursive index"
hlm validate --format json
hlm show-node AI/INDEX.md
hlm show-neighbours AI/Knowledge/INDEX.md
hlm watch
hlm config show --json
hlm config set --index-root Projects --folder-rules '[{"path":"Projects/Private","access":"none"}]'
```

`scan` rebuilds the disposable catalog; `query` returns selected memories with
retrieval scores, activation scores, reasons, and a trace ID. `watch` is a local
convenience that debounces events and requests a bounded full scan. External
sync tools can deliver partial writes, so `hlm scan` remains the authoritative
recovery action. `hlm config set` writes only the application TOML and reports
that a restart is required. The local settings page is available at `/settings`
with matching `GET`/`PUT` `/api/v1/settings` endpoints. There is no
`short-term` CLI command; short-term state is managed only in SQL as part of
query execution.

The frontend service also exposes the JSON API with uvicorn (bundled
dependency):

```text
HLM_FRONTEND__ENABLED=true HLM_FRONTEND__MODE=loopback \
HLM_API__ENABLED=true HLM_VAULT_PATH=/path/to/your/obsidian/vault \
hlm serve
```

Core JSON API routes are `GET /health`, `GET /api/v1/status`,
`POST /api/v1/queries`, `GET /api/v1/traces/{trace_id}`,
`POST /api/v1/feedback`, and `GET /api/v1/cache-status`. The service also
exposes `GET /api/v1/settings`, `PUT /api/v1/settings`,
`GET /api/v1/update-status`, and `POST /api/v1/update`. Settings changes
persist only application configuration and do not mutate a running watcher.
`GET /api/v1/graph/traversal/stream` is an authenticated SSE feed of newly
published, policy-filtered traversal events. It has no history or replay cursor;
slow subscribers may miss events.

The `@Memory` agent can load vault context before tasks by querying the API:

```bash
curl -s -X POST http://127.0.0.1:8765/api/v1/queries \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"query": "vault safety"}' | python -m json.tool
```

Response includes `selected_memories` (paths, excerpts, scores, reasons) and a
`trace_id` for replay. Use this to load relevant memory into agent context
before starting work.

## Limitations and troubleshooting

- **Python and Node are required**: install Python 3.12+, uv, and Node.js 18+ before building the bundled web UI
- **Semantic search is optional**: installing `sentence-transformers` downloads PyTorch; without it, the service uses lexical and graph retrieval
- **Network surfaces stay off by default**: enable the frontend, REST API, MCP, and LAN access explicitly, then restart `hlm serve`
- **MCP needs a valid token**: create a non-revoked bearer token with `hlm token create`; `GET /health` reports `mcp.active_tokens` when diagnosing authentication
- **Reconnect after a restart**: cached MCP sessions become stale after `hlm serve` restarts

## Verification

```text
uv run pytest -v
uv run alembic -c backend/alembic.ini upgrade head
uv run ruff check backend tests
uv run ruff format --check .
uv run pyright backend/src
```

The parser uses safe YAML loading for a leading frontmatter block and reports
malformed frontmatter, duplicate keys, and malformed wikilink fragments as
typed diagnostics. It ignores headings and wikilinks inside top-level,
indented, and list-nested fenced code, and does not resolve link targets. The
scan-derived SQLite tables are `notes`, `links`, `scan_runs`, `diagnostics`,
and the `note_fts` FTS5 projection; when embeddings are enabled, scans also
regenerate `note_embeddings`. Separate operational tables are
`query_traces`, `activation_visits`, `context_selections`,
`short_term_events`, `short_term_entries`, and `adaptive_edges` for query
traces, context selections, short-term cache state, and feedback adjustments.
`rebuild_catalog()` clears the scan-derived projection rows without touching
vault files. `ScanService`, `GraphBuilder`, `GraphService`, `SearchService`,
and `ValidationService` are framework-neutral Python interfaces for the
completed scan/graph/search/validation wave. The CLI, localhost API factory,
and defensive watcher are thin adapters over those services.

## Project links

- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Security policy](SECURITY.md)
- [Report a bug or request a feature](https://github.com/Idriz-hax/harbor-ledger-memory/issues)
- [Released versions](https://github.com/Idriz-hax/harbor-ledger-memory/releases)
