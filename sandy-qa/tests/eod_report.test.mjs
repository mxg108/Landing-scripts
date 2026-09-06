#!/usr/bin/env node
// E2 (ShiftReport §10.5): the EOD Google-Sheet report on the pump.
//
// 1. Pure compute — the SAME synthetic day as the Python reference's
//    tests/test_ms_eod_report.py, expecting the SAME numbers (the Python
//    script stays the backfill tool; both must agree on one sheet).
// 2. State machine on node:sqlite behind the D1 shim, real migration chain
//    0001→0016: tick gate, latch, not-ready resume, credentials-missing →
//    error → retry with fresh exports → completed, already_completed.
// 3. Sheets sink asserted on a URL-routed fetch stub (JWT signed with a
//    throwaway RSA key; token exchange, addSheet, clear, resize+freeze, RAW
//    PUT with the contract headers).
//
//   node tests/eod_report.test.mjs        (from sandy-qa/)

import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { generateKeyPairSync } from "node:crypto";
import assert from "node:assert/strict";

execFileSync(
  "node_modules/wrangler/node_modules/esbuild/bin/esbuild",
  [
    "src/lib/eodReport.ts",
    "--bundle",
    "--format=esm",
    "--outfile=tests/.build/eodReport.mjs",
    "--platform=neutral",
  ],
  { stdio: "inherit" }
);
const E = await import(new URL("./.build/eodReport.mjs", import.meta.url));

let pass = 0;
const failures = [];
const test = async (name, fn) => {
  try {
    await fn();
    pass++;
  } catch (err) {
    failures.push(`${name}: ${err.stack?.split("\n").slice(0, 3).join(" | ") ?? err.message}`);
  }
};

// ── fixtures: identical to tests/test_ms_eod_report.py ─────────────────────

const CALL_COLS = [
  "date_started", "call_id", "target_kind", "category", "direction", "categories",
  "external_number", "internal_number", "date_queued", "date_first_rang", "date_connected",
  "date_ended", "time_to_answer", "talk_duration", "voicemail", "callback_type",
  "entry_point_call_id", "email", "name",
];
const cc = (call_id, started, category, o = {}) => ({
  date_started: started, call_id, target_kind: "CallCenter", category,
  direction: o.direction ?? "inbound", categories: o.categories ?? "",
  external_number: "+15550001111", internal_number: "+12058528798",
  date_queued: o.queued ?? "", date_first_rang: "", date_connected: o.connected ?? "",
  date_ended: o.ended ?? "", time_to_answer: o.tta === undefined ? "" : String(o.tta),
  talk_duration: o.talk === undefined ? "" : String(o.talk), voicemail: "false",
  callback_type: "", entry_point_call_id: "", email: "", name: "",
});
const leg = (call_id, entry, started, email, name = "Agent", connected = "") => ({
  date_started: started, call_id, target_kind: "UserProfile", category: "incoming",
  direction: "inbound", categories: "answered,inbound", external_number: "", internal_number: "",
  date_queued: "", date_first_rang: "", date_connected: connected, date_ended: "",
  time_to_answer: "", talk_duration: "", voicemail: "false", callback_type: "",
  entry_point_call_id: entry, email, name,
});
const RECORDS_D = [
  cc("c1", "2026-09-01 06:10:00", "incoming", { connected: "2026-09-01 06:10:02", ended: "2026-09-01 06:15:02", tta: 0.03, talk: 5.0 }),
  leg("l1", "c1", "2026-09-01 06:10:00", "a@x.com", "A", "2026-09-01 06:10:02"),
  cc("c2", "2026-09-01 13:00:00", "incoming", { connected: "2026-09-01 13:00:45", ended: "2026-09-01 13:10:45", tta: 0.75, categories: "answered,transferred_to" }),
  leg("l2a", "c2", "2026-09-01 13:00:00", "a@x.com", "A"),
  leg("l2b", "c2", "2026-09-01 13:00:10", "b@x.com", "B", "2026-09-01 13:00:45"),
  cc("c3", "2026-09-01 23:30:00", "abandoned", { queued: "2026-09-01 23:30:05", ended: "2026-09-01 23:31:35", categories: "abandoned,unanswered,inbound" }),
  cc("c5", "2026-09-01 10:00:00", "missed", { ended: "2026-09-01 10:00:30", categories: "missed,spam,inbound" }),
  cc("c6", "2026-09-01 11:00:00", "outgoing", { direction: "outbound", connected: "2026-09-01 11:00:05", ended: "2026-09-01 11:03:05" }),
  { ...leg("l7", "c1", "2026-09-01 06:10:00", ""), email: "" },
];
const RECORDS_TODAY = [
  cc("c4", "2026-09-02 02:00:00", "abandoned", { ended: "2026-09-02 02:00:04", categories: "abandoned,unanswered,inbound" }),
];
const DUTY_COLS = ["date", "record_id", "email", "name", "on_duty_status"];
const duty = (date, email, on_duty_status, name = "Agent") => ({ date, record_id: `${email}-${date}`, email, name, on_duty_status });
const DUTY_D = [
  duty("2026-09-01 05:55:00", "a@x.com", "available"),
  duty("2026-09-01 14:00:00", "a@x.com", "unavailable"),
  duty("2026-09-01 12:30:00", "b@x.com", "available"),
  duty("2026-09-01 22:10:00", "b@x.com", "unavailable"),
  duty("2026-09-01 06:00:00", "spec@x.com", "available", "Spec"),
  duty("2026-09-01 15:00:00", "spec@x.com", "unavailable"),
  duty("2026-09-01 22:00:00", "n@x.com", "occupied"),
];
const DAILY = [{
  date: "2026-09-01", inbound_calls: "4", outbound_calls: "1", answered: "2", abandoned: "1",
  short_abandoned: "0", missed: "1", cancelled: "0", spam: "1", voicemails: "0", service_level: "1", asa: "0.39",
}];
const USERS = [{
  date: "2026-09-01", email: "a@x.com", name: "A", type: "user", all_calls: "2", inbound_calls: "2",
  outbound_calls: "0", answered: "2", missed: "0", abandoned: "0", ring_no_answer: "0",
  talk_duration: "5", hold_duration: "0", wrapup_duration: "1",
}];
const toCsv = (cols, rows) =>
  [cols.join(","), ...rows.map((r) => cols.map((c) => {
    const v = String(r[c] ?? "");
    return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
  }).join(","))].join("\n") + "\n";
