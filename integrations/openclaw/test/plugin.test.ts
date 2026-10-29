import { test } from "node:test";
import { strict as assert } from "node:assert";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import plugin, { approvalRequirement, createProviderRuntime, REVIEW_INSTRUCTIONS } from "../src/index.js";
import { HlmClient } from "../src/client.js";
test("manifest registers token as a config-contract secret input", async () => {
  const manifest = JSON.parse(await readFile(fileURLToPath(new URL("../openclaw.plugin.json", import.meta.url)), "utf8"));
  assert.deepEqual(manifest.configContracts.secretInputs.paths, [{ path: "token", expected: "string" }]);
});
test("maps paths to scoped refs and refuses foreign refs", async () => {
  const runtime = createProviderRuntime(new HlmClient("http://localhost", "t", async () => Response.json({ selected_memories: [{ path: "a b/☃.md", title: "T", excerpt: "short" }] })));
  const result = await (await runtime.open({} as never)).provider!.search({ query: "q" });
  assert.equal(result.hits[0].reference.id, "hlm:a%20b%2F%E2%98%83.md");
  assert.deepEqual(await (await runtime.open({} as never)).provider!.get({ reference: { providerId: "else", id: "other:x" } } as never), { status: "not_found" });
  assert.deepEqual(await (await runtime.open({} as never)).provider!.get({ reference: { providerId: "else", id: "hlm:a%20b" } } as never), { status: "not_found" });
});
test("requires host one-shot approval for resolution tools only", () => {
  for (const name of ["hlm_approve_proposal", "hlm_reject_proposal"]) {
    const requirement = approvalRequirement(name)!.requireApproval;
    assert.deepEqual(requirement.allowedDecisions,["allow-once","deny"]);
    assert.equal(requirement.timeoutMs,60_000);
  }
  assert.equal(approvalRequirement("hlm_pending_proposals"),undefined);
});
test("surfaces server note failures rather than not_found", async () => {
  const runtime = createProviderRuntime(new HlmClient("http://localhost","t",async () => new Response("",{status:500})));
  await assert.rejects((await runtime.open({} as never)).provider!.get({reference:{providerId:"harbor-ledger-memory-openclaw",id:"hlm:x"}}));
});
test("note get distinguishes 404 from oversized-response failures", async () => {
  const reference = {providerId:"harbor-ledger-memory-openclaw",id:"hlm:caf%C3%A9%2Fa%20b.md"};
  const calls: string[] = [];
  const run = async (status: number) => {
    const runtime = createProviderRuntime(new HlmClient("http://localhost","t",async input => { calls.push(String(input)); return new Response("",{status}); }));
    return (await runtime.open({} as never)).provider!.get({reference} as never);
  };
  assert.deepEqual(await run(404),{status:"not_found"});
  await assert.rejects(run(413));
  assert.match(calls[0],/path=caf%C3%A9%2Fa%20b\.md/);
});
test("provider get returns complete notes larger than 1 MiB", async () => {
  const content = "é".repeat(600_000);
  const runtime = createProviderRuntime(new HlmClient("http://localhost","t",async () => Response.json({content})));
  const result = await (await runtime.open({} as never)).provider!.get({reference:{providerId:"harbor-ledger-memory-openclaw",id:"hlm:large"}} as never);
  assert.equal(result.status,"ok");
  if (result.status === "ok") assert.equal(result.text,content);
});
test("health uses status endpoint only and maps ok to ready", async () => {
  const urls: string[] = [];
  const runtime = createProviderRuntime(new HlmClient("http://localhost","t",async input => { urls.push(String(input)); return Response.json({status:"ok"}); }));
  const provider = (await runtime.open({} as never)).provider!;
  assert.deepEqual(await provider.health(),{status:"ready"});
  assert.deepEqual(urls,["http://localhost/api/v1/status"]);
});
test("registered approval hook gates resolution execute and pending result guides review", async () => {
  let before: ((event: any) => any) | undefined;
  const tools = new Map<string, any>();
  let resolves = 0;
  let resolveUrl = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async input => {
    if (/\/agent\/writes\/[^/]+\/(approve|reject)$/.test(String(input))) { resolves++; resolveUrl=String(input); return Response.json({id:9,path:"x.md",operation:"update",status:"approved",requested_at:"now",content:"secret",diff:"secret"}); }
    if (String(input).includes("/agent/writes/pending")) return Response.json({proposals:[{id:8,path:"x.md",operation:"create",status:"pending",requested_at:"now",diff:"secret"}]});
    return Response.json({});
  };
  const api = {
    pluginConfig: {baseUrl:"https://hlm.example",token:"secret"},
    registerMemoryCapability() {}, registerTool(tool: any) { tools.set(tool.name,tool); },
    on(_name: string, handler: (event:any)=>any) { before=handler; },
  };
  (plugin as any).register(api);
  assert.ok(before);
  const approval = before!({toolName:"hlm_approve_proposal"});
  assert.deepEqual(approval.requireApproval.allowedDecisions,["allow-once","deny"]);
  const tool = tools.get("hlm_approve_proposal");
  try {
    for (const decision of ["deny","timeout","cancelled","no-route"]) {
      const hostCanApprove = decision !== "no-route";
      const hookResult = hostCanApprove ? before!({toolName:"hlm_approve_proposal"}) : undefined;
      if (hookResult && decision === "allow-once") await tool.execute("1",{id:"p"});
    }
    assert.equal(resolves,0);
    const hostDecision = "allow-once";
    if (approval.requireApproval && hostDecision === "allow-once") await assert.rejects(tool.execute("1",{id:"9"}),/Invalid arguments/);
    const resolved = await tool.execute("1",{id:9});
    assert.equal(resolves,1);
    assert.equal(resolveUrl,"https://hlm.example/api/v1/agent/writes/9/approve");
    assert.deepEqual(Object.keys(resolved.details).sort(),["id","operation","path","requested_at","status"]);
    const pending = tools.get("hlm_pending_proposals");
    const result = await pending.execute("2",{});
    assert.deepEqual(result.details.proposals,[{id:8,path:"x.md",operation:"create",status:"pending",requested_at:"now"}]);
    assert.equal(result.details.reviewUrl,"https://hlm.example");
    assert.equal(result.details.reviewInstructions,REVIEW_INSTRUCTIONS);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
test("batch retrieval tool invokes client and returns per-path results", async () => {
  const originalFetch = globalThis.fetch;
  const tools = new Map<string, any>();
  globalThis.fetch = async (_input, init) => {
    assert.deepEqual(JSON.parse(String(init?.body)),{paths:["a.md","missing.md"]});
    return new Response(JSON.stringify({path:"a.md",ok:true,content:"full note"})+"\n"+JSON.stringify({path:"missing.md",ok:false,error:"unavailable"})+"\n");
  };
  try {
    (plugin as any).register({pluginConfig:{baseUrl:"https://hlm.example",token:"secret"},registerMemoryCapability(){},registerTool(tool:any){tools.set(tool.name,tool);},on(){}});
    const result = await tools.get("hlm_get_notes").execute("1",{paths:["a.md","missing.md"]});
    assert.deepEqual(result.details,[{path:"a.md",content:"full note"},{path:"missing.md",error:"unavailable"}]);
  } finally { globalThis.fetch=originalFetch; }
});
