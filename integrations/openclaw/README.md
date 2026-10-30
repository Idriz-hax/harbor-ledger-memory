# Harbor Ledger Memory OpenClaw plugin

Requires OpenClaw **2026.10.5**. From this directory, package and install:

For guided host setup, run `hlm setup` and choose OpenClaw. It prints the
configuration to merge and reminds you to store `HLM_TOKEN` in OpenClaw's secret
store; neither the config nor setup writes a plaintext token. You can manage or
revoke the token later with `hlm token` or the HLM UI. The manual steps below
remain available for standalone installation.

```sh
npm pack --pack-destination /tmp
openclaw plugins install npm-pack:/tmp/harbor-ledger-memory-openclaw-2.0.0.tgz --force
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
      reviewUrl: "https://memory.example/"
    } } },
    slots: { memory: "harbor-ledger-memory-openclaw" }
  }
}
```

Use a secret-backed OpenClaw token reference for `token`; never store a literal credential in configuration committed to source control. For a remote HLM service, set `baseUrl` to its HTTPS origin and use a token with the desired HLM scope. Selecting this memory slot replaces `memory-core`; HLM failures are surfaced rather than silently falling back.

Configure `baseUrl` for the local or authenticated remote HLM service, `token` using OpenClaw's secret-backed plugin configuration (never put it in source/config examples), and optionally `reviewUrl`. Requests use bearer auth and a 10-second timeout. Search sends only the query; note content is fetched on demand and is neither cached nor truncated. HLM enforces token/path policy. Alongside the provider single-note get tool, `hlm_get_notes` calls `POST /api/v1/notes/content/batch` with 1–10 explicit paths and returns an independent result per path. Notes are full and untruncated; paths are not inferred from search.

`hlm_propose_write` and `hlm_propose_folder` use only the bounded `/api/v1/agent/writes` API and always create pending proposals. The plugin enforces the 4 KiB UTF-8 path, 64 KiB UTF-8 content, 512 KiB serialized request, and 8 MiB response limits for other JSON endpoints; streamed note content is exempt. Write, pending, and resolution results expose only the summary fields `id`, `path`, `operation`, `status`, and `requested_at`; proposal content and diffs are never returned. Pending results use the paginated `/api/v1/agent/writes/pending` endpoint and preserve its next cursor. They also include `reviewUrl` (configured override or HLM base URL) and `reviewInstructions: "Open the HLM web UI and select Approvals in the navigation."` Approve/reject accept numeric proposal IDs and use `/api/v1/agent/writes/{id}/approve|reject`; they invoke OpenClaw's `before_tool_call` approval gate, requesting only `allow-once` or `deny`. Timeout or denial blocks execution. If the host cannot provide the approval route, do not resolve in chat—use the web UI navigation above. Legacy `/api/v1/writes` routes remain available in HLM but this provider never calls them.