const DAILY_COLS = Object.keys(DAILY[0]);
const USER_COLS = Object.keys(USERS[0]);
const OPTS = { slSeconds: 30, slTargetPct: 80, shortAbandonS: 6, shifts: E.DEFAULT_SHIFTS, generatedAt: "2026-09-02 07:07 America/Mexico_City" };
const col = (hdr, name) => hdr.indexOf(name);

// ── 1. pure compute (Python parity) ────────────────────────────────────────

await test("shift windows + night straddle + overlap labels", () => {
  const [s, e] = E.shiftWindow("2026-09-01", E.DEFAULT_SHIFTS[2]);
  assert.equal(new Date(s).toISOString(), "2026-09-01T22:00:00.000Z");
  assert.equal(new Date(e).toISOString(), "2026-09-02T06:00:00.000Z");
  assert.deepEqual(E.shiftsContaining(Date.UTC(2026, 8, 2, 2, 0)), ["night(2026-09-01)"]);
  assert.deepEqual(E.shiftsContaining(Date.UTC(2026, 8, 1, 13, 0)), ["morning", "afternoon"]);
  assert.equal(E.windowLabel(E.DEFAULT_SHIFTS[2]), "22:00–06:00 (+1)");
});

await test("buildCalls: entry-point rows only, answering agent joined, outcome rules", () => {
  const { calls, legs } = E.buildCalls([...RECORDS_D, ...RECORDS_TODAY], { slSeconds: 30, shortAbandonS: 6 });
  const by = Object.fromEntries(calls.map((c) => [c.call_id, c]));
  assert.deepEqual(Object.keys(by).sort(), ["c1", "c2", "c3", "c4", "c5", "c6"]);
  assert.equal(by.c1.agent_email, "a@x.com");
  assert.equal(by.c2.agent_email, "b@x.com");
  assert.equal(by.c2.legs, 2);
  assert.equal(by.c2.transferred, true);
  assert.deepEqual(legs.map((l) => l.email), ["a@x.com", "a@x.com", "b@x.com"]);
  assert.equal(by.c1.within_sl, true);
  assert.equal(by.c2.within_sl, false);
  assert.equal(by.c3.within_sl, null);
  assert.equal(by.c3.wait_s, 90);
  assert.equal(by.c4.short_abandoned, true);
  assert.equal(by.c3.short_abandoned, false);
  assert.equal(by.c5.spam, true);
  assert.equal(by.c1.duration_s, 300);
  assert.equal(by.c1.wait_s, 1.8);
});

