// Sandy MCP-gateway client for Landing Snowflake (ShiftReport.md §4.3 /
// SupervisorDeliverables.md §4.3). One JSON-RPC `tools/call` against
// `snowflake_sql_exec_tool`, machine-to-machine auth via `X-MCP-Token`
// (an sgmcp_* permission token — Dashboard-only secret SNOWFLAKE_MCP_TOKEN).
// The gateway answers either plain JSON or an SSE stream; both are handled.
//
// Read-only and lagged (Stitch replica, ~30 min): every consumer prints the
// `as_of` it gets back. A missing token or a gateway error is returned as
// {error} — the deliverable renders "unavailable: <reason>", never throws.

import type { FetchLike } from "./dialpadStats.js";

export const SNOWFLAKE_GATEWAY = "https://sandy.hellolanding.tech/mcp-gateway/snowflake";

export interface SqlResult {
  rows: Record<string, unknown>[];
  raw?: string;
}

function parseSse(text: string): any | null {
  // Take the last `data:` payload that parses as JSON-RPC.
  let last: any = null;
  for (const line of text.split(/\r?\n/)) {
    if (!line.startsWith("data:")) continue;
    try {
      const j = JSON.parse(line.slice(5).trim());
      if (j && (j.result || j.error)) last = j;
    } catch {}
  }
  return last;
}

/** Extract tabular rows from whatever the tool returned (JSON array, an
 *  object with rows/data, or a text content block carrying JSON). */
export function rowsFromToolResult(result: any): Record<string, unknown>[] {
  const tryRows = (v: any): Record<string, unknown>[] | null => {
    if (Array.isArray(v)) return v.every((x) => x && typeof x === "object") ? v : null;
    if (v && typeof v === "object") {
      for (const k of ["rows", "data", "results", "records"]) if (Array.isArray(v[k])) return v[k];
    }
    return null;
  };
  const direct = tryRows(result);
  if (direct) return direct;
  if (result?.structuredContent) {
    const s = tryRows(result.structuredContent);
    if (s) return s;
  }
  for (const c of result?.content ?? []) {
    if (c?.type === "text" && typeof c.text === "string") {
      try {
        const j = JSON.parse(c.text);
        const r = tryRows(j);
        if (r) return r;
      } catch {}
    }
  }
  return [];
}

export async function sqlExec(
  token: string | undefined,
  sql: string,
  fetchImpl: FetchLike = fetch
): Promise<SqlResult | { error: string }> {
  if (!token) return { error: "no snowflake token (SNOWFLAKE_MCP_TOKEN)" };
  let res: Response;
  try {
    res = await fetchImpl(SNOWFLAKE_GATEWAY, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json, text/event-stream",
        "X-MCP-Token": token,
      },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "tools/call",
        params: { name: "snowflake_sql_exec_tool", arguments: { sql } },
      }),
      signal: AbortSignal.timeout(25_000),
    });
  } catch (err) {
    return { error: `gateway: ${String((err as any)?.message ?? err).slice(0, 120)}` };
  }
  const text = await res.text();
  if (!res.ok) return { error: `gateway HTTP ${res.status}: ${text.slice(0, 120)}` };
  let rpc: any = null;
  try {
    rpc = JSON.parse(text);
  } catch {
    rpc = parseSse(text);
  }
  if (!rpc) return { error: `gateway: unparseable reply (${text.slice(0, 80)})` };
  if (rpc.error) return { error: `gateway: ${String(rpc.error.message ?? rpc.error).slice(0, 120)}` };
  if (rpc.result?.isError) {
    const msg = (rpc.result.content ?? []).map((c: any) => c?.text ?? "").join(" ").slice(0, 160);
    return { error: `snowflake: ${msg || "tool error"}` };
  }
  return { rows: rowsFromToolResult(rpc.result), raw: text.slice(0, 400) };
}

// ── Mission Control ticket counts (ShiftReport §1.2/§4.3) ──────────────────

export interface TicketCategory {
  label: string;
  type_id?: number;
  reason_id?: number;
}

