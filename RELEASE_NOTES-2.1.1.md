# Harbor Ledger Memory 2.1.1

## Added

- Move proposals preview and apply affected-link rewrites after approval. Delete proposals are version-checked and always require approval.
- OpenClaw's global `maxResponseBytes` setting configures the maximum response body size from 1 byte to 256 MiB; the default remains 8 MiB.

## Changed

- Conflict-free updates to any note in an `auto-write` folder now apply immediately, even if the note has no HLM ownership marker. `propose-write` remains approval-gated, and version conflicts remain pending.
- Re-running the source installer upgrades an existing `uv` tool installation.