await test("buildReport reproduces the Python reference numbers", () => {
  const rep = E.buildReport("2026-09-01",
    { records: [...RECORDS_D, ...RECORDS_TODAY], duty: DUTY_D, daily: DAILY, users: USERS }, OPTS);
  const H = E.SUMMARY_HEADER;
  assert.equal(rep.summaryRows.length, 4);
  assert.deepEqual(rep.summaryRows.map((r) => r[1]), ["Full day", "Morning", "Afternoon", "Night"]);
  const full = rep.summaryRows[0];
  assert.equal(full[col(H, "inbound")], 4);
  assert.equal(full[col(H, "outbound")], 1);
  assert.equal(full[col(H, "answered")], 2);
  assert.equal(full[col(H, "abandoned")], 1);
  assert.equal(full[col(H, "missed")], 1);
  assert.equal(full[col(H, "spam")], 1);
  assert.equal(full[col(H, "sl_count")], 1);
  assert.equal(full[col(H, "sl_pct")], 33.3);            // 1 / (4 − 0 short − 1 missed)
  assert.equal(full[col(H, "asa_s")], 23.4);             // official asa 0.39 min
  assert.equal(full[col(H, "avg_wait_abandoned_s")], 90);
  assert.equal(full[col(H, "agents_handled")], 2);
  assert.equal(full[col(H, "agents_on_duty")], 4);
  assert.equal(full[col(H, "reconciliation")], "OK");
  assert.equal(full[col(H, "sl_formula")], "sl_count / (inbound − short_abandoned − missed)");
  const night = rep.summaryRows[3];
  assert.equal(night[col(H, "inbound")], 2);
  assert.equal(night[col(H, "abandoned")], 2);
  assert.equal(night[col(H, "short_abandoned")], 1);
  assert.equal(night[col(H, "sl_pct")], 0);
  assert.equal(night[col(H, "agents_handled")], 0);
  assert.equal(night[col(H, "agents_on_duty")], 2);
  // Calls tab: calendar-day rows only, sorted; c4 (Sep 2) excluded
  assert.deepEqual(rep.callRows.map((r) => r[2]), ["c1", "c5", "c6", "c2", "c3"]);
  assert.ok(rep.callRows.every((r) => r.length === E.CALLS_HEADER.length));
  assert.equal(rep.callRows[4][col(E.CALLS_HEADER, "shifts")], "night");
  assert.equal(rep.callRows[0][col(E.CALLS_HEADER, "dialpad_link")], "https://dialpad.com/callhistory/callreview/c1");
  // Agents tab: union incl. the off-the-phone specialist
  const A = E.AGENTS_HEADER;
  const agents = Object.fromEntries(rep.agentRows.map((r) => [r[2], r]));
  assert.deepEqual(Object.keys(agents).sort(), ["a@x.com", "b@x.com", "n@x.com", "spec@x.com"]);
  assert.equal(agents["spec@x.com"][col(A, "handled_calls")], 0);
  assert.equal(agents["spec@x.com"][col(A, "on_duty")], "Y");
  assert.equal(agents["spec@x.com"][col(A, "on_duty_min")], 540);
  assert.equal(agents["spec@x.com"][col(A, "shifts_on_duty")], "Morning, Afternoon");
  assert.equal(agents["spec@x.com"][col(A, "first_on_duty")], "06:00");
  assert.equal(agents["spec@x.com"][col(A, "last_off_duty")], "15:00");
  assert.equal(agents["a@x.com"][col(A, "all_calls")], 2);
  assert.equal(agents["a@x.com"][col(A, "talk_min")], 5);
  assert.ok(rep.agentRows.every((r) => r.length === A.length));
});

await test("officialDay + slPct match Dialpad Analytics on the probe days (74 % / 67 %)", () => {
  const sep1 = E.officialDay({ inbound_calls: "396", outbound_calls: "110", answered: "353", abandoned: "21",
    short_abandoned: "5", missed: "6", cancelled: "7", spam: "4", voicemails: "1", service_level: "286", asa: "0.7" });
  const sep2 = E.officialDay({ inbound_calls: "345", outbound_calls: "87", answered: "289", abandoned: "37",
    short_abandoned: "11", missed: "0", cancelled: "5", spam: "2", voicemails: "1", service_level: "225", asa: "1.4" });
  assert.equal(sep1.sl_pct, 74.3);
  assert.equal(Math.round(sep2.sl_pct), 67);
  assert.equal(sep1.abandon_pct, 5.3);
  assert.equal(sep1.asa_s, 42);
  assert.equal(E.reconcile(sep1, { ...sep1 }), "OK");
  assert.match(E.reconcile(sep1, { ...sep1, sl_count: 284 }), /^CHECK: sl_count records=284 vs dialpad=286/);
});

