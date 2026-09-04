// Dialpad Stats API client + export helpers (ShiftReport.md §2.1 / SR1).
//
// Lifted verbatim from dispositionSweep.ts (NightlyScoring) so every
// Stats-API consumer — the nightly disposition sweep, the EOD sheet report
// (§10), the shift report (SR2+) — shares ONE initiate / poll / download /
// CSV / timezone implementation. Behaviour is pinned by
// tests/dialpad_stats.test.mjs; the sweep re-exports the names it used to own.
//
// Stats-API facts (probed 2026-07/09, fixtures/dialpad_stats_headers.md):
//   POST /stats {export_type, stat_type, timezone, target_type, target_id,
//                days_ago_start/days_ago_end | is_today, group_by?}
//     → {request_id};  GET /stats/{id} → {status, download_url}
//   export timestamps are NAIVE local time in the row's `timezone` column;
//   every duration column is MINUTES.

export const DIALPAD_BASE = "https://dialpad.com/api/v2";
export const POLL_ATTEMPTS = 4; // ~12s budget per tick; next tick resumes
export const POLL_SPACING_MS = 3_000;

export type FetchLike = typeof fetch;

export interface ExportOpts {
  exportType: "records" | "stats";
  statType: "calls" | "csat" | "dispositions" | "onduty" | "recordings" | "screenshare" | "texts" | "voicemails";
  timezone: string;
  targetId: string | number;
  targetType?: "callcenter" | "office" | "department" | "user";
  /** [days_ago_start, days_ago_end]; ignored when isToday. */
  daysAgo?: [number, number];
  isToday?: boolean;
  /** stats exports only */
  groupBy?: "date" | "group" | "user";
}

// ── CSV ────────────────────────────────────────────────────────────────────

// RFC4180-ish scanner: quoted fields, escaped quotes, \r\n. Disposition
// labels are free text ("Reservation & Stay Changes~Locker / package
// access code") and the note column can carry anything — split(',') is
// not an option.
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let field = "";
  let row: string[] = [];
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else inQuotes = false;
      } else field += c;
    } else if (c === '"') {
      inQuotes = true;
    } else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      field = "";
      if (row.length > 1 || row[0] !== "") rows.push(row);
      row = [];
    } else field += c;
  }
  row.push(field);
  if (row.length > 1 || row[0] !== "") rows.push(row);
  return rows;
}

/** Header-mapped rows (DictReader). Missing cells read as "". */
export function csvRecords(text: string): Record<string, string>[] {
  const rows = parseCsv(text);
  if (!rows.length) return [];
  const header = rows[0].map((h) => h.trim());
  return rows.slice(1).map((r) => {
    const rec: Record<string, string> = {};
    header.forEach((h, i) => (rec[h] = r[i] ?? ""));
    return rec;
  });
}

// ── timezone helpers ───────────────────────────────────────────────────────

// What UTC-rendered wall time does `atMs` read as in `tz`? Fixed-offset
// zones (America/Mexico_City, UTC-6 since DST abolition) make this exact.
export function tzOffsetMs(tz: string, atMs: number): number {
  const parts: Record<string, string> = {};
  new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })
    .formatToParts(new Date(atMs))
    .forEach((p) => (parts[p.type] = p.value));
  const rendered = Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
    Number(parts.hour === "24" ? "0" : parts.hour),
    Number(parts.minute),
    Number(parts.second)
  );
  return rendered - Math.trunc(atMs / 1000) * 1000;
}

// Export timestamps are NAIVE in the row's own timezone column (verified
// on the live CSV + disposition_pull.py::_row_ts) — localize to UTC ISO.
export function naiveLocalToIso(naive: string, tz: string): string | null {
  const m = (naive || "")
    .trim()
    .match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?$/);
  if (!m) return null;
  const ms = m[7] ? Math.round(Number(`0.${m[7]}`) * 1000) : 0;
  const asUtc = Date.UTC(
    Number(m[1]), Number(m[2]) - 1, Number(m[3]),
    Number(m[4]), Number(m[5]), Number(m[6]), ms
  );
  try {
    return new Date(asUtc - tzOffsetMs(tz, asUtc)).toISOString();
  } catch {
    return null;
  }
}

/** Naive export timestamp → epoch ms AS IF it were UTC (a fixed frame for
 *  window math and deltas; never mix with real UTC instants). null on junk. */
