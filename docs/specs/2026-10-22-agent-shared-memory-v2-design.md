# Harbor Ledger Memory 2.0: Shared Agent Memory Design

**Status:** Approved by user
**Date:** 2026-10-22

## Summary

Harbor Ledger Memory (HLM) 2.0 will let HLM serve as the shared, authoritative memory for the user's existing workflows and external AI agents, initially Hermes and OpenClaw. HLM's current vault, retrieval, graph, policy, approvals, and web interface remain the product foundation. Native integrations make HLM selectable as each host's memory provider rather than merely adding another disconnected memory tool. Apps and agents that support MCP can continue to connect through the existing MCP surface.

The Markdown vault remains the source of truth. Agent-originated writes use HLM's existing proposal and approval lifecycle. Users can review in the web UI; integrations should use host-mediated chat approval when it is supported safely and otherwise provide a direct route to the web review UI.

## Problem and product goal

Today HLM provides useful memory through its own interface, CLI, REST, and MCP. An agent that connects only as an MCP client can call HLM tools while still using its own separate native long-term memory. That produces parallel stores and prevents all authorized apps and agents from sharing one coherent memory.

HLM 2.0 should preserve the current product while adding native Hermes and OpenClaw memory-provider integrations, so HLM can be selected as their shared memory backend. The memory remains inspectable and portable as Markdown, and the existing web interface remains available for administration and approval.

## Goals

- Preserve current HLM behavior and existing users' vaults, retrieval, graph, policy, web UI, CLI, REST, and MCP surfaces.
- Make one HLM instance/vault the shared memory for existing HLM workflows, Hermes, OpenClaw, and other MCP-capable apps.
- Provide native provider integrations for Hermes and OpenClaw, using each host's supported memory extension point rather than MCP tools alone.
- Support a locally running HLM service and a remotely hosted HLM service; this is a client-to-service relationship, not in-process embedding of HLM internals.
- Give agents read/query and context-retrieval capabilities, graph/context inspection, retrieval feedback/marks, and proposal-based memory/folder writes through shared HLM behavior.
- Let agents surface pending proposals to the user. Preserve web approval and use safe host-mediated chat approval when available.
- Provide a bounded write/list/resolve API for native agent integrations, without changing the existing web, REST, or MCP write contracts.
- Keep Markdown as the durable, human-readable, portable source of truth.
- Document installation, configuration, supported host versions, security, and troubleshooting for both integrations.

## Non-goals

- Replacing HLM's current web/CLI experience or requiring use of an agent host.
- Creating an HLM-hosted SaaS service as part of this release.
- Embedding Python HLM services directly inside Hermes or OpenClaw processes.
- Maintaining a separate HLM memory database or storage model for each agent.
- Allowing agents to bypass HLM access policies or write directly to vault files.
- Narrowing the existing `/api/v1/writes` or MCP tool contracts; the bounded limits apply to the new native-agent routes only.
- Automatically ingesting every conversation without an explicit host integration policy and user consent.
- Requiring chat approval when a host/runtime cannot reliably establish user-confirmed authorization; web approval remains available.

## Product and architecture boundaries

1. **HLM core remains authoritative.** Vault content and existing shared services own storage, indexing, retrieval, graph context, access decisions, proposals, and approval state.
2. **Native providers are thin host adapters.** Hermes uses its memory-provider extension point; OpenClaw uses its memory-plugin slot. They adapt host lifecycle events and tools to HLM operations rather than reimplementing memory logic.
3. **MCP remains the agent-neutral tool surface.** Existing MCP tools remain available for general agent/app access. The native providers may reuse that surface where it fits their lifecycle; only demonstrable provider gaps should cause additions to the shared contract.
4. **Local and remote deployments share semantics.** Providers connect to a locally running HLM service or an authenticated remote HLM service. Both paths use the same authorization and proposal rules. Local mode does not mean direct unrestricted file access.
5. **No duplicated authority.** HLM is the selected long-term memory source for a configured host. Host-specific transient state may remain where the host requires it, but integrations must not silently maintain a second independent long-term store that can conflict with HLM.

## Agent behavior

The shared agent memory contract should cover the following existing HLM capabilities, exposed through the smallest sufficient tool set:

- Query/retrieve relevant memories with the normal HLM query context and scope.
- Inspect linked memory context where useful, including neighbors/graph context.
- Submit feedback/marks about retrieval so HLM can preserve its existing learning and lifecycle behavior.
- Submit proposed memory and folder creation through HLM's proposal service.
- Check a proposal's pending/review state and provide a usable review route.
- Approve or reject a proposal only through an authorized user-confirmed flow; a normal agent response is not itself approval.

Host adapters must use the same user/token policy and scope rules as other HLM clients. Integration setup must make clear which HLM identity and vault the agent can access. Agent write access is proposal access, not direct vault mutation access.

## Bounded native-agent write contract

Native Hermes/OpenClaw providers use additive, agent-specific routes so model-originated proposal traffic has bounded resource use while existing web, REST, CLI, and MCP behavior remains compatible:

- `POST /api/v1/agent/writes` accepts the existing `{path, content, operation}` shape, but caps raw JSON at **512 KiB**, UTF-8 content at **64 KiB**, and the path at **4 KiB**. The raw body cap is enforced while streaming and before JSON parsing. Agent writes always create proposals, even if a token would auto-apply through the legacy write route.
- `GET /api/v1/agent/writes/pending?limit=20&before_id=...` returns a deterministic, paginated summary of pending proposals only. `limit` defaults to 20 and is capped at 100. Summaries omit content, diff, and failure details; unreadable paths are never returned.
- `POST /api/v1/agent/writes/{id}/approve` and `/reject` return proposal summaries only. They re-check path policy and require the active token to have `approve_own_proposals` enabled and to be the proposal creator; host adapters still require explicit user confirmation before invoking these routes.
- Provider clients cap request content before serialization and cap response bytes before JSON parsing. A note read is limited by the existing 1 MiB note-content endpoint; provider response caps allow its JSON encoding overhead but remain finite.

