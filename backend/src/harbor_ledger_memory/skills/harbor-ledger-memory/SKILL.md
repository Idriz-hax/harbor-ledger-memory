---
name: harbor-ledger-memory
description: Use when retrieving or updating Harbor Ledger Memory, a neural memory service for AI agents, through the harbor-ledger-memory MCP.
---

# Harbor Ledger Memory MCP

Read tools are `status` (catalog health and policy), `settings_snapshot`
(active configuration), `query` (relevant context), `neighbours` (link graph),
and `scan` (rebuild the catalog after vault changes). These operations honor
the configured vault scope.

## Query feedback

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
updates remain subject to the write lifecycle below; eligible managed updates
under `auto-write` can apply immediately.

## Write lifecycle

Writes are policy-controlled and proposal-based:

1. Call `propose_write` with a vault-relative path and content.
2. Call `approve_proposal` with the returned proposal ID to apply it, or
   `reject_proposal` to decline it.

The default access is `read`: default, `read`, and `deny` paths cannot be
written. A `propose-write` folder rule creates a pending approval, which must
then be resolved with `approve_proposal` or `reject_proposal`. Eligible managed
updates under `auto-write` can apply immediately. Folder rules apply to
matching paths; the most specific matching rule determines access, while paths
without a matching rule retain the read-only default.

After upgrading the server, clients must disconnect and reconnect before using
the MCP tools so the new tool and transport definitions are loaded.
