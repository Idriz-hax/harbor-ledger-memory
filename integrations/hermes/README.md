# Hermes HLM memory provider

Supported Hermes release: **v0.18.1 (`v2026.7.7`)**. The host supports user-directory providers in `$HERMES_HOME/plugins/<name>/`; this integration deliberately does not use an unverified Python entry point. It uses Hermes profile-scoped `get_secret("HLM_TOKEN")` and the stdlib HTTP client only.

## Install

With Hermes installed and `HERMES_HOME` set to the intended profile directory:

```sh
mkdir -p "$HERMES_HOME/plugins/hlm"
cp integrations/hermes/hlm/__init__.py integrations/hermes/hlm/client.py "$HERMES_HOME/plugins/hlm/"
```

Configure the profile's Hermes secret `HLM_TOKEN` (never put the value in config or logs). Create `$HERMES_HOME/plugins/hlm/config.json`:

```json
{
  "base_url": "http://127.0.0.1:8765"
}
```

Each profile reads its own config and Hermes secret scope. If `review_url` is omitted, pending proposal results link to that profile's configured `base_url` and include the instruction: “Open HLM and select Approvals in the navigation.” The default service URL is `http://127.0.0.1:8765`.

Use a locally running HLM service for local setups. Plain HTTP is accepted only for `localhost` and IP loopback addresses; remote HLM must use HTTPS. Base URLs containing user information, query strings, or fragments are rejected. The provider refuses HTTP redirects so an Authorization header cannot be forwarded to another host. For remote HLM, set `base_url` to its HTTPS URL and use the same profile-scoped token. That token grants only the read/proposal permissions assigned by HLM; provider writes are proposals, never direct vault mutation. Network/auth/API failures are surfaced as tool failures; the provider does not substitute Hermes' other long-term memory.

## Review and optional chat approval

The provider uses `POST /api/v1/queries`, `GET /api/v1/notes/content`, `POST /api/v1/agent/writes`, and summary-only paginated `GET /api/v1/agent/writes/pending` (`limit` defaults to 20, with optional `before_id`). Proposal JSON is capped at 512KiB, content at 64KiB UTF-8, paths at 4KiB UTF-8; responses are capped at 8MiB. `hlm_pending_proposals` includes the configured review URL and navigation instruction; HLM's web UI remains the safe fallback for all approval. The provider exposes no approval/rejection tools. Any optional gated chat resolution must go through host-gated routes, use the same `HLM_TOKEN` identity that created the proposals, and have HLM's `approve_own_proposals` enabled for that token; optional legacy MCP chat approval is separate from the bounded provider API.

The provider performs query-only prefetch and does not ingest conversation turns. Hermes' selected HLM provider is the shared long-term memory source; configure the host not to treat a separate long-term store as a fallback authority.

## Development

From the HLM repository root, run fast tests using host stubs:

```sh
uv run pytest integrations/hermes/tests -q
```

Run the pinned real-host loader/`MemoryManager` integration test with the released Hermes package:

```sh
uv run --with hermes-agent==0.18.1 pytest integrations/hermes/tests -q
```

The second command installs Hermes only in uv's temporary environment; Hermes is not a runtime dependency of the provider directory. Run `uv run ruff check integrations/hermes` and `uv run pyright integrations/hermes` for lint and type checks.