export interface TicketCounts {
  source: "snowflake:LANDING.MISSION_CONTROL";
  queue_id: number;
  as_of: string | null;
  total_open: number;
  categories: { label: string; new: number; working: number; need_action: number; total: number }[];
}

const num = (v: unknown): number => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};
const col = (row: Record<string, unknown>, name: string): unknown => {
  if (name in row) return row[name];
  const k = Object.keys(row).find((x) => x.toLowerCase() === name.toLowerCase());
  return k ? row[k] : undefined;
};

export function ticketCountsFromRows(
  rows: Record<string, unknown>[],
  categories: TicketCategory[],
  queueId: number,
  asOf: string | null
): TicketCounts {
  const out = categories.map((c) => ({ label: c.label, new: 0, working: 0, need_action: 0, total: 0 }));
  let total = 0;
  for (const r of rows) {
    const type = num(col(r, "TICKET_TYPE_ID"));
    const reason = num(col(r, "TICKET_REASON_ID"));
    const status = num(col(r, "TICKET_STATUS_ID"));
    const n = num(col(r, "N"));
    total += n;
    categories.forEach((c, i) => {
      const hit = (c.reason_id !== undefined && c.reason_id === reason) || (c.reason_id === undefined && c.type_id === type);
      if (!hit) return;
      if (status === 1) out[i].new += n;
      else if (status === 2) out[i].working += n;
      else if (status === 34) out[i].need_action += n;
      out[i].total += n;
    });
  }
  return { source: "snowflake:LANDING.MISSION_CONTROL", queue_id: queueId, as_of: asOf, total_open: total, categories: out };
}

// ── Workforce Management parity (SupervisorDeliverables.md §4.5) ───────────
// The Sigma "Member Support Workforce Management" workbook — the Direction
// index's primary productivity source — reads two warehouse tables
// (TMPAGENTPERFORMANCE_MEMBERSUPPORT: one row per agent status interval
// with STANDARDIZED_STATUS + DURATION_SECONDS; TMPCSAT_MEMBERSUPPORT: one
// row per survey response with AI_HUMAN + RESPONSE + OPERATOR). Their fully
// qualified paths are config (deliverables.wfm.*_table) because the Sigma
// element does not expose them to non-admins. Formulas mirror the workbook:
//   productivity = (logged − Available − Unavailable) / logged
//   CSAT (human) = AVG(RESPONSE) WHERE AI_HUMAN = 'human'

const TABLE_RE = /^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){0,2}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const str = (v: unknown) => (v === null || v === undefined ? "" : String(v));

export interface WfmAgentStatus {
  email: string;
  name: string;
  logged_h: number;
  available_h: number;
  unavailable_h: number;
  productivity_pct: number | null;
  by_status: Record<string, number>; // hours
}

export async function fetchWfmStatusMix(
  token: string | undefined,
  table: string | null | undefined,
  start: string,
  end: string,
  fetchImpl: FetchLike = fetch
): Promise<{ agents: WfmAgentStatus[]; source: string } | { error: string }> {
  if (!table) return { error: "deliverables.wfm.status_table not configured" };
  if (!TABLE_RE.test(table) || !DATE_RE.test(start) || !DATE_RE.test(end)) return { error: "bad wfm table or dates" };
  const res = await sqlExec(
    token,
    `SELECT EMAIL, NAME, STANDARDIZED_STATUS, SUM(DURATION_SECONDS) AS SECS FROM ${table} ` +
      `WHERE EVENT_DATE_ADJUSTED >= '${start}' AND EVENT_DATE_ADJUSTED < DATEADD(day, 1, '${end}') ` +
      `GROUP BY 1,2,3`,
    fetchImpl
  );
  if ("error" in res) return res;
  const per = new Map<string, WfmAgentStatus & { logged: number; avail: number; unavail: number }>();
  for (const r of res.rows) {
    const email = str(col(r, "EMAIL")).toLowerCase();
    if (!email) continue;
    const status = str(col(r, "STANDARDIZED_STATUS"));
    const secs = num(col(r, "SECS"));
    let p = per.get(email);
    if (!p) {
      p = { email, name: str(col(r, "NAME")), logged_h: 0, available_h: 0, unavailable_h: 0, productivity_pct: null, by_status: {}, logged: 0, avail: 0, unavail: 0 };
      per.set(email, p);
    }
    p.logged += secs;
    if (status === "Available") p.avail += secs;
    else if (status === "Unavailable") p.unavail += secs;
    p.by_status[status] = Math.round(((p.by_status[status] ?? 0) + secs / 3600) * 100) / 100;
  }
  const h = (s: number) => Math.round((s / 3600) * 100) / 100;
  const agents = [...per.values()].map((p) => ({
    email: p.email, name: p.name, logged_h: h(p.logged), available_h: h(p.avail), unavailable_h: h(p.unavail),
    productivity_pct: p.logged > 0 ? Math.round(((p.logged - p.avail - p.unavail) / p.logged) * 1000) / 10 : null,
    by_status: p.by_status,
  }));
  agents.sort((a, b) => (b.productivity_pct ?? -1) - (a.productivity_pct ?? -1));
  return { agents, source: `snowflake:${table}` };
}

