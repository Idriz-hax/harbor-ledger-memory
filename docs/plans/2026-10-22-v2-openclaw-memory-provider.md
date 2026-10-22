# OpenClaw Shared Memory Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make HLM the selected OpenClaw long-term memory provider and route memory access and proposals through the shared HLM service.

**Architecture:** Add a separate TypeScript plugin package using OpenClaw's exclusive memory slot and `providerRuntime`; use HLM REST for query, note get, and proposals. Provider references are identifiers only, never authorization. The HLM note-content API plan is a prerequisite.

**Tech Stack:** TypeScript, OpenClaw plugin SDK, Node.js, built-in test runner via `tsx`.

---

## File map

- Create `integrations/openclaw/package.json` (`name: harbor-ledger-memory-openclaw`, `version: 2.0.0`), `openclaw.plugin.json`, and `tsconfig.json` for a standalone package pinned to the selected tested OpenClaw SDK version. Define scripts `test: tsx --test test/*.test.ts` and `typecheck: tsc --noEmit`.
- Create `integrations/openclaw/src/index.ts` for plugin registration and `src/client.ts` for authenticated HLM REST calls.
- Create `integrations/openclaw/test/client.test.ts` and `test/plugin.test.ts`; create `integrations/openclaw/README.md` for install, slot selection, configuration, permissions, and approval behavior.

## Task 1: Pin SDK contract and specify failing tests

- [ ] Pin one OpenClaw release in dev/test dependencies and declare the compatible peer range; verify the exported `MemoryPluginCapability`, `providerRuntime`, `registerMemoryCapability`, `registerTool`, and `before_tool_call` signatures against that release.
- [ ] Add client tests with mocked `fetch`: send bearer auth; map query to `POST /api/v1/queries`, read to `GET /api/v1/notes/content`, propose/list to `POST/GET /api/v1/writes`; non-2xx and malformed JSON must reject, never return empty success.
- [ ] Add registration tests asserting `plugins.slots.memory` capability exports `search`, `get`, `health`, and `close`, and registers `hlm_propose_write`, `hlm_propose_folder`, `hlm_pending_proposals`, `hlm_approve_proposal`, and `hlm_reject_proposal` tools.
- [ ] Run `npm test`; confirm the tests fail before implementation.

## Task 2: Implement client and native memory runtime

- [ ] Implement `HlmClient` with configured local/remote base URL, bearer token, `AbortSignal.timeout(10_000)`, and explicit error mapping; do not log credentials or cache note content.
- [ ] Set `openclaw.plugin.json` fields `id`, `kind: "memory"`, `configSchema`, and `contracts.tools`; register with the supported package entrypoint and read secrets through the host's supported configuration mechanism.
- [ ] Implement the provider using SDK-derived types, not guessed local interfaces:

```ts
type ProviderRuntime = NonNullable<MemoryPluginCapability["providerRuntime"]>;
const providerRuntime: ProviderRuntime = createProviderRuntime(client, config);
api.registerMemoryCapability({ providerRuntime });
```

`createProviderRuntime(client, config)` returns the SDK-typed object with `open`, `search`, `get`, `health`, and `close`; define that factory and its request/result conversions in `src/index.ts`.

- [ ] `search` maps HLM query results to provider-scoped references (`hlm:${encodeURIComponent(path)}`) and excerpts; `get` requires the `hlm:` prefix, decodes once, calls the policy-checked note endpoint, and returns explicit success/not-found/failure. Never substitute another result, silently truncate at host byte bounds, or convert denial to empty success.
- [ ] `health` distinguishes ready/degraded/unavailable without leaking memory content. Return no session/transcript hits; no `authorizeSearchHits` is needed unless that capability is added explicitly.
- [ ] Register proposal, pending-list, approve, and reject tools. Proposal creation only queues HLM proposals. Add a `before_tool_call` hook requiring host approval for every approve/reject call, allow only `allow-once`/deny, and call HLM only after allow-once. Reject/timeout/no approval route must not call HLM; pending-list results always include the web review route. No alternate unguarded resolution path.

## Task 3: Test host runtime, package, and security

- [ ] Run `npm test` and `npm run typecheck`; verify local and remote HLM URLs, Unicode/space paths, foreign references, oversized notes, 401/404/413/422, and proposal status.
- [ ] Run `npm pack --pack-destination /tmp` and install the generated 2.0.0 tarball into the pinned OpenClaw test environment with `openclaw plugins install npm-pack:/tmp/harbor-ledger-memory-openclaw-2.0.0.tgz --force`.
- [ ] Run `openclaw plugins inspect harbor-ledger-memory-openclaw --runtime --json`; verify selecting `plugins.slots.memory` disables `memory-core` as the competing provider and HLM errors do not silently fall back to it.
- [ ] Test `before_tool_call`: allow-once triggers exactly one HLM approve/reject request; deny, timeout, cancellation, and absent approval route trigger zero requests. Confirm HLM still enforces token/path scope.
- [ ] Document tested OpenClaw version, install/config/secret commands, service modes, tool behavior, and web review fallback in `integrations/openclaw/README.md`. Commit as `feat: add OpenClaw shared memory provider`.
