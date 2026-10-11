# Harbor Ledger Memory 1.3.3

Released 2026-10-11.

## Breaking: passwordless LAN is graph-only

Passwordless LAN visitors can browse the read-only graph, but cannot access
Approvals, Settings, token management, or write APIs. Passwordless LAN mode now
requires an explicit `network.allowed_cidrs` allowlist; startup rejects the
configuration if it is empty. Configure the allowlist before upgrading an
existing passwordless LAN installation. To use the protected UI, configure a
LAN password and sign in. See [Passwordless LAN mode](README.md#passwordless-lan-mode).

## Security

- Unauthenticated LAN visitors no longer receive a privileged UI session.
- LAN health requests honor the configured CIDR allowlist.

## Write lifecycle

- `AUTO_WRITE` creates new files and folders immediately when the target and
  every affected missing parent grant auto-write. A `PROPOSE_WRITE` parent still
  requires approval.
- Proposals left in `applying` after a process interruption are shown as
  `reconciliation_required`. The service does not guess whether the filesystem
  operation completed; inspect the vault before resolving one.

## Reliability

- Settings and Trust summary display retryable errors instead of empty/loading
  states.