export interface WfmCsatRow {
  operator: string;
  name: string;
  responses: number;
  avg: number | null;
}

export async function fetchWfmCsat(
  token: string | undefined,
  table: string | null | undefined,
  start: string,
  end: string,
  fetchImpl: FetchLike = fetch
): Promise<{ agents: WfmCsatRow[]; team_avg: number | null; responses: number; source: string } | { error: string }> {
  if (!table) return { error: "deliverables.wfm.csat_table not configured" };
  if (!TABLE_RE.test(table) || !DATE_RE.test(start) || !DATE_RE.test(end)) return { error: "bad wfm table or dates" };
  const res = await sqlExec(
    token,
    `SELECT OPERATOR, OPERATOR_NAME, COUNT(*) AS N, AVG(RESPONSE) AS AVG_RESPONSE FROM ${table} ` +
      `WHERE AI_HUMAN = 'human' AND DATE >= '${start}' AND DATE < DATEADD(day, 1, '${end}') GROUP BY 1,2`,
    fetchImpl
  );
  if ("error" in res) return res;
  let n = 0, sum = 0;
  const agents: WfmCsatRow[] = res.rows.map((r) => {
    const responses = num(col(r, "N"));
    const avg = col(r, "AVG_RESPONSE");
    const a = avg === null || avg === undefined ? null : Math.round(Number(avg) * 100) / 100;
    if (a !== null) {
      n += responses;
      sum += a * responses;
    }
    return { operator: str(col(r, "OPERATOR")).toLowerCase(), name: str(col(r, "OPERATOR_NAME")), responses, avg: a };
  });
  agents.sort((a, b) => (a.avg ?? 99) - (b.avg ?? 99));
  return { agents, team_avg: n ? Math.round((sum / n) * 100) / 100 : null, responses: n, source: `snowflake:${table}` };
}

export async function fetchTicketCounts(
  token: string | undefined,
  queueId: number,
  categories: TicketCategory[],
  fetchImpl: FetchLike = fetch
): Promise<TicketCounts | { error: string }> {
  const q = Math.trunc(Number(queueId)); // ints only — never interpolate user text
  const counts = await sqlExec(
    token,
    `SELECT t.TICKET_TYPE_ID, t.TICKET_REASON_ID, t.TICKET_STATUS_ID, COUNT(*) AS N ` +
      `FROM LANDING.MISSION_CONTROL.TICKETS t ` +
      `WHERE t.DELETED_AT IS NULL AND t.QUEUE_ID = ${q} AND t.TICKET_STATUS_ID IN (1,2,34) ` +
      `GROUP BY 1,2,3`,
    fetchImpl
  );
  if ("error" in counts) return counts;
  let asOf: string | null = null;
  const fresh = await sqlExec(token, `SELECT MAX(_SDC_BATCHED_AT) AS AS_OF FROM LANDING.MISSION_CONTROL.TICKETS`, fetchImpl);
  if (!("error" in fresh) && fresh.rows[0]) {
    const v = col(fresh.rows[0], "AS_OF");
    asOf = v === undefined || v === null ? null : String(v);
  }
  return ticketCountsFromRows(counts.rows, categories, q, asOf);
}
