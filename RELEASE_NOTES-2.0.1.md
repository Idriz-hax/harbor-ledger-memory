# Harbor Ledger Memory 2.0.1

**Released 2026-10-27 — GitHub-only follow-up; no PyPI publish.**

This release raises the `certifi` minimum to `>=2026.10.19` for the critical
emSign certificate bundle issue (the lock resolves `2026.10.23`) and
`opentelemetry-api` to `>=1.45.1`. The optional `embeddings` extra now requires
`filelock>=4.2.1` and `setuptools>=84.1.0`.

No database or vault migration is required. MCP SDK remains pinned to 2.2.0;
its documented defaults remain a 30-minute idle timeout and a 10,000-session
cap.

The OpenClaw plugin's production-only npm audit is clean, but its full
development dependency tree still reports seven alerts, including unpatched
`http-cache-semantics` and pinned-host transitive findings. The pinned OpenClaw
host remains `2026.10.5`. This does not mean every repository dependency alert
is resolved.
