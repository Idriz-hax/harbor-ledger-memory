# Harbor Ledger Memory

Harbor Ledger Memory is local neural memory for AI agents that works with an existing Markdown vault. Obsidian is a compatible vault application, not a product requirement. It builds a rebuildable index with lexical search, optional semantic search, knowledge-graph context, and Model Context Protocol (MCP) tools. Your Markdown files remain authoritative: the service never rewrites vault content outside its policy-controlled proposal and approval workflow.

HLM 2.1.1 adds policy-checked move and delete proposals, broader conflict-free auto-write updates, and a configurable OpenClaw response limit. It retains optional multilingual E5 embeddings and explicit full-note reads. See [GitHub Releases](https://github.com/Idriz-hax/harbor-ledger-memory/releases), the [Hermes provider guide](integrations/hermes/README.md), and [OpenClaw plugin guide](integrations/openclaw/README.md).

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

This installs `hlm` and runs its first-time setup. It does not start the service;
you start it after setup. You do not need Obsidian installed—only an existing
directory of Markdown notes.

Requirements:

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) — an isolated Python toolchain; no `sudo`
  and no system-wide package changes
- Node.js 18 or newer — required to build the bundled web UI
- Git and npm — used to download the source and build the UI

The global install packages the web UI, so the npm build step is required. For
CLI-only use without Node.js, see [Development from a checkout](#development-from-a-checkout).

### Install the CLI

This installs the `hlm` command for the current user. Its command and runtime stay
under `~/.local`; the source checkout stays in the directory you choose.

On macOS or Linux, run these commands from the directory where you want the
checkout. They check prerequisites and will not overwrite an existing checkout:

```sh
command -v python3 >/dev/null || {
  echo 'Install Python 3.12 or newer first.' >&2
  exit 1
}
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || {
  echo 'Python 3.12 or newer is required.' >&2
  exit 1
}
command -v git >/dev/null || { echo 'install git first.' >&2; exit 1; }
command -v uv >/dev/null || {
  echo 'install uv: https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 1
}
command -v node >/dev/null || { echo 'Install Node.js 18 or newer first.' >&2; exit 1; }
command -v npm >/dev/null || { echo 'install npm first.' >&2; exit 1; }
node -e 'if (+process.versions.node.split(".")[0] < 18) process.exit(1)' || {
  echo 'Node.js 18 or newer is required.' >&2
  exit 1
}
[ ! -e "$PWD/harbor-ledger-memory" ] || {
  echo 'harbor-ledger-memory already exists; choose another directory.' >&2
  exit 1
}
git clone https://github.com/Idriz-hax/harbor-ledger-memory.git
cd harbor-ledger-memory
./scripts/install.sh
```

The installer builds the web UI, installs the CLI in uv's isolated tool
environment, and runs `hlm setup`. Enter the path to an existing directory of
Markdown notes when asked for the vault. Have these choices ready:

- Whether to enable the browser UI, MCP, or REST API. For a typical local setup,
  enable the UI and only enable MCP if connecting an agent; leave the REST API
  off unless you have a REST client. Keep localhost-only access.
- If enabling MCP with a host, its name (`opencode`, `openclaw`, or `hermes`), a
  descriptive token name (for example, `openclaw-read`) and one or more
  vault-relative folders to grant read access to. Only those selected folders
  are readable by that token.
  If MCP is enabled, choose `skip` at the host prompt when you do not want a
  host integration. Leave MCP disabled for a UI-only setup.
- Embeddings are optional; choose no unless you specifically want semantic
  search. They require a separate, large model download.

Setup enables only the services you choose and saves the config under
`~/.config/harbor-ledger-memory/`. It does not start the server. If you enabled
the UI, start the service and open it locally:

```sh
hlm serve
# Open http://127.0.0.1:8765
```

If setup stops on an error, fix the reported issue and rerun `hlm setup`.

If you enable MCP, setup asks which host to connect, then creates a read-scoped
token for the folders you choose. OpenCode's `opencode.json` is updated when it
can be edited safely; JSONC or conflicting configurations need a manual merge.
For OpenClaw, setup prints manual plugin/configuration steps. For Hermes, setup
installs provider files and configures the profile URL. Both hosts require you
to save the one-time token in their secret store; do not put it in host config.
Have the selected host installed and configured first. HLM cannot create or
populate a host secret for you; complete that step in the host's configured
secret provider before connecting it.
See the [OpenClaw](integrations/openclaw/README.md) and
[Hermes](integrations/hermes/README.md) guides for their supported versions and
manual steps. Revoke a token with `hlm token revoke NAME`.

To configure an existing checkout manually, use:

```text
# Point it at an existing Obsidian vault directory
hlm config set --vault-path /path/to/your/obsidian/vault

# Enable the surfaces you want. The server is config-gated and refuses to
#    start until the frontend is enabled. Localhost UI + MCP:
hlm config set --frontend-enabled --frontend-mode loopback --mcp-enabled

# Verify the index builds, then start the service
hlm scan
hlm serve  # open http://127.0.0.1:8765
```

`uv tool install` puts `hlm` in `~/.local/bin` and keeps its dependencies in a
private runtime under `~/.local/share/uv/tools`. If `hlm` is not found,
open a new terminal or add `~/.local/bin` to your `PATH`. Remove it later with
`uv tool uninstall hlm`.

To update a manual checkout later, pull the changes and reinstall:

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

For multilingual notes, choose the multilingual E5 model explicitly:

```text
uv tool install --reinstall '.[embeddings]'
hlm config set --embedding-model intfloat/multilingual-e5-small
hlm scan
```

For English-only notes, `all-MiniLM-L6-v2` is the compact baseline configured
for this local install; this project has not established it as the best English
model. The first scan/query downloads model weights into the local model cache;
installation does not download model weights. E5 inputs are prefixed internally
with `passage: ` for indexed notes and `query: ` for searches, as required by
that model family. Other models,
including MiniLM, receive unchanged text. After changing model IDs, run
`hlm scan` to regenerate model-keyed vectors. This configuration is opt-in; the
application default remains embeddings disabled (`embedding_model = None`).

### Semantic search performance

Explicit note reads (`read_note` and `read_notes`) do not use embeddings.
Semantic queries do: on this 200-note synthetic test, warm p50 latency was
about 2 ms without embeddings and 35–37 ms with them. Embeddings improved
retrieval on these test queries, but the small synthetic sample does not show
which model is generally more accurate.

| Test set | Configuration | Top-1 | MRR@10 | Warm query p50 / p95 |
| --- | --- | ---: | ---: | ---: |
| English | No embeddings | 6/20 | 0.300 | 2.4 / 18.2 ms |
| English | `all-MiniLM-L6-v2` | 20/20 | 1.000 | 34.7 / 40.1 ms |
| English | `intfloat/e5-small-v2` | 20/20 | 1.000 | 37.0 / 41.4 ms |
| Multilingual (EN + ES → EN) | No embeddings | 6/40 | 0.150 | 2.3 / 18.0 ms |
| Multilingual (EN + ES → EN) | `intfloat/multilingual-e5-small` | 29/40 | 0.738 | 36.8 / 38.8 ms |
| Multilingual (EN + ES → EN) | `paraphrase-multilingual-MiniLM-L12-v2` | 32/40 | 0.800 | 36.9 / 39.3 ms |

These are warm timings on a small catalog; model startup and first-use downloads
are additional. Semantic retrieval currently scores every stored vector, so
that linear scan can become the larger cost as a catalog grows. See the
[benchmark details](docs/embedding-benchmark.md) for the methodology, other
measurements, and limitations.

## What it does

- **Indexes local Markdown**: builds a deterministic SQLite projection from an admitted vault scope
- **Finds relevant notes**: combines lexical search, optional semantic similarity, and graph-based context
- **Serves local agents**: exposes bounded MCP and REST operations behind explicit configuration and bearer-token rules
- **Keeps vault ownership clear**: rebuilds derived data from Markdown and requires policy-controlled write approval

## Use cases

- **Search a local knowledge base**: retrieve notes from an Obsidian vault without sending vault content to a hosted service
- **Give an AI agent bounded context**: connect a local MCP client to retrieve and cite relevant Markdown notes
- **Run an auditable memory service**: rebuild the catalog from Markdown, inspect its health, and control write access with folder rules

## Security and privacy

- **Vault access is policy-controlled.** Read-only vault commands read Markdown
  and build a disposable index in a local SQLite database (default
  `~/.config/harbor-ledger-memory/data/memory.db`, overridable with
  `DATABASE_URL`; relative SQLite URLs resolve from that stable application
  directory, while absolute URLs are unchanged). Vault writes are exposed
  only through the API and MCP lifecycle tools. `propose-write` requires
  approval; `auto-write` applies conflict-free updates and creates immediately
  within the configured folder rules. Version conflicts remain pending for
  review. MCP exposes `propose_write`, `approve_proposal`, and
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
- **Vault content stays local by default.** HLM does not send note contents to
  an embedding provider or hosted HLM service. Optional embedding models are
  downloaded from their model host on first use; connecting an agent to a remote
  HLM server sends its queries and any explicitly read note content to that
  server. Network access is otherwise limited to the connections you configure.

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

### Passwordless LAN mode

Without a LAN password, visitors can browse the graph only; Approvals, Settings,
token management, and write APIs remain protected. An explicit CIDR allowlist is
required. For an existing passwordless LAN installation, set the actual trusted
network range and restart:

```text
hlm config set --allowed-cidrs '["192.168.1.0/24"]'
```

To use the protected UI over LAN, configure a password with
`hlm config set-password` and sign in.

In a trusted network, insecure LAN HTTP requires an explicit acknowledgement
and no TLS files:

```text
HLM_FRONTEND__ENABLED=true HLM_FRONTEND__MODE=lan \
HLM_NETWORK__ENABLED=true HLM_NETWORK__INSECURE_HTTP=true \
HLM_FRONTEND__PUBLIC_ORIGIN=http://memory.local \
HLM_NETWORK__ALLOWED_CIDRS='["192.168.1.0/24"]' \
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

Permission metadata is mutable: `hlm token update <name> --admin --no-admin
--rule PATH=LEVEL --clear-rules --approve-own-proposals
--no-approve-own-proposals` (or `PATCH /api/v1/tokens/<name>`, admin-only)
updates rules, the admin flag, and `approve_own_proposals` in place. Omitted
options are unchanged; `--clear-rules` (mutually exclusive with `--rule`)
replaces all rules with none. The update never changes the token secret —
the existing plaintext stays valid, and the hash is only ever replaced by
revoking and creating a new token.

`approve_own_proposals` is exposed by the Web UI, the REST token APIs, and
`hlm token update`. Set it to `true` to explicitly permit that token to
approve proposals it created itself; without it, own-proposal approval is
rejected even when the token can otherwise write to the path.

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

Manage tokens with `hlm token list`, `hlm token update <name>`, and
`hlm token revoke <name>` (or the settings page). Revoking a token takes effect immediately; the service never
falls back to open mode.

### MCP clients

MCP `read_note` and `read_notes` return full content without an application byte
cap. `read_notes` takes explicit paths and returns independent per-path results;
both tools enforce path policy and report denied or missing paths as errors.

MCP is a streamable-HTTP endpoint at `/mcp`, bearer-only, and independent of
the browser UI. Enable it with `hlm config set --mcp-enabled` (or
`HLM_MCP__ENABLED=true`). For a least-privilege, read-only OpenCode token when
the configured index root is `AI`, use a named token with an explicit denied
root and one allowed folder:

```text
hlm token create --name opencode-read --rule AI=none --rule AI/Knowledge=read
```

Copy the one-time plaintext into `HLM_MCP_TOKEN` in the environment used to
launch OpenCode. In OpenCode's `opencode.json`, use the official remote
configuration exactly:

```json
{
  "mcp": {
    "servers": {
      "harbor-ledger-memory": {
        "type": "remote",
        "url": "http://127.0.0.1:8765/mcp/",
        "oauth": false,
        "enabled": true,
        "headers": { "Authorization": "Bearer {file:/path/to/opencode-token}" }
      }
    }
  }
}
```

OpenCode uses the official `mcp.servers` remote-server shape. The `{file:...}`
reference points to a protected token file managed by `hlm setup`; do not put
the token value in host configuration. Setup leaves JSONC files for a manual
merge rather than rewriting comments or formatting. In that case, add the same
entry to `mcp.servers` yourself, using the protected token file path reported
by setup.

After changing the token or server, reconnect and verify the integration:

```text
opencode mcp list
opencode mcp debug
```

If the token is exposed or no longer needed, revoke it immediately with
`hlm token revoke opencode-read`, remove `HLM_MCP_TOKEN`, and reconnect. A
revoked token receives `401`; create a replacement token rather than reusing
the old secret. This integration uses only the official remote HTTP config:
there is no OAuth flow, relay, or plugin involved.

MCP sessions are per-process: after `hlm serve` restarts, a client's cached
session goes stale (`404 Session not found`) until the client reconnects.
MCP tools and operations remain compatible, but Streamable HTTP session
lifecycle follows the Python MCP SDK 2.2.0 defaults: sessions expire after 30
minutes idle, and each session manager holds at most 10,000 sessions. After an
idle expiry, the old session ID returns `404` and the client must initialize a
new session. If the cap is reached, a new session request returns `503`. As
before, restarting `hlm serve` also invalidates cached sessions; clients must
reconnect and initialize a new session. The cap is shared across all clients, not
per token, so issue MCP bearer tokens only to trusted agents.

If MCP appears disabled to a client, check `GET /health`: the
`mcp.active_tokens` count is the number of non-revoked bearer tokens. A count of
`0` means every token has been revoked, so every `/mcp` request returns `401`
until a token is recreated with `hlm token create`.

## Development from a checkout

To run the CLI from a checkout without installing a global command:

```text
uv sync --no-dev                 # runtime dependencies only
cp .env.example .env             # edit HLM_VAULT_PATH to an existing vault
uv run --env-file .env hlm scan
uv run --env-file .env hlm query "your search"
```

These CLI commands do not need Node.js or a web build. To serve the UI from the
checkout, install development dependencies and build it:

```text
uv sync
(cd web && npm ci && npm run build)
HLM_FRONTEND__ENABLED=true HLM_FRONTEND__MODE=loopback \
  uv run --env-file .env hlm serve # then open http://127.0.0.1:8765
```

The checkout serves the UI from `web/dist`. Loopback mode does not require a UI
login. The service scans before serving, then watches admitted Markdown files
and rescans after changes; scanning and watching do not write vault files.

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
hlm doctor
hlm doctor --json
hlm doctor --url http://127.0.0.1:8765
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

`hlm doctor` is a read-only, offline-first diagnostic. It inspects configuration,
vault availability, database schema and latest scan state, validation findings,
embedding runtime, non-secret token/MCP settings, and the installed runtime.
`--json` emits a stable check report; exit code 0 means healthy, 1 means warnings,
and 2 means actionable failures. `--url` probes only local `/health` and
`/openapi.json` endpoints to identify unreachable or stale servers. Doctor never
creates or migrates a catalog, downloads models, starts a daemon, or prints
secrets.

### Rebuild and vault-change semantics

`hlm scan` and `POST /api/v1/scan` perform a full, deterministic rebuild from
the admitted Markdown scope. The catalog, embeddings, graph, and diagnostics
are derived and disposable; scan/status responses include the latest attempt,
latest successful scan, admitted index root, embedding configuration, and
compact rebuild evidence. Rebuilding never restores, backs up, or repairs vault
files, and Markdown bytes remain unchanged.

Vault writes are separate: `POST /api/v1/writes` creates a version-pinned
proposal (with a safe base hash and diff), and approval is the only path that
may apply it under the existing policy and version checks. Lifecycle marks are
also separate SQL metadata; they do not edit Markdown and are not part of a
rebuild or a vault proposal.

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

`GET /api/v1/notes/content?path=...` is bearer-authenticated and path-policy
checked; it returns `path`, `content`, and `revision`, sends `Cache-Control:
no-store`, and returns full content without an application byte cap or
truncation. `hlm read PATH...` prints one JSON object per line (JSONL). The
authenticated `POST /api/v1/notes/content/batch` accepts 1–10 explicit paths
and streams NDJSON with one result per path, including safe per-path failures;
the request body is limited to 256 KiB and each path to 4 KiB UTF-8. These are
input limits only: responses and note contents have no byte cap or truncation.
It is `no-store` and does not expand paths through search. Native agent providers use
`POST /api/v1/agent/writes` (content up to 64 KiB UTF-8, path up to 4 KiB UTF-8,
raw request up to 512 KiB) to create forced proposal-only writes, and
`GET /api/v1/agent/writes/pending` for paginated summaries only. Bounded summary
approve/reject routes are also available. These limits apply to the agent-only
routes; legacy `/api/v1/writes` and MCP behavior remains unchanged and has no
new global quota. This is not global resource-exhaustion protection.

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

- [Contributing](CONTRIBUTING.md)
- [Report a bug or request a feature](https://github.com/Idriz-hax/harbor-ledger-memory/issues)
- [Released versions](https://github.com/Idriz-hax/harbor-ledger-memory/releases)
