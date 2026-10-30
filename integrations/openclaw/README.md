# Harbor Ledger Memory OpenClaw plugin

Requires OpenClaw **2026.10.5**. From this directory, package and install:

For guided host setup, enable MCP in `hlm setup`, choose OpenClaw, then provide a
token name and at least one folder to scope its read access. Setup prints the
plugin install command and configuration to merge; you must install the plugin,
merge the configuration, and add the one-time `HLM_TOKEN` to OpenClaw's
configured default environment-backed secret provider. HLM cannot create that
host secret. Do not put the token value in JSON5. Manage or revoke the token
later with `hlm token` or the HLM UI. The manual steps below remain available
for standalone installation.

```sh
PACK_DIR="${TMPDIR:-/tmp}"
npm pack --pack-destination "$PACK_DIR"
openclaw plugins install "npm-pack:$PACK_DIR/harbor-ledger-memory-openclaw-2.0.0.tgz" --force
```

Enable the plugin and select it as the exclusive memory provider in OpenClaw configuration:

```json5
{
  plugins: {
    allow: ["harbor-ledger-memory-openclaw"],
    entries: { "harbor-ledger-memory-openclaw": { enabled: true, config: {
      baseUrl: "http://127.0.0.1:8765",
      token: { source: "env", provider: "default", id: "HLM_TOKEN" },
      // Optional; defaults to baseUrl.
      reviewUrl: "https://memory.example/",
      // Optional; defaults to 8 MiB. Maximum 256 MiB.
      maxResponseBytes: 8388608
    } } },
    slots: { memory: "harbor-ledger-memory-openclaw" }
  }
}
```

The example resolves `HLM_TOKEN` through OpenClaw's default environment-backed
secret provider. Make the secret available to the OpenClaw process using the
host's secret-management setup; do not put its literal value in configuration
committed to source control. For a remote HLM service, set `baseUrl` to its
HTTPS origin and use a token with the desired HLM scope. Selecting this memory
slot replaces `memory-core`; HLM failures are surfaced rather than silently
falling back.

Configure `baseUrl` for the local or authenticated remote HLM service, `token` using OpenClaw's secret-backed plugin configuration (never put it in source/config examples), and optionally `reviewUrl` and `maxResponseBytes` (positive integer, at most 268435456/256 MiB; default 8388608/8 MiB). The limit applies to every response body, including single and batch note content; oversized bodies are rejected and cancelled. Requests use bearer auth and a 10-second timeout. Search sends only the query; note content is fetched on demand and is neither cached nor truncated. HLM enforces token/path policy. Alongside the provider single-note get tool, `hlm_get_notes` calls `POST /api/v1/notes/content/batch` with 1–10 explicit paths and returns an independent result per path. Notes are full and untruncated; paths are not inferred from search.

`hlm_propose_write` and `hlm_propose_folder` use only the bounded `/api/v1/agent/writes` API and always create pending proposals. The plugin enforces the 4 KiB UTF-8 path, 64 KiB UTF-8 content, 512 KiB serialized request, and the configured response limit for all JSON and NDJSON bodies. Write, pending, and resolution results expose only the summary fields `id`, `path`, `operation`, `status`, and `requested_at`; proposal content and diffs are never returned. Pending results use the paginated `/api/v1/agent/writes/pending` endpoint and preserve its next cursor. They also include `reviewUrl` (configured override or HLM base URL) and `reviewInstructions: "Open the HLM web UI and select Approvals in the navigation."` Approve/reject accept numeric proposal IDs and use `/api/v1/agent/writes/{id}/approve|reject`; they invoke OpenClaw's `before_tool_call` approval gate, requesting only `allow-once` or `deny`. Timeout or denial blocks execution. If the host cannot provide the approval route, do not resolve in chat—use the web UI navigation above. Legacy `/api/v1/writes` routes remain available in HLM but this provider never calls them.
