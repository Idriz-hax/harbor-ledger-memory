# Upgrading to HLM 2.0.0

HLM remains local-first and Markdown remains the source of truth. **No database
or vault migration is required** for 2.0. Existing web, CLI, REST, and MCP
behaviors remain compatible. The new agent-only write routes are separate from
legacy `/api/v1/writes` and MCP; they force proposals and expose bounded
summary-only agent operations, but do not impose a global quota.

## Upgrade HLM

Install or update HLM using your existing deployment procedure. Existing
configuration and vault contents need no conversion. Note-content reads require
a bearer token with read access to the requested path and return an uncached
`path`/`content`/`revision` response, capped at 1 MiB.

Loopback HTTP remains supported for local services. Configure HTTPS for any
non-loopback remote HLM service. Do not put bearer credentials in committed
configuration or examples.

## Optional native integrations

- **Hermes v0.18.1 (`v2026.7.7`):** copy the provider directory files into
  `$HERMES_HOME/plugins/hlm/`, configure the profile's `HLM_TOKEN`, and select
  the HLM provider. See the [Hermes guide](../integrations/hermes/README.md).
  Chat approval is optional and requires `trust: untrusted` MCP with the same
  creator token; otherwise approve through HLM's web UI.
- **OpenClaw 2026.10.5:** install
  `harbor-ledger-memory-openclaw@2.0.0`, configure a secret-backed token, and
  select it in the memory slot. Chat approval uses one-shot `allow-once`/deny;
  use the web review link and instructions if host approval is unavailable. See
  the [OpenClaw guide](../integrations/openclaw/README.md).

Both providers use bounded agent proposal routes and do not silently fall back
to another memory authority. Their optional setup does not affect existing HLM
clients.
