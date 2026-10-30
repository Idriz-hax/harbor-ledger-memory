---
name: harbor-ledger-memory
description: Use when retrieving or updating Harbor Ledger Memory, a neural memory service for AI agents, through the harbor-ledger-memory MCP.
---

# Harbor Ledger Memory MCP

Read tools are `status` (catalog health and policy), `settings_snapshot`
(active configuration), `query` (relevant context), `neighbours` (link graph),
and `scan` (rebuild the catalog after vault changes). These operations honor
the configured vault scope.

## Retrieve full notes

Search with `query`, then read only the paths you select from its results using
`read_note` for one note or `read_notes` for up to 10 explicit paths. Batch
results are independent and returned in request order; the service never
automatically expands search results or related paths.

## Query feedback

When querying, pass `active_project` as a vault-relative POSIX path when possible.
Absolute paths inside the configured vault are normalized; external absolute
paths are reduced to their final directory name. Empty or malformed hints are
ignored.

Retain the `trace_id` returned by `query`. Use `feedback` with
`relevant_paths` and/or `irrelevant_paths` to submit judgments for selected,
admitted paths only:

```json
{
  "trace_id": "<query trace_id>",
  "relevant_paths": ["AI/Knowledge/example.md"],
  "irrelevant_paths": []
}
```

Feedback adjusts retrieval behavior and does not write vault files. Vault
updates remain subject to the write policy below.

## Write lifecycle

Writes are policy-controlled. Call `propose_write` with a vault-relative path
and content; inspect the returned status before taking another action:

- Under `auto-write`, conflict-free updates and creates apply immediately.
- Under `propose-write`, the write remains pending approval.
- A version conflict may leave an `auto-write` request pending for review.

Only call `approve_proposal` or `reject_proposal` when the returned status is
`pending` and the caller is authorized to resolve it. Do not attempt approval
for a write that already applied.

The default access is `read`: default, `read`, and `deny` paths cannot be
written. Folder rules apply to matching paths; the most specific matching rule
determines access, while paths without a matching rule retain the read-only
default.

After upgrading the server, clients must disconnect and reconnect before using
the MCP tools so the new tool and transport definitions are loaded.

## OpenCode remote connection

Create a named, read-only token scoped to the allowed folder (with `AI` as the
configured index root):

```text
hlm token create --name opencode-read --rule AI=none --rule AI/Knowledge=read
```

After MCP is enabled, `hlm setup` offers OpenCode, OpenClaw, Hermes, or skip.
For a host integration, setup requires a token name and at least one
vault-relative folder to scope the token. OpenCode config is automated when
safe; JSONC or unsafe-to-edit configs need a manual merge. If OpenCode setup
fails, setup displays the token once for manual merge, or revoke it with
`hlm token revoke NAME`. OpenClaw config and its `HLM_TOKEN` secret stay manual;
Hermes provider files/profile `base_url` are configured by setup, but
`HLM_TOKEN` must be saved in Hermes' profile secret store. No plaintext token
goes into host config. Use the official remote-server shape in `opencode.json`:

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

Replace the file path with the protected token file path reported by setup.
Never put the plaintext token in host config. Manage or revoke access with
`hlm token` or the HLM UI.

After changing the token or server, run `opencode mcp list` and
`opencode mcp debug`, then reconnect. Revoke exposed or retired tokens with
`hlm token revoke opencode-read`; there is no OAuth, relay, or plugin involved.
