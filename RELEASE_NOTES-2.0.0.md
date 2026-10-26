# Harbor Ledger Memory 2.0.0

**2026-10-27**

HLM 2.0 brings native shared-memory integrations for Hermes and OpenClaw while
keeping the existing local-first product intact. Markdown remains the source of
truth; the database is a rebuildable projection. Existing web, CLI, REST, and
MCP behavior remains compatible, and upgrading requires no database or vault
migration.

## What's new

- `GET /api/v1/notes/content?path=...` provides bearer-authenticated,
  path-policy-checked note reads. Responses contain `path`, `content`, and
  `revision`, use `Cache-Control: no-store`, and content is capped at 1 MiB.
- Agent-only `POST /api/v1/agent/writes` accepts at most 64 KiB UTF-8 content,
  a 4 KiB UTF-8 path, and a 512 KiB raw request, and always creates a proposal.
  `GET /api/v1/agent/writes/pending` returns paginated summaries only; bounded
  summary approve/reject endpoints are also available. These constraints do not
  add a global quota: legacy `/api/v1/writes` and MCP remain unchanged.
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

See the [upgrade guide](docs/upgrading-to-2.0.0.md),
[Hermes provider guide](integrations/hermes/README.md), and
[OpenClaw plugin guide](integrations/openclaw/README.md).
