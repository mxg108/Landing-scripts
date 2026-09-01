// SOP retrieval — ports of backend/services/rag/{pulpo,sop_retrieval}.py
// (PulpoConnection §4.1/§4.2). Speaks MCP over Streamable HTTP against
// PULPO_MCP_URL via plain fetch: initialize handshake, then tools/call
// JSON-RPC POSTs, Bearer-authenticated; SSE-or-JSON responses handled;
// expired session (404) re-initializes once.
import {
  renderSopContextBlock,
  SOP_BLOCK_PLACEHOLDER,
  type SopBlockParts,
} from "./scoringPrompts.js";
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
    // Pulpo canonicalizes tags server-side (kebab-case slugs, namespaced
    // facets like system:sofia) — the scope filter keys on these.
    tags: Array.isArray(raw.tags) ? raw.tags.map(String) : null,
    body_format: raw.body_format ?? "",
  };
}

function mapDoc(raw: any) {
  return {
    id: String(raw.id ?? ""),
    title: raw.title ?? "",
    body: raw.body ?? "",
    flags: (raw.open_flags ?? []).map((f: any) => ({ quote: f.quote ?? "" })),
    updated_at: String(raw.last_verified ?? raw.updated_at ?? ""),
    body_format: raw.body_format ?? "",
  };
}

export function buildSopQuery(
  dispositionCategory: string | null,
  disposition: string | null,
  transcriptText: string,
  // Provider-supplied summary (retell call_analysis.call_summary) — better
  // retrieval material than the raw transcript head when no disposition
  // exists (SofiaRetellSpec §4.3).
  summaryQuery?: string | null
): string | null {
  if (dispositionCategory) {
    return disposition ? `${dispositionCategory} — ${disposition}` : dispositionCategory;
  }
  const summary = (summaryQuery ?? "").trim();
  if (summary) return summary;
  const head = (transcriptText ?? "").trim().slice(0, 600);
  return head || null;
}

export function renderSopBlock(docs: { doc: any; hit: any }[]): string {
  if (!docs.length) return "";
  const sections: string[] = [];
  docs.forEach(({ doc }, i) => {
    const lines = [`[SOP ${i + 1}] ${doc.title}`];
    if ((doc.body_format ?? "") === "flowchart") {
      lines.push(
        "  NOTE: this SOP is a process flowchart (Mermaid source). Read it " +
          'as a decision graph — follow nodes and edges; "%% def <node>:" ' +
          'comment lines carry the full rule for that node; "%% always:" / ' +
          '"%% never:" lines are doc-wide guardrails that apply at every step.'
      );
    }
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

// Per-team tag scoping (teams.retrieval_config, migrations 0006+0013),
// enforced on the tags each search hit itself carries (Pulpo returns
// canonicalized tags on every hit — verified live 2026-08-31). This
// replaced the list_documents_by_tag doc-id roundtrip, which cost an extra
// tool call and silently truncated at 50 docs (system:sofia already
// exceeds it). Shape stays provider-agnostic: {tags, match} allow-scopes
// a team to its doc families (sofia); {exclude_tags} carves families out
// of the shared pool (MS/Sales exclude system:sofia — Sofia's
// machine-maintained engineering estate). FAIL-CLOSED: under an active
// scope, a hit with no tags array (unknown tag state) is dropped rather
// than risk leaking another team's docs.
export interface RetrievalScope {
  tags?: string[] | null;
  match?: string;
  exclude_tags?: string[] | null;
}

export function applyTagScope(hits: any[], scope: RetrievalScope | null): any[] {
  const allow = (scope?.tags ?? []).map((t) => String(t).toLowerCase());
  const exclude = (scope?.exclude_tags ?? []).map((t) => String(t).toLowerCase());
  if (!allow.length && !exclude.length) return hits;
  return hits.filter((h) => {
    if (!Array.isArray(h.tags)) return false;
    const tags = h.tags.map((t: string) => String(t).toLowerCase());
    if (exclude.length && tags.some((t: string) => exclude.includes(t))) return false;
    if (allow.length) {
      return scope?.match === "all"
        ? allow.every((t) => tags.includes(t))
        : allow.some((t) => tags.includes(t));
    }
    return true;
  });
}

export async function fetchSopContext(opts: {
  pulpoUrl?: string;
  pulpoToken?: string;
  threshold?: number;
  dispositionCategory: string | null;
  disposition: string | null;
  transcriptText: string;
  summaryQuery?: string | null;
  scope?: RetrievalScope | null;
}): Promise<SopContext> {
  const query = buildSopQuery(
    opts.dispositionCategory,
    opts.disposition,
    opts.transcriptText,
    opts.summaryQuery
  );
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

    // Tag scope filter — applied post-cache so the search cache stays
    // scope-agnostic. Empty result = conservative sop_context_missing path.
    if ((opts.scope?.tags?.length ?? 0) > 0 || (opts.scope?.exclude_tags?.length ?? 0) > 0) {
      hits = applyTagScope(hits, opts.scope ?? null);
      if (!hits.length) return empty(query, "no_hits_in_team_scope");
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
          if (doc && !doc.body_format && hit.body_format) doc.body_format = hit.body_format;
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
        // Scope-audit stamps (2026-08-31 Sofia-leak analysis was done by
        // title matching — tags make contamination queries exact).
        tags: hit.tags ?? [],
        body_format: doc.body_format || hit.body_format || "",
      })),
      skipped_reason: "",
    };
  } catch {
    return empty(query, "provider_error");
  }
}