await test("neededExports: today-pair for days_ago 1, collapsed range afterwards", () => {
  assert.deepEqual(Object.keys(E.neededExports(1, "1", "UTC")).sort(),
    ["calls:1-1", "calls:today", "daily:1-1", "onduty:1-1", "onduty:today", "users:1-1"]);
  const later = E.neededExports(3, "1", "UTC");
  assert.deepEqual(Object.keys(later).sort(), ["calls:2-3", "daily:3-3", "onduty:2-3", "users:3-3"]);
  assert.deepEqual(later["calls:2-3"].daysAgo, [2, 3]);
  assert.equal(later["daily:3-3"].groupBy, "date");
});

// ── 2 + 3. state machine on sqlite + Sheets stub ───────────────────────────

const raw = new DatabaseSync(":memory:");
for (const f of readdirSync("migrations").sort()) raw.exec(readFileSync(`migrations/${f}`, "utf8"));
const db = {
  prepare(sql) {
    const mk = (params) => ({
      first: async () => raw.prepare(sql).get(...params) ?? null,
      all: async () => ({ results: raw.prepare(sql).all(...params) }),
      run: async () => ({ meta: { changes: Number(raw.prepare(sql).run(...params).changes) } }),
    });
    return { bind: (...params) => mk(params), ...mk([]) };
  },
};
const cfg = {
  callcenter_id: "5699048497577984",
  eod_sheet: {
    enabled: true, spreadsheet_id: "SHEET1", timezone: "America/Mexico_City", local_hour_utc: 13,
    catchup_hours: 6, short_abandon_s: 6, sl_seconds_fallback: 30, sl_target_pct_fallback: 80,
  },
};
raw.prepare("INSERT OR IGNORE INTO teams (id, name, provider) VALUES ('member_support','Member Support','dialpad')").run();
raw.prepare("UPDATE teams SET provider_config = ? WHERE id = 'member_support'").run(JSON.stringify(cfg));

const { privateKey } = generateKeyPairSync("rsa", {
  modulusLength: 2048, privateKeyEncoding: { type: "pkcs8", format: "pem" }, publicKeyEncoding: { type: "spki", format: "pem" },
});
const SA = JSON.stringify({ client_email: "sa@test.iam.gserviceaccount.com", private_key: privateKey });

