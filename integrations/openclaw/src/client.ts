export class HlmHttpError extends Error {
  constructor(readonly status: number) { super(`HLM request failed (${status})`); }
}

export type AgentWriteSummary = {
  id: number;
  path: string;
  operation: "create" | "update" | "mkdir";
  status: string;
  requested_at: string;
};

function projectAgentWriteSummary(value: unknown): AgentWriteSummary {
  if (!value || typeof value !== "object") throw new Error("HLM returned invalid agent write summary");
  const summary = value as Record<string, unknown>;
  if (typeof summary.id !== "number" || !Number.isSafeInteger(summary.id) || summary.id < 1 || typeof summary.path !== "string" || (summary.operation !== "create" && summary.operation !== "update" && summary.operation !== "mkdir") || typeof summary.status !== "string" || typeof summary.requested_at !== "string") {
    throw new Error("HLM returned invalid agent write summary");
  }
  return { id: summary.id, path: summary.path, operation: summary.operation, status: summary.status, requested_at: summary.requested_at };
}

export class HlmClient {
  static readonly MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
  private readonly baseUrl: URL;
  constructor(baseUrl: string, private readonly token: string, private readonly fetcher: typeof fetch = fetch) {
    this.baseUrl = new URL(baseUrl);
    const host = this.baseUrl.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    const loopback = host === "localhost" || host === "::1" || /^127(?:\.\d{1,3}){3}$/.test(host) && host.split(".").every(part => Number(part) <= 255);
    if (this.baseUrl.username || this.baseUrl.password || this.baseUrl.search || this.baseUrl.hash || !(this.baseUrl.protocol === "https:" || this.baseUrl.protocol === "http:" && loopback)) {
      throw new Error("HLM baseUrl must use HTTPS, or HTTP on explicit loopback, without userinfo, query, or fragment");
    }
  }
  private async request<T>(path: string, init: RequestInit = {}, maxResponseBytes = HlmClient.MAX_RESPONSE_BYTES): Promise<T> {
    const response = await this.fetcher(new URL(path, this.baseUrl), { ...init, redirect: "error", signal: AbortSignal.timeout(10_000), headers: { authorization: `Bearer ${this.token}`, ...(init.body ? { "content-type": "application/json" } : {}), ...init.headers } });
    if (!response.ok) throw new HlmHttpError(response.status);
    const length = Number(response.headers.get("content-length"));
    if (Number.isFinite(length) && length > maxResponseBytes) {
      await response.body?.cancel();
      throw new Error(`HLM response exceeds ${maxResponseBytes === HlmClient.MAX_RESPONSE_BYTES ? "8 MiB" : `${maxResponseBytes} byte`} limit`);
    }
    if (!response.body) throw new Error("HLM returned invalid JSON");
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxResponseBytes) {
        await reader.cancel();
        throw new Error(`HLM response exceeds ${maxResponseBytes === HlmClient.MAX_RESPONSE_BYTES ? "8 MiB" : `${maxResponseBytes} byte`} limit`);
      }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    let value: unknown;
    try { value = JSON.parse(new TextDecoder().decode(bytes)); } catch { throw new Error("HLM returned invalid JSON"); }
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("HLM returned invalid payload");
    return value as T;
  }
  async query(query: string) {
    const data = await this.request<{ selected_memories: Array<{ path: string; title: string; excerpt: string }> }>("/api/v1/queries", { method: "POST", body: JSON.stringify({ query }) });
    if (!Array.isArray(data.selected_memories) || data.selected_memories.some(hit => typeof hit.path !== "string" || typeof hit.title !== "string" || typeof hit.excerpt !== "string")) throw new Error("HLM returned invalid query results");
    return data;
  }
  async note(path: string) {
    const data = await this.request<{ content: string }>(`/api/v1/notes/content?path=${encodeURIComponent(path)}`, {}, Infinity);
    if (typeof data.content !== "string") throw new Error("HLM returned invalid note content");
    return data;
  }
  async notes(paths: string[]) {
    if (!Array.isArray(paths) || paths.length < 1 || paths.length > 10 || paths.some(path => typeof path !== "string" || !path.length)) throw new Error("Batch paths must contain between 1 and 10 non-empty paths");
    const response = await this.fetcher(new URL("/api/v1/notes/content/batch", this.baseUrl), { method: "POST", body: JSON.stringify({ paths }), redirect: "error", signal: AbortSignal.timeout(10_000), headers: { authorization: `Bearer ${this.token}`, "content-type": "application/json" } });
    if (!response.ok) throw new HlmHttpError(response.status);
    if (!response.body) throw new Error("HLM returned invalid NDJSON");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const results: Array<{ path: string; content?: string; error?: string }> = [];
    let pending = "";
    const consume = (line: string) => {
      if (!line.trim()) return;
      let record: unknown;
      try { record = JSON.parse(line); } catch { throw new Error("HLM returned invalid NDJSON"); }
      if (!record || typeof record !== "object" || Array.isArray(record)) throw new Error("HLM returned invalid batch record");
      const item = record as Record<string, unknown>;
      if (typeof item.path !== "string" || typeof item.ok !== "boolean") throw new Error("HLM returned invalid batch record");
      if (results.length >= paths.length || item.path !== paths[results.length]) throw new Error("HLM returned inconsistent batch records");
      if (item.ok && typeof item.content === "string") results.push({ path: item.path, content: item.content });
      else if (!item.ok) results.push({ path: item.path, error: typeof item.error === "string" ? item.error : "unavailable" });
      else throw new Error("HLM returned invalid batch record");
    };
    try {
      while (true) {
        const { done, value } = await reader.read();
        pending += decoder.decode(value, { stream: !done });
        let newline: number;
        while ((newline = pending.indexOf("\n")) >= 0) { consume(pending.slice(0, newline)); pending = pending.slice(newline + 1); }
        if (done) break;
      }
      if (pending) consume(pending);
    } catch (error) { await reader.cancel(); throw error; }
    if (results.length !== paths.length) throw new Error("HLM returned incomplete batch records");
    return results;
  }
  async health() { return this.request<{ status: string }>("/api/v1/status"); }
  async propose(body: { path: string; content: string; operation: "write" | "mkdir" }) {
    const encoder = new TextEncoder();
    if (typeof body.path !== "string" || encoder.encode(body.path).byteLength > 4096) throw new Error("Agent write path exceeds 4096 UTF-8 byte limit");
    if (typeof body.content !== "string" || encoder.encode(body.content).byteLength > 65536) throw new Error("Agent write content exceeds 65536 UTF-8 byte limit");
    if (body.operation !== "write" && body.operation !== "mkdir") throw new Error("Invalid agent write operation");
    const serialized = JSON.stringify(body);
    if (encoder.encode(serialized).byteLength > 512 * 1024) throw new Error("Agent write request exceeds 512KiB raw limit");
    return projectAgentWriteSummary(await this.request<unknown>("/api/v1/agent/writes", { method: "POST", body: serialized }));
  }
  async pending(options: { limit?: number; before_id?: number } = {}) {
    const limit = options.limit ?? 20;
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error("Pending limit must be between 1 and 100");
    if (options.before_id !== undefined && (!Number.isSafeInteger(options.before_id) || options.before_id < 1)) throw new Error("Pending cursor must be a positive safe integer");
    const params = new URLSearchParams({ limit: String(limit) });
    if (options.before_id !== undefined) params.set("before_id", String(options.before_id));
    const data = await this.request<{ proposals: unknown[]; next_before_id?: number | null }>(`/api/v1/agent/writes/pending?${params}`);
    if (!Array.isArray(data.proposals)) throw new Error("HLM returned invalid proposals");
    const proposals = data.proposals.map(proposal => {
      return projectAgentWriteSummary(proposal);
    });
    if (data.next_before_id !== undefined && data.next_before_id !== null && (typeof data.next_before_id !== "number" || !Number.isSafeInteger(data.next_before_id) || data.next_before_id < 1)) throw new Error("HLM returned invalid pending cursor");
    return { proposals, ...(data.next_before_id !== undefined ? { next_before_id: data.next_before_id } : {}) };
  }
  async resolve(id: number, action: "approve" | "reject") {
    if (!Number.isSafeInteger(id) || id < 1) throw new Error("Agent write ID must be a positive integer");
    if (action !== "approve" && action !== "reject") throw new Error("Invalid agent write resolution action");
    return projectAgentWriteSummary(await this.request<unknown>(`/api/v1/agent/writes/${encodeURIComponent(String(id))}/${action}`, { method: "POST", body: "{}" }));
  }
}