// ── trigger-time resolution (v0.64) ─────────────────────────────────────────
// Retrieval used to run at ENQUEUE, so a nightly sweep built ~58 payloads —
// and fired ~58 Pulpo retrievals — inside two minutes, blowing the 60/min
// token limit (14/62 provider_error on the 2026-09-01 supervised night).
// The SOP block now resolves at TRIGGER, where the one-platform-slot drain
// serializes jobs minutes apart: natural pacing, no burst. The DISPOSITION
// (the retrieval key) stays frozen at enqueue — only the Pulpo lookup moves.

// Everything fetchSopContext needs, frozen into the queue payload at
// enqueue (plus the static block halves, so resolution is config-free).
export interface DeferredSop {
  disposition_category: string | null;
  disposition: string | null;
  transcript_head: string;
  summary_query: string | null;
  scope: RetrievalScope | null;
  block_parts: SopBlockParts;
}

const replaceAll = (s: string, from: string, to: string) => s.split(from).join(to);

// Resolves payload.sop_deferred in place: fetch SOP context, substitute
// {{SOP_BLOCK}} in both prompt strings, stamp persist provenance, drop the
// marker. NEVER throws and never blocks the trigger: any failure renders
// the conservative missing-note path (sop_context_missing doctrine). A
// payload without the marker (pre-v0.64 queue rows) is returned untouched.
export async function resolveDeferredSop(
  payload: any,
  creds: { pulpoUrl?: string; pulpoToken?: string }
): Promise<any> {
  const d: DeferredSop | undefined = payload?.sop_deferred;
  if (!d) return payload;
  let sop: SopContext;
  try {
    sop = await fetchSopContext({
      pulpoUrl: creds.pulpoUrl,
      pulpoToken: creds.pulpoToken,
      dispositionCategory: d.disposition_category ?? null,
      disposition: d.disposition ?? null,
      transcriptText: d.transcript_head ?? "",
      summaryQuery: d.summary_query ?? null,
      scope: d.scope ?? null,
    });
  } catch {
    sop = empty("", "provider_error"); // belt over fetchSopContext's braces
  }
  const block = renderSopContextBlock(d.block_parts, sop.sop_title, sop.block_text);
  if (payload.judge?.prompt_template)
    payload.judge.prompt_template = replaceAll(
      payload.judge.prompt_template, SOP_BLOCK_PLACEHOLDER, block
    );
  if (payload.single_stage?.prompt)
    payload.single_stage.prompt = replaceAll(
      payload.single_stage.prompt, SOP_BLOCK_PLACEHOLDER, block
    );
  if (payload.persist) {
    payload.persist.sop_used = sop.sop_title || null;
    payload.persist.pulpo_docs = sop.provenance;
    payload.persist.sop_skipped_reason = sop.skipped_reason || null;
  }
  delete payload.sop_deferred;
  return payload;
}
