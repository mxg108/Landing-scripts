// SOP retrieval — ports of backend/services/rag/{pulpo,sop_retrieval}.py
// (PulpoConnection §4.1/§4.2). Speaks MCP over Streamable HTTP against
// PULPO_MCP_URL via plain fetch: initialize handshake, then tools/call
// JSON-RPC POSTs, Bearer-authenticated; SSE-or-JSON responses handled;
// expired session (404) re-initializes once.
//
// Policy: the verified DISPOSITION is the query (transcript head as the
// absence fallback) → search → τ threshold → fetch bodies → render the
// numbered SOP block (flag cautions, char cap honored lowest-score-first).
// Never throws: any failure returns an empty context with skipped_reason —
// retrieval is an enhancement, never a scoring blocker.

const PROTOCOL_VERSION = "2025-03-26";
const CLIENT_INFO = { name: "landing-qa-scoring-sandy", version: "1.0" };
const QUERY_MAX_CHARS = 500; // Pulpo's server-side zod cap
const MAX_DOCS = 3;
const BLOCK_CHAR_CAP = 16_000;
const DEFAULT_THRESHOLD = 0.55;

export interface SopContext {
  query: string;
  sop_title: string;
  block_text: string;
  provenance: any[];
  skipped_reason: string;
}

const empty = (query = "", reason = ""): SopContext => ({
  query,
  sop_title: "",
  block_text: "",
  provenance: [],
  skipped_reason: reason,
});

// Isolate-lifetime caches (stateless workers: helpful within bursts; the
// finite disposition label space makes even short-lived caches pay off).
const searchCache = new Map<string, { exp: number; hits: any[] }>();
const docCache = new Map<string, { exp: number; doc: any | null }>();
const CACHE_TTL_MS = 3600_000;

class PulpoClient {
  private sessionId: string | null = null;
  private initialized = false;
  private nextId = 0;
  constructor(
    private url: string,
    private token: string
  ) {}