// URL-routed fetch stub: records every call; `state.ready` gates the exports.
const state = { ready: true, exports: {}, n: 0, puts: {}, log: [], expired: new Set() };
const CSV_BY_KEY = {
  "calls:1-1": toCsv(CALL_COLS, RECORDS_D),
  "calls:today": toCsv(CALL_COLS, RECORDS_TODAY),
  "onduty:1-1": toCsv(DUTY_COLS, DUTY_D),
  "onduty:today": toCsv(DUTY_COLS, []),
  "daily:1-1": toCsv(DAILY_COLS, DAILY),
  "users:1-1": toCsv(USER_COLS, USERS),
  // collapsed range used when a row is resumed after another midnight (days_ago 2)
  "calls:1-2": toCsv(CALL_COLS, [...RECORDS_D, ...RECORDS_TODAY]),
  "onduty:1-2": toCsv(DUTY_COLS, DUTY_D),
  "daily:2-2": toCsv(DAILY_COLS, DAILY),
  "users:2-2": toCsv(USER_COLS, USERS),
};
const keyFor = (body) => {
  const sel = body.is_today ? "today" : `${body.days_ago_start}-${body.days_ago_end}`;
  const kind = body.export_type === "stats" ? (body.group_by === "date" ? "daily" : "users") : body.stat_type;
  return `${kind}:${sel}`;
};
const reply = (json, status = 200) => ({
  ok: status < 400, status,
  json: async () => json,
  text: async () => (typeof json === "string" ? json : JSON.stringify(json)),
});
const fetchStub = async (url, init = {}) => {
  url = String(url);
  const method = init.method ?? "GET";
  state.log.push(`${method} ${url}`);
  if (url === "https://dialpad.com/api/v2/stats" && method === "POST") {
    const id = `req-${++state.n}`;
    state.exports[id] = keyFor(JSON.parse(init.body));
    return reply({ request_id: id });
  }
  let m = url.match(/^https:\/\/dialpad\.com\/api\/v2\/stats\/(req-\d+)$/);
  if (m && state.expired.has(m[1]))
    return { ok: false, status: 400, json: async () => ({}), text: async () => "Results have expired" };
  if (m) return state.ready
    ? reply({ status: "complete", download_url: `https://storage.test/${m[1]}.csv` })
    : reply({ status: "processing" });
  m = url.match(/^https:\/\/storage\.test\/(req-\d+)\.csv$/);
  if (m) return reply(CSV_BY_KEY[state.exports[m[1]]]);
  if (url.startsWith("https://dialpad.com/api/v2/callcenters/"))
    return reply({ alerts: { cc_service_level: "80", cc_service_level_seconds: "30" } });
  if (url === "https://oauth2.googleapis.com/token") {
    assert.ok(String(init.body).includes("grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer"));
    return reply({ access_token: "tok" });
  }
  if (url.startsWith("https://sheets.googleapis.com/v4/spreadsheets/SHEET1")) {
    assert.equal(init.headers?.Authorization, "Bearer tok");
    const rest = url.slice("https://sheets.googleapis.com/v4/spreadsheets/SHEET1".length);
    if (rest.startsWith("?fields="))
      return reply({ sheets: [{ properties: { sheetId: 0, title: "Sheet1", gridProperties: { rowCount: 1000, columnCount: 26 } } },
        ...Object.keys(state.puts).map((t, i) => ({ properties: { sheetId: 10 + i, title: t, gridProperties: { rowCount: 100, columnCount: 5 } } }))] });
    if (rest === ":batchUpdate") {
      const req = JSON.parse(init.body).requests[0];
      if (req.addSheet) return reply({ replies: [{ addSheet: { properties: { sheetId: 99, title: req.addSheet.properties.title, gridProperties: req.addSheet.properties.gridProperties } } }] });
      assert.equal(req.updateSheetProperties.properties.gridProperties.frozenRowCount, 1);
      return reply({});
    }
    m = rest.match(/^\/values\/([^?:]+)(?::clear)?(\?.*)?$/);
    if (m) {
      const title = decodeURIComponent(m[1]).replace("!A1", "").replace(/^'|'$/g, "");
      if (rest.endsWith(":clear")) return reply({});
      if (method === "PUT") {
        assert.ok(rest.includes("valueInputOption=RAW"));
        state.puts[title] = JSON.parse(init.body).values;
        return reply({});
      }
      return reply({ values: state.puts[title] ?? [] });
    }
  }
  throw new Error(`unexpected fetch ${method} ${url}`);
};

const NOW_OPEN = Date.UTC(2026, 8, 2, 13, 7);   // 07:07 Mexico City, Sep 2 → report 2026-09-01
const env = { DIALPAD_API_KEY: "K", GSHEETS_SA_JSON: SA };
const opts = (nowMs, extra = {}) => ({ nowMs, fetchImpl: fetchStub, pollAttempts: 1, pollSpacingMs: 0, ...extra });
const row = () => raw.prepare("SELECT * FROM qa_eod_reports WHERE team_id='member_support' AND report_date='2026-09-01'").get();

await test("gate: nothing happens outside the window, no key → skipped", async () => {
  assert.deepEqual(await E.runEodReports(db, env, opts(Date.UTC(2026, 8, 2, 12, 7))), { member_support: { skipped: "outside_window" } });
  assert.deepEqual(await E.runEodReports(db, {}, opts(NOW_OPEN)), { skipped: "no_dialpad_key" });
  assert.equal(row(), undefined);
});

await test("tick 1: exports initiated (6 selectors), not ready → fetching; ids persisted", async () => {
  state.ready = false;
  const out = await E.runEodReports(db, env, opts(NOW_OPEN));
  assert.equal(out.member_support.status, "fetching");
  const r = row();
  assert.equal(r.status, "fetching");
  const ids = JSON.parse(r.export_ids);
  assert.deepEqual(Object.keys(ids).sort(), ["calls:1-1", "calls:today", "daily:1-1", "onduty:1-1", "onduty:today", "users:1-1"]);
  assert.equal(Object.values(state.exports).sort().join(), Object.keys(ids).sort().join());
  assert.equal(state.n, 6);
});