The legacy `/api/v1/writes` endpoints and existing MCP tool contracts remain unchanged for existing clients. The bounded agent routes protect the native providers when they use this contract; they are not a claim that legacy authenticated routes have a global resource quota.

## Approval and notification flow

1. The agent submits a proposal using HLM's existing guarded write path.
2. HLM returns a stable proposal identifier and pending state.
3. The adapter tells the user that review is required and identifies the proposed change.
4. If the host exposes a trustworthy confirmation mechanism, the adapter may offer approve/reject actions bound to that specific proposal and the active user's authority.
5. If the host cannot establish a safe user-confirmed action, the adapter gives the user a link/instruction to review and approve or reject in the HLM web UI.
6. HLM remains responsible for enforcing authorization and validating the proposal transition regardless of which interface initiated it.

Approvals must not be inferred from an agent's tool invocation, an untrusted message, or an unattended-session default. The web UI remains a supported review path in all configurations.

## Installation and deployment

- Provide a native, documented Hermes provider installation and configuration path.
- Provide a native, documented OpenClaw memory-plugin installation and configuration path.
- Each integration can connect to a local HLM service or a remote HLM service using the supported authentication mechanism.
- Provide setup guidance for selecting HLM as the host's memory provider and preventing a parallel host long-term-memory store from remaining an undisclosed competing source.
- Keep existing HLM network defaults and access-policy safeguards; integrations must not silently enable public network access.
- Publish supported Hermes/OpenClaw version ranges and test them against the documented extension APIs, which can change independently of HLM.
- Retain MCP setup for other compatible agents and apps; native providers are additions, not replacements for HLM's generic integration.

## Compatibility and failure behavior

- If HLM is temporarily unreachable, the adapter must report that shared memory is unavailable rather than silently treating a separate host store as authoritative.
- Read/query failures must be surfaced as failures, not as a successful empty-memory result.
- Failed proposal submission must not imply that a proposal is pending or approved.
- Reconnection must not duplicate a proposal or silently replay a write without idempotent handling or a user-visible confirmation.
- Unsupported or unauthenticated approval actions must fail closed and offer the web review route where possible.
- Installing or enabling a provider must not migrate, delete, or overwrite the user's existing HLM vault. Host-native memory migration is outside this design unless required by a host's selection mechanism; any such operation needs explicit opt-in and a separate migration plan.
- HLM's existing interfaces and clients remain compatible; version 2.0 additions should be additive unless a separately reviewed compatibility change is necessary.

## Security and privacy requirements

- Never expose direct filesystem mutation as an agent capability.
- Apply the same token scopes, folder rules, and policy checks as existing HLM APIs.
- Store provider credentials using each host's supported secret/configuration mechanism; do not include credentials in logs or example configuration.
- Require authenticated remote connections and preserve HLM's existing network-access safeguards.
- Bound chat approval to an explicit proposal and a verified user-confirmed host action. Do not treat an agent assertion or arbitrary chat text as authorization.
- Clearly document what conversation/context data is sent to HLM and what remains host-local.

## Validation criteria

The 2.0 implementation is acceptable when:

1. Existing HLM web, CLI, REST, MCP, retrieval, and approval behavior continues to work against existing vaults.
2. Hermes can select HLM through its native memory-provider extension and retrieve the same approved memories visible in HLM's web UI.
3. OpenClaw can select HLM through its native memory-plugin mechanism and retrieve the same approved memories visible in HLM's web UI.
4. Both integrations work with local-service and authenticated remote-service configurations.
5. A proposal submitted by either host appears in HLM's pending approval lifecycle, remains unapplied before approval, and is visible after approval through all clients.
6. Approve/reject from chat succeeds only through a supported explicit user-confirmed host flow; otherwise the integration routes to web UI approval.
7. Agent proposal requests over 64 KiB content, 4 KiB paths, or 512 KiB raw JSON are rejected before mutation; pending and resolution responses contain summaries rather than note bodies.
8. Unauthorized scopes, rejected proposals, inaccessible HLM service, oversized responses, and failed writes are reported without bypassing policy or creating misleading success states.
9. Installation and troubleshooting instructions identify tested host versions and explain how to make HLM the shared long-term memory source.

## Implementation sequencing

The implementation first adds the policy-checked note-content API, then the additive bounded agent write/list/resolve contract, and then builds the Hermes and OpenClaw providers over those routes. Provider work can proceed in parallel only after the shared API and approval semantics are verified. Local and remote connection modes use the same contract and security checks. This document defines product intent; package layout and host API pins are specified in the implementation plans.

## Reference findings

- HLM is currently a Python package at version 1.3.5 with REST and streamable HTTP MCP interfaces; the Markdown vault remains authoritative and proposals protect writes.
- Hermes documents an external memory-provider extension point and supports MCP clients. Provider lifecycle and approval behavior depend on the installed host version and interactive runtime.
- OpenClaw documents a memory-plugin slot and MCP clients. Plugin APIs are version-sensitive; human approval is runtime/policy dependent.
- Both native integrations are needed: MCP tool access by itself does not replace each host's separate native memory provider.