  private async post(body: any): Promise<Response> {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.token}`,
      Accept: "application/json, text/event-stream",
      "content-type": "application/json",
    };
    if (this.sessionId) headers["Mcp-Session-Id"] = this.sessionId;
    return fetch(this.url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(11_000),
    });
  }

  private parseRpc(contentType: string, text: string): any | null {
    const t = text.trim();
    if (!t) return null;
    if (contentType.includes("text/event-stream")) {
      let message: any = null;
      for (let line of t.split("\n")) {
        line = line.trim();
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try {
          const candidate = JSON.parse(payload);
          if (candidate && typeof candidate === "object" && ("result" in candidate || "error" in candidate))
            message = candidate;
        } catch {}
      }
      return message;
    }
    return JSON.parse(t);
  }

  private async initialize(): Promise<void> {
    this.nextId += 1;
    const resp = await this.post({
      jsonrpc: "2.0",
      id: this.nextId,
      method: "initialize",
      params: { protocolVersion: PROTOCOL_VERSION, capabilities: {}, clientInfo: CLIENT_INFO },
    });
    if (resp.status >= 400)
      throw new Error(`pulpo initialize HTTP ${resp.status}: ${(await resp.text()).slice(0, 200)}`);
    const message = this.parseRpc(resp.headers.get("content-type") ?? "", await resp.text());
    if (!message || message.error) throw new Error(`pulpo initialize rejected: ${JSON.stringify(message)?.slice(0, 200)}`);
    this.sessionId = resp.headers.get("mcp-session-id") ?? this.sessionId;
    await this.post({ jsonrpc: "2.0", method: "notifications/initialized" });
    this.initialized = true;
  }

  async toolCall(tool: string, args: any): Promise<any> {
    if (!this.initialized) await this.initialize();
    this.nextId += 1;
    const body = {
      jsonrpc: "2.0",
      id: this.nextId,
      method: "tools/call",
      params: { name: tool, arguments: args },
    };
    let resp = await this.post(body);
    if (resp.status === 404 && this.sessionId) {
      this.initialized = false;
      this.sessionId = null;
      await this.initialize();
      resp = await this.post(body);
    }
    if (resp.status >= 400)
      throw new Error(`pulpo ${tool} HTTP ${resp.status}: ${(await resp.text()).slice(0, 200)}`);
    const message = this.parseRpc(resp.headers.get("content-type") ?? "", await resp.text());
    if (!message) throw new Error(`pulpo ${tool}: no response message`);
    if (message.error) throw new Error(`pulpo ${tool} error: ${JSON.stringify(message.error).slice(0, 200)}`);
    const result = message.result ?? {};
    if (result.isError) throw new Error(`pulpo ${tool} tool error`);
    if ("structuredContent" in result) return result.structuredContent;
    for (const block of result.content ?? []) {
      if (block.type === "text") {
        try {
          return JSON.parse(block.text ?? "");
        } catch {
          return block.text;
        }
      }
    }
    return result;
  }
}

function mapHit(raw: any) {
  return {
    id: String(raw.id ?? ""),
    title: raw.title ?? "",
    score: Number(raw.score ?? 0),
    score_kind: raw.score_type ?? "",
    updated_at: String(raw.last_verified ?? raw.updated_at ?? ""),
  };
}

function mapDoc(raw: any) {
  return {
    id: String(raw.id ?? ""),
    title: raw.title ?? "",
    body: raw.body ?? "",
    flags: (raw.open_flags ?? []).map((f: any) => ({ quote: f.quote ?? "" })),
    updated_at: String(raw.last_verified ?? raw.updated_at ?? ""),
  };
}

export function buildSopQuery(
  dispositionCategory: string | null,
  disposition: string | null,
  transcriptText: string
): string | null {
  if (dispositionCategory) {
    return disposition ? `${dispositionCategory} — ${disposition}` : dispositionCategory;
  }
  const head = (transcriptText ?? "").trim().slice(0, 600);
  return head || null;
}

export function renderSopBlock(docs: { doc: any; hit: any }[]): string {
  if (!docs.length) return "";
  const sections: string[] = [];
  docs.forEach(({ doc }, i) => {
    const lines = [`[SOP ${i + 1}] ${doc.title}`];
    for (const flag of doc.flags ?? []) {
      lines.push(
        `  NOTE: the passage "${(flag.quote ?? "").slice(0, 120)}" is under ` +
          "review — verify before treating it as current policy."
      );
    }
    lines.push((doc.body ?? "").trim());
    sections.push(lines.join("\n"));
  });
  // cap from the lowest-scoring doc up; retruncate guard mirrors Python
  while (sections.length && sections.reduce((a, s) => a + s.length, 0) > BLOCK_CHAR_CAP) {
    if (sections[sections.length - 1].length > 2_100) {
      sections[sections.length - 1] = sections[sections.length - 1].slice(0, 2_000) + "\n[truncated]";
    } else {
      sections.pop();
    }
  }
  return sections.join("\n\n");
}

function cacheGet<T>(cache: Map<string, { exp: number } & any>, key: string): T | null {
  const entry = cache.get(key);
  if (entry && entry.exp > Date.now()) return entry as unknown as T;
  cache.delete(key);
  return null;
}

export async function fetchSopContext(opts: {
  pulpoUrl?: string;
  pulpoToken?: string;
  threshold?: number;
  dispositionCategory: string | null;
  disposition: string | null;
  transcriptText: string;
}): Promise<SopContext> {
  const query = buildSopQuery(opts.dispositionCategory, opts.disposition, opts.transcriptText);
  if (query === null) return empty("", "no_query_material");
  if (!opts.pulpoUrl || !opts.pulpoToken) return empty(query, "no_provider");

  try {
    const client = new PulpoClient(opts.pulpoUrl, opts.pulpoToken);
    const sent = query.slice(0, QUERY_MAX_CHARS);
    let hits: any[];
    const cached = cacheGet<{ hits: any[] }>(searchCache, sent);
    if (cached) {
      hits = cached.hits;
    } else {
      // rerank false by design (§4.2): the judge reads full bodies itself
      const payload = await client.toolCall("search_knowledge_base", {
        queries: [sent],
        limit: 5,
        rerank: false,
      });
      const batches = payload?.batches ?? [];
      const batch = batches.find((b: any) => b.query === sent) ?? batches[0] ?? {};
      hits = (batch.results ?? []).map(mapHit);
      searchCache.set(sent, { exp: Date.now() + CACHE_TTL_MS, hits });
    }

    const tau = opts.threshold ?? DEFAULT_THRESHOLD;
    const selected = hits.filter((h) => h.score >= tau).slice(0, MAX_DOCS);
    if (!selected.length) return empty(query, "below_threshold");

    const docs: { doc: any; hit: any }[] = [];
    for (const hit of selected) {
      let doc: any;
      const cachedDoc = cacheGet<{ doc: any }>(docCache, hit.id);
      if (cachedDoc) {
        doc = cachedDoc.doc;
      } else {
        try {
          const payload = await client.toolCall("get_document", { id: hit.id });
          const raw =
            payload && typeof payload.document === "object" ? payload.document : payload;
          doc = raw?.id ? mapDoc(raw) : null;
        } catch (e) {
          if (String(e).toLowerCase().includes("not found")) doc = null;
          else throw e;
        }
        docCache.set(hit.id, { exp: Date.now() + CACHE_TTL_MS, doc });
      }
      if (doc && (doc.body ?? "").trim()) docs.push({ doc, hit });
    }
    if (!docs.length) return empty(query, "no_bodies");

    return {
      query,
      sop_title: docs[0].doc.title,
      block_text: renderSopBlock(docs),
      provenance: docs.map(({ doc, hit }) => ({
        id: doc.id,
        title: doc.title,
        score: hit.score,
        score_kind: hit.score_kind,
        updated_at: doc.updated_at,
        open_flags: (doc.flags ?? []).length,
      })),
      skipped_reason: "",
    };
  } catch {
    return empty(query, "provider_error");
  }
}
