# Hermes provider reliability and automatic recall design

**Status:** Proposed for review

**Date:** 2026-11-13

## Goal

Verify and harden HLM's existing Hermes memory provider against the currently
documented supported release, Hermes v0.18.1 (`v2026.7.7`), so profile setup,
provider initialization, and pre-turn recall behave predictably without
blocking a Hermes session when HLM is unavailable.

## Scope

- Target only Hermes v0.18.1 for this milestone. Use provider stubs for the
  normal test suite and an isolated real host-loader/`MemoryManager` test when
  the pinned host package is available.
- Test profile-scoped configuration and `HLM_TOKEN` loading, provider
  initialization, prefetch success/empty/failure paths, and provider shutdown.
- Verify from the tagged host that initialization and prefetch exceptions are
  non-fatal to the conversation, and that a failed provider does not leak
  credentials or context between profiles.
- Keep `is_available()` network-free. Do not add a remote health request during
  provider discovery or startup. First establish its exact call order and
  semantics in the pinned host before changing its implementation.
- Prefetch uses the actual request query passed by v0.18.1. That release also
  passes a session ID, but does not provide project/workspace context to
  `prefetch`; do not infer project context or broaden access from the session
  ID.
- Keep returned context bounded by HLM's existing selected-memory and excerpt
  limits, with each result attributed to its source path. Add a provider-side
  cap only if tests show the existing limits do not bound the rendered result.
- Document the supported release and observed lifecycle/error behavior.

## Safety and compatibility

- Do not install, upgrade, disable, or cut over the user's live Hermes profile.
- Run any real-host test with an isolated temporary Hermes home and fake HLM
  endpoint/client; do not contact a live HLM service or use real credentials.
- Missing configuration, invalid profile credentials, unavailable HLM,
  malformed/empty results, or provider exceptions must not block the Hermes
  conversation. They may omit memory context and should produce diagnosable
  local test/log output without exposing token values.
- Preserve profile-scoped secret lookup and HLM token folder ACLs as the
  authorization boundary. No request field or project/session label grants
  additional access.

## Out of scope

- Supporting additional Hermes versions or adopting unpinned current provider
  APIs.
- Automatic conversation capture, turn/session-end hooks, or new memory writes;
  these belong to Stage 3 and remain proposal-gated.
- Changes to HLM authorization policy, token scope, database schema, or live
  profile configuration.

## Acceptance checks

1. Stub tests cover clean initialization, missing/invalid profile setup,
   profile-secret separation, query prefetch, source attribution, empty result,
   large result, endpoint/network failure, and shutdown.
2. A pinned v0.18.1 host-loader test verifies the real provider discovery and
   manager lifecycle, including that initialization/prefetch failure is
   non-fatal and empty context is omitted.
3. The test proves `is_available()` performs no network request and records the
   actual v0.18.1 ordering/meaning of the availability hook.
4. No project/workspace context is assumed for v0.18.1 prefetch; actual user
   query text is passed through unchanged, within the existing HLM output
   limits.
5. The regular integration suite passes without Hermes installed; the pinned
   real-host test is clearly marked/skipped when the optional host package is
   unavailable.
6. Documentation describes tested support and distinguishes verified
   v0.18.1 behavior from newer, unpinned Hermes APIs.
