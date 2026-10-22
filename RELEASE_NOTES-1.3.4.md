# Harbor Ledger Memory 1.3.4

Released 2026-10-22.

## Fixed

- Queries no longer fail when `active_project` is supplied as an absolute
  filesystem path. Paths inside the configured vault are converted to
  vault-relative hints; paths outside it fall back to the final directory name.
  Empty or malformed hints are ignored. Vault-relative paths remain preferred.
