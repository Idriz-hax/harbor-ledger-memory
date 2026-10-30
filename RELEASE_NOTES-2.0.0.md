# Harbor Ledger Memory 2.0.0

**2026-10-27**

HLM 2.0 brings native shared-memory integrations for Hermes and OpenClaw while
keeping the existing local-first product intact. Markdown remains the source of
truth; the database is a rebuildable projection. Existing web, CLI, REST, and
MCP tools and operations remain compatible, and upgrading requires no database
or vault migration. The MCP Streamable HTTP session lifecycle now follows the
Python MCP SDK 2.2.0 defaults: sessions expire after 30 minutes idle, with a
10,000-session cap per session manager. An expired session ID returns `404` and
the client must initialize a new session; at capacity, a new session request
returns `503`. The cap is shared across MCP clients, so issue MCP bearer tokens
only to trusted agents.

## What's new

- `GET /api/v1/notes/content?path=...` provides bearer-authenticated,
  path-policy-checked note reads. Responses contain `path`, `content`, and
  `revision`, use `Cache-Control: no-store`, and content is capped at 1 MiB.
- Agent-only `POST /api/v1/agent/writes` accepts at most 64 KiB UTF-8 content,
  a 4 KiB UTF-8 path, and a 512 KiB raw request, and always creates a proposal.
  `GET /api/v1/agent/writes/pending` returns paginated summaries only; bounded
  summary approve/reject endpoints are also available. These write constraints
  do not change legacy `/api/v1/writes` or MCP tool operations, and do not add a
  global write quota.
- Hermes provider is a directory plugin copied to
  `$HERMES_HOME/plugins/hlm/`, tested with Hermes v0.18.1 (`v2026.7.7`). It uses
  profile-scoped `HLM_TOKEN`. Optional chat approval requires `trust: untrusted`
  MCP using the same creator token; otherwise review proposals in the web UI.
- OpenClaw provider is `harbor-ledger-memory-openclaw@2.0.0`, verified against
  the pinned experimental plugin SDK 2026.10.5 and selected through its memory
  slot. Chat approval requests one-shot `allow-once` or deny; if unavailable,
  use the web review link and instructions.

The HLM service supports loopback HTTP. Remote services on non-loopback
addresses require HTTPS. Never put credentials in integration configuration
examples or source control.

## Setup

See the [README quickstart](README.md#quickstart),
[Hermes provider guide](integrations/hermes/README.md), and
[OpenClaw plugin guide](integrations/openclaw/README.md).
