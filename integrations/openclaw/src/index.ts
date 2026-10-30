import { Type, type Static, type TSchema } from "@sinclair/typebox";
import { Value } from "@sinclair/typebox/value";
import { definePluginEntry, type OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";
import type { MemoryPluginCapability } from "openclaw/plugin-sdk/memory-host-core";
import { HlmClient, HlmHttpError } from "./client.js";

const ID = "harbor-ledger-memory-openclaw";
export const REVIEW_INSTRUCTIONS = "Open the HLM web UI and select Approvals in the navigation.";
type Runtime = NonNullable<MemoryPluginCapability["providerRuntime"]>;
export function createProviderRuntime(client: HlmClient): Runtime {
  return {
    open: async () => ({ provider: {
      capabilities: { sources: ["memory"], pagination: false, candidates: [], projectFilter: false },
      search: async ({ query }) => {
        const { selected_memories } = await client.query(query);
        return { hits: selected_memories.map(hit => ({ reference: { providerId: ID, id: `hlm:${encodeURIComponent(hit.path)}` }, excerpt: hit.excerpt })) };
      },
      get: async ({ reference }) => {
        if (reference.providerId !== ID || !reference.id.startsWith("hlm:")) return { status: "not_found" };
        let path: string;
        try { path = decodeURIComponent(reference.id.slice(4)); } catch { return { status: "not_found" }; }
        try { return { status: "ok", reference, text: (await client.note(path)).content }; }
        catch (error) { if (error instanceof HlmHttpError && error.status === 404) return { status: "not_found" }; throw error; }
      },
      health: async () => { try { const s = await client.health(); return { status: s.status === "ok" ? "ready" as const : "degraded" as const }; } catch { return { status: "unavailable" as const }; } },
      close: async () => undefined,
    } }),
  };
}

const schema = Type.Object({ path: Type.String({ minLength: 1 }), content: Type.String() });
const proposalSchema = Type.Object({ id: Type.Integer({ minimum: 1 } ) });
export function approvalRequirement(toolName: string) {
  if (toolName !== "hlm_approve_proposal" && toolName !== "hlm_reject_proposal") return undefined;
  return { requireApproval: { title: "Resolve HLM proposal", description: `Confirm ${toolName} for the specified proposal.`, allowedDecisions: ["allow-once", "deny"] as Array<"allow-once" | "deny">, timeoutMs: 60_000, timeoutReason: "Approval timed out; no proposal was changed.", pluginId: ID } };
}
const plugin = definePluginEntry({
  id: ID,
  name: "Harbor Ledger Memory",
  description: "Shared HLM memory provider",
  kind: "memory",
  register(api: OpenClawPluginApi) {
    const config = api.pluginConfig as { baseUrl: string; token: string; reviewUrl?: string };
    if (!config?.baseUrl || !config.token) throw new Error("HLM baseUrl and token are required");
    const responseLimit = (api.pluginConfig as { maxResponseBytes?: number }).maxResponseBytes;
    const client = new HlmClient(config.baseUrl, config.token, fetch, responseLimit ?? HlmClient.MAX_RESPONSE_BYTES);
    api.registerMemoryCapability({ providerRuntime: createProviderRuntime(client) });
    const registerTool = <S extends TSchema>(name: string, parameters: S, execute: (args: Static<S>) => Promise<unknown>, description = name) => api.registerTool({ name, label: name, description, parameters, execute: async (_toolCallId: string, args: unknown) => { if (!Value.Check(parameters, args)) throw new Error(`Invalid arguments for ${name}`); const details = await execute(args as Static<S>); return { content: [{ type: "text", text: JSON.stringify(details) }], details }; } });
    registerTool("hlm_propose_write", schema, args => client.propose({ path: args.path, content: args.content, operation: "write" }));
    registerTool("hlm_get_notes", Type.Object({ paths: Type.Array(Type.String({ minLength: 1 }), { minItems: 1, maxItems: 10 }) }), args => client.notes(args.paths), "After searching HLM, read up to 10 explicitly selected full Markdown note paths. Results are independent; paths are not expanded.");
    registerTool("hlm_propose_folder", Type.Object({ path: Type.String({ minLength: 1 }) }), args => client.propose({ path: args.path, content: "", operation: "mkdir" }));
    registerTool("hlm_pending_proposals", Type.Object({ limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })), before_id: Type.Optional(Type.Integer({ minimum: 1 })) }), async args => ({ ...(await client.pending(args)), reviewUrl: config.reviewUrl ?? config.baseUrl, reviewInstructions: REVIEW_INSTRUCTIONS }));
    api.on("before_tool_call", event => {
      return approvalRequirement(event.toolName);
    });
    registerTool("hlm_approve_proposal", proposalSchema, args => client.resolve(args.id, "approve"));
    registerTool("hlm_reject_proposal", proposalSchema, args => client.resolve(args.id, "reject"));
  },
});

export default plugin;