export function naiveToMs(naive: string): number | null {
  const m = (naive || "")
    .trim()
    .match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?$/);
  if (!m) return null;
  const ms = m[7] ? Math.round(Number(`0.${m[7]}`) * 1000) : 0;
  return Date.UTC(
    Number(m[1]), Number(m[2]) - 1, Number(m[3]),
    Number(m[4]), Number(m[5]), Number(m[6]), ms
  );
}

export function localDay(tz: string, d: Date): string {
  return new Intl.DateTimeFormat("sv-SE", { timeZone: tz }).format(d);
}

// ── Stats API (initiate / poll / download) ─────────────────────────────────

function authHeaders(apiKey: string): Record<string, string> {
  return { Authorization: `Bearer ${apiKey}`, Accept: "application/json" };
}

export async function initiateExport(
  apiKey: string,
  opts: ExportOpts,
  fetchImpl: FetchLike = fetch
): Promise<string> {
  const body: Record<string, unknown> = {
    export_type: opts.exportType,
    stat_type: opts.statType,
    timezone: opts.timezone,
    target_type: opts.targetType ?? "callcenter",
    target_id: Number(opts.targetId),
  };
  if (opts.isToday) body.is_today = true;
  else if (opts.daysAgo) {
    body.days_ago_start = opts.daysAgo[0];
    body.days_ago_end = opts.daysAgo[1];
  } else throw new Error("initiateExport: daysAgo or isToday required");
  if (opts.groupBy) body.group_by = opts.groupBy;
  const res = await fetchImpl(`${DIALPAD_BASE}/stats`, {
    method: "POST",
    headers: { ...authHeaders(apiKey), "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) throw new Error(`stats initiate HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const json: any = await res.json();
  const requestId = json?.request_id;
  if (!requestId) throw new Error(`stats initiate: no request_id in ${JSON.stringify(json).slice(0, 200)}`);
  return String(requestId);
}

/** One status check. CSV text when complete, null while processing;
 *  throws when Dialpad reports the export failed. */
export async function pollOnce(
  apiKey: string,
  requestId: string,
  fetchImpl: FetchLike = fetch
): Promise<string | null> {
  const res = await fetchImpl(`${DIALPAD_BASE}/stats/${requestId}`, {
    headers: authHeaders(apiKey),
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) throw new Error(`stats poll HTTP ${res.status}`);
  const body: any = await res.json();
  if (body?.status === "failed") throw new Error(`stats export failed: ${JSON.stringify(body).slice(0, 200)}`);
  if (body?.status === "complete" && body?.download_url) {
    const url = String(body.download_url);
    // Signed storage links need no auth; only send the key back to Dialpad.
    const isDialpad = url.startsWith("https://dialpad.com/");
    const dl = await fetchImpl(url, {
      headers: isDialpad ? { Authorization: `Bearer ${apiKey}` } : {},
      redirect: "follow",
      signal: AbortSignal.timeout(60_000),
    });
    if (!dl.ok) throw new Error(`stats download HTTP ${dl.status}`);
    return await dl.text();
  }
  return null;
}

// One bounded poll pass. Returns the CSV text when the export is ready,
// null when still processing (the next hourly tick resumes), throws when
// Dialpad reports the export failed.
export async function pollAndDownload(
  apiKey: string,
  requestId: string,
  fetchImpl: FetchLike = fetch,
  attempts: number = POLL_ATTEMPTS,
  spacingMs: number = POLL_SPACING_MS
): Promise<string | null> {
  for (let attempt = 0; attempt < attempts; attempt++) {
    if (attempt > 0) await new Promise((r) => setTimeout(r, spacingMs));
    const csv = await pollOnce(apiKey, requestId, fetchImpl);
    if (csv !== null) return csv;
  }
  return null;
}

/** Poll several exports inside ONE bounded budget. Returns the CSV per key
 *  for those that completed; keys still processing are absent. */
export async function pollMany(
  apiKey: string,
  requestIds: Record<string, string>,
  fetchImpl: FetchLike = fetch,
  attempts: number = POLL_ATTEMPTS,
  spacingMs: number = POLL_SPACING_MS
): Promise<Record<string, string>> {
  const done: Record<string, string> = {};
  const pending = Object.keys(requestIds);
  for (let attempt = 0; attempt < attempts && pending.length; attempt++) {
    if (attempt > 0) await new Promise((r) => setTimeout(r, spacingMs));
    const results = await Promise.all(
      pending.map((key) => pollOnce(apiKey, requestIds[key], fetchImpl).then((csv) => [key, csv] as const))
    );
    for (const [key, csv] of results) {
      if (csv !== null) {
        done[key] = csv;
        pending.splice(pending.indexOf(key), 1);
      }
    }
  }
  return done;
}
