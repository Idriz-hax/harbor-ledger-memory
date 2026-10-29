import { test } from "node:test";
import { strict as assert } from "node:assert";
import { HlmClient } from "../src/client.js";
test("query authenticates and encodes local/remote URL paths", async () => {
  for (const base of ["http://localhost:8000", "https://memory.example/api"]) {
    let seen: Request | undefined;
    const client = new HlmClient(base, "secret", async (input, init) => { seen = new Request(input, init); return Response.json({ selected_memories: [] }); });
    await client.query("topic");
    assert.equal(new URL(seen!.url).pathname, "/api/v1/queries");
    assert.equal(seen!.headers.get("authorization"), "Bearer secret");
    assert.deepEqual(await seen!.json(), { query: "topic" });
  }
});
test("rejects non-2xx and malformed payloads", async () => {
  await assert.rejects(new HlmClient("http://localhost", "t", async () => new Response("no", { status: 401 })).query("x"));
  await assert.rejects(new HlmClient("http://localhost", "t", async () => new Response("null")).query("x"));
});
test("maps selected memories, enforces note payload/errors, and shapes write endpoints", async () => {
  const calls: Array<{url:string; init:RequestInit}> = [];
  const client = new HlmClient("https://host.example/root", "t", async (input, init = {}) => {
    const url = new URL(String(input)); calls.push({url:url.href,init});
    if (url.pathname.endsWith("/queries")) return Response.json({ selected_memories: [{path:"café/a b.md",title:"Title",excerpt:"Excerpt"}] });
    if (url.pathname.endsWith("/notes/content")) return Response.json({content:"body"});
    if (url.pathname.endsWith("/status")) return Response.json({status:"ok"});
    if (url.pathname.endsWith("/agent/writes") && init.method === "POST") return Response.json({id:1,path:"x.md",operation:"create",status:"pending",requested_at:"now",content:"secret",diff:"secret",extra:true});
    if (url.pathname.endsWith("/agent/writes/pending")) return Response.json({proposals:[{id:2,path:"x.md",operation:"update",status:"pending",requested_at:"now",content:"must not be exposed"}],next_before_id:23});
    if (url.pathname.endsWith("/agent/writes/3/approve")) return Response.json({id:3,path:"x.md",operation:"create",status:"approved",requested_at:"now",content:"secret",diff:"secret",extra:true});
    if (url.pathname.endsWith("/agent/writes/3/reject")) return Response.json({id:3,path:"x.md",operation:"mkdir",status:"rejected",requested_at:"now",diff:"secret"});
    return Response.json({error:"server"},{status:500});
  });
  assert.deepEqual((await client.query("q")).selected_memories[0], {path:"café/a b.md",title:"Title",excerpt:"Excerpt"});
  assert.deepEqual(await client.note("café/a b.md"),{content:"body"});
  await client.health();
  assert.deepEqual(await client.propose({path:"x.md",content:"x",operation:"write"}),{id:1,path:"x.md",operation:"create",status:"pending",requested_at:"now"});
  await client.propose({path:"folder",content:"",operation:"mkdir"});
  assert.deepEqual(await client.pending({limit:17,before_id:42}),{proposals:[{id:2,path:"x.md",operation:"update",status:"pending",requested_at:"now"}],next_before_id:23});
  await client.pending({before_id:23});
  assert.deepEqual(await client.resolve(3,"approve"),{id:3,path:"x.md",operation:"create",status:"approved",requested_at:"now"});
  assert.deepEqual(await client.resolve(3,"reject"),{id:3,path:"x.md",operation:"mkdir",status:"rejected",requested_at:"now"});
  assert.equal(calls[1].url,"https://host.example/api/v1/notes/content?path=caf%C3%A9%2Fa%20b.md");
  assert.equal(calls[2].url,"https://host.example/api/v1/status");
  assert.equal(new URL(calls[3].url).pathname,"/api/v1/agent/writes");
  assert.deepEqual(JSON.parse(String(calls[3].init.body)),{path:"x.md",content:"x",operation:"write"});
  assert.deepEqual(JSON.parse(String(calls[4].init.body)),{path:"folder",content:"",operation:"mkdir"});
  assert.equal(calls[5].url,"https://host.example/api/v1/agent/writes/pending?limit=17&before_id=42");
  assert.equal(calls[6].url,"https://host.example/api/v1/agent/writes/pending?limit=20&before_id=23");
  assert.equal(calls[7].url,"https://host.example/api/v1/agent/writes/3/approve");
  assert.equal(calls[8].url,"https://host.example/api/v1/agent/writes/3/reject");
  assert.ok(calls.slice(3).every(call => !new URL(call.url).pathname.startsWith("/api/v1/writes")));
});
test("write and resolution responses require all numeric summary fields", async () => {
  for (const response of [{id:"4",path:"x",operation:"create",status:"pending",requested_at:"now"},{id:4,path:"x",operation:"write",status:"pending",requested_at:"now"},{id:4,path:"x",operation:"create",status:"pending"}]) {
    const client = new HlmClient("https://memory.example","t",async () => Response.json(response));
    await assert.rejects(client.propose({path:"x",content:"",operation:"write"}),/invalid.*summary/i);
    await assert.rejects(client.resolve(4,"reject"),/invalid.*summary/i);
  }
});
test("pending cursors must be positive safe integers", async () => {
  const client = new HlmClient("https://memory.example", "t", async input => {
    const before = new URL(String(input)).searchParams.get("before_id");
    return Response.json({ proposals: [], ...(before === "" ? { next_before_id: null } : { next_before_id: 0.5 }) });
  });
  for (const before_id of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
    await assert.rejects(client.pending({ before_id }), /positive safe integer/i);
  }
  await assert.rejects(client.pending(), /invalid pending cursor/i);
});
test("fails invalid note content and preserves HTTP status including 404/413/500", async () => {
  await assert.rejects(new HlmClient("http://localhost", "t", async () => Response.json({content:42})).note("x"));
  for (const status of [404,401,403,413,500]) await assert.rejects(new HlmClient("http://localhost", "t", async () => new Response("",{status})).note("x"), new RegExp(String(status)));
});
test("allows loopback HTTP and remote HTTPS, rejects plaintext remote and URL credentials", () => {
  for (const url of ["http://127.0.0.1:8765", "http://localhost:8765", "https://memory.example"]) assert.doesNotThrow(() => new HlmClient(url,"t"));
  for (const url of ["http://memory.example", "https://user:pass@memory.example", "http://user@localhost"]) assert.throws(() => new HlmClient(url,"t"));
});
test("HLM requests disable redirects and keep bearer auth and ten-second timeout", async () => {
  let seen: RequestInit | undefined;
  const client = new HlmClient("https://memory.example","secret",async (_url,init) => { seen=init; return Response.json({selected_memories:[]}); });
  await client.query("q");
  assert.equal(seen?.redirect,"error");
  assert.equal(new Headers(seen?.headers).get("authorization"),"Bearer secret");
  assert.ok(seen?.signal instanceof AbortSignal);
});
test("agent write API validates UTF-8 bounds and serialized payload before fetch", async () => {
  let calls=0;
  let serializedBytes=0;
  const client = new HlmClient("https://memory.example","t",async (_url,init) => { calls++; serializedBytes=new TextEncoder().encode(String(init?.body)).byteLength; return Response.json({id:1,path:"x",operation:"create",status:"pending",requested_at:"now"}); });
  await client.propose({path:"é".repeat(2048),content:"é".repeat(32768),operation:"write"});
  assert.ok(serializedBytes <= 512 * 1024);
  await assert.rejects(client.propose({path:"é".repeat(2049),content:"",operation:"write"}),/path.*4096/i);
  await assert.rejects(client.propose({path:"x",content:"é".repeat(32769),operation:"write"}),/content.*65536/i);
  assert.equal(calls,1);
});
test("oversized streamed responses cancel instead of parsing or truncating", async () => {
  let cancelled=false;
  const body = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new Uint8Array(8 * 1024 * 1024 + 1)); },
    cancel() { cancelled=true; },
  });
  const response = new Response(body);
  const client = new HlmClient("https://memory.example","t",async () => response);
  await assert.rejects(client.pending(),/8 MiB/);
  assert.equal(cancelled,true);
});
test("batch retrieval streams ordered NDJSON including complete large notes and unavailable paths", async () => {
  const large = "x".repeat(1_100_000);
  const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new TextEncoder().encode(JSON.stringify({path:"a.md",ok:true,content:large})+"\n"+JSON.stringify({path:"missing.md",ok:false,error:"unavailable"})+"\n")); controller.close(); } });
  const client = new HlmClient("https://memory.example", "t", async (_input, init) => { assert.equal(init?.redirect,"error"); assert.deepEqual(JSON.parse(String(init?.body)),{paths:["a.md","missing.md"]}); return new Response(body); });
  assert.deepEqual(await client.notes(["a.md","missing.md"]),[{path:"a.md",content:large},{path:"missing.md",error:"unavailable"}]);
});
test("batch retrieval rejects over ten paths before making a request", async () => {
  let calls=0;
  const client = new HlmClient("https://memory.example","t",async () => { calls++; return new Response(""); });
  await assert.rejects(client.notes(Array.from({length:11},(_,i)=>String(i))),/1 and 10/);
  assert.equal(calls,0);
});
test("batch retrieval preserves duplicate paths as independent ordered records", async () => {
  const client = new HlmClient("https://memory.example","t",async () => new Response(
    JSON.stringify({path:"same.md",ok:true,content:"first"})+"\n"+
    JSON.stringify({path:"same.md",ok:false,error:"unavailable"})+"\n"));
  assert.deepEqual(await client.notes(["same.md","same.md"]),[
    {path:"same.md",content:"first"},{path:"same.md",error:"unavailable"},
  ]);
  const inconsistent = new HlmClient("https://memory.example","t",async () => new Response(JSON.stringify({path:"other.md",ok:false,error:"unavailable"})+"\n"));
  await assert.rejects(inconsistent.notes(["same.md"]),/inconsistent batch records/);
});