await test("tick 2 (resume): no re-initiate; credentials missing → error row keeps the aggregates", async () => {
  state.ready = true;
  // an hour later Dialpad has expired every stored id (live behaviour, 2026-09-06)
  for (const id of Object.values(JSON.parse(row().export_ids))) state.expired.add(id);
  const out = await E.runEodReports(db, { DIALPAD_API_KEY: "K" }, opts(Date.UTC(2026, 8, 2, 14, 7)));
  assert.equal(out.member_support.status, "error");
  assert.match(out.member_support.error, /GSHEETS_SA_JSON/);
  assert.equal(state.n, 12);                                 // every expired id re-initiated on the spot
  assert.ok(Object.values(JSON.parse(row().export_ids)).every((id) => !state.expired.has(id))); // new ids persisted
  const rep = JSON.parse(row().report);
  assert.equal(rep.summary.windows["Full day"].inbound, 4);
  assert.equal(rep.summary.windows["Full day"].reconciliation, "OK");
});

await test("tick 3 (retry inside catch-up): fresh exports, sheet written, completed", async () => {
  const out = await E.runEodReports(db, env, opts(Date.UTC(2026, 8, 2, 15, 7)));
  assert.equal(out.member_support.status, "completed", JSON.stringify(out));
  assert.deepEqual(out.member_support.sheet, { summary_rows: 4, calls_rows: 5, agents_rows: 4 });
  assert.equal(state.n, 18);                                 // error retry re-initiates
  assert.deepEqual(state.puts.Summary[0], E.SUMMARY_HEADER);
  assert.deepEqual(state.puts.Calls[0], E.CALLS_HEADER);
  assert.deepEqual(state.puts.Agents_Daily[0], E.AGENTS_HEADER);
  assert.equal(state.puts.Summary.length, 5);
  assert.equal(state.puts.Calls.length, 6);
  assert.equal(state.puts.Summary[1][col(E.SUMMARY_HEADER, "sl_pct")], 33.3);
  assert.equal(typeof state.puts.Calls[1][col(E.CALLS_HEADER, "legs")], "number"); // RAW numbers stay numbers
  const r = row();
  assert.equal(r.status, "completed");
  const rep = JSON.parse(r.report);
  assert.equal(rep.service_level.slSeconds, 30);
  assert.ok(!JSON.stringify(rep).includes("+1555"));          // no caller numbers in D1
});

await test("tick 4: already completed → skipped; rows for the date are replaced, not duplicated", async () => {
  const out = await E.runEodReports(db, env, opts(Date.UTC(2026, 8, 2, 16, 7)));
  assert.deepEqual(out.member_support, { skipped: "already_completed", report_date: "2026-09-01" });
  // force a re-run of the same date through the error path → upsert must not duplicate
  raw.prepare("UPDATE qa_eod_reports SET status='error' WHERE report_date='2026-09-01'").run();
  const again = await E.runEodReports(db, env, opts(Date.UTC(2026, 8, 2, 17, 7)));
  assert.equal(again.member_support.status, "completed");
  assert.equal(state.puts.Summary.length, 5);
  assert.equal(state.puts.Calls.length, 6);
  assert.equal(state.puts.Agents_Daily.length, 5);
});

await test("a stale row from another day is resumed before any new gate decision", async () => {
  raw.prepare("INSERT INTO qa_eod_reports (team_id, report_date, status) VALUES ('member_support','2026-08-30','pending')").run();
  const out = await E.runEodReports(db, env, opts(Date.UTC(2026, 8, 2, 3, 7)));  // outside window, still resumes
  assert.equal(out.member_support.report_date, "2026-08-30");
  assert.equal(out.member_support.status, "completed");
  const ids = JSON.parse(raw.prepare("SELECT export_ids FROM qa_eod_reports WHERE report_date='2026-08-30'").get().export_ids);
  assert.deepEqual(Object.keys(ids).sort(), ["calls:1-2", "daily:2-2", "onduty:1-2", "users:2-2"]); // today-local = Sep 1 21:07 → days_ago 2
});

console.log(`${pass} passed, ${failures.length} failed`);
for (const f of failures) console.log("  ✗ " + f);
process.exit(failures.length ? 1 : 0);
