#!/usr/bin/env node
// CC1 (CronContinuation.md §7): the nightly disposition sweep as a phase
// state machine driven by bounded ticker steps.
//
// node:sqlite behind the D1 shim, real migration chain 0001→0017; the
// Dialpad Stats API is a URL-routed fetch stub; ../routes/scoring.js is the
// recording stub in tests/routes/. Cases:
//   1. full cycle — initiate → poll (not ready) → poll (ready) → fill ×N →
//      select → enqueue ×N → completed; every step bounded; `more` flips
//      false exactly once; report matches the pre-refactor shape.
//   2. expired export id (404) → re-initiated on the spot (≤3), then error.
//   3. resume mid-enqueue: a cut step re-does at most one chunk, never
//      over-enqueues (one-attempt dedupe via qa_score_queue).
//   4. window/latch: outside window → skipped; completed → skipped;
//      error inside window → fresh export on the same row.
//   5. sweepHasMore aggregator.
//
//   node tests/cron_ticker.test.mjs        (from sandy-qa/)

import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";

execFileSync(
  "node_modules/wrangler/node_modules/esbuild/bin/esbuild",
  [
    "src/lib/dispositionSweep.ts",
    "--bundle",
    "--format=esm",
    "--outfile=tests/.build/dispositionSweep.mjs",
    "--platform=neutral",
    "--external:../routes/scoring.js",
  ],
  { stdio: "inherit" }
);
const S = await import(new URL("./.build/dispositionSweep.mjs", import.meta.url));
const scoringStub = await import(new URL("./routes/scoring.js", import.meta.url));

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

// ── D1 shim on node:sqlite (same as eod_report.test.mjs) ───────────────────
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
  async batch(stmts) {
    return Promise.all(stmts.map((s) => s.run()));
  },
};

const TEAM = "member_support";
const cfg = {
  callcenter_id: "5699048497577984",
  nightly_sweep: {
    enabled: true, per_agent: 2, min_duration_s: 60, max_duration_s: 1800,
    timezone: "America/Mexico_City", local_hour_utc: 6, suppress_email: true,
    reviewer_email: "qa-system@hellolanding.com",
  },
};
raw.prepare("INSERT OR IGNORE INTO teams (id, name, provider) VALUES (?, 'Member Support', 'dialpad')").run(TEAM);
raw.prepare("UPDATE teams SET provider_config = ? WHERE id = ?").run(JSON.stringify(cfg), TEAM);
for (const [i, email] of ["a@x.com", "b@x.com", "c@x.com"].entries())
  raw.prepare("INSERT INTO qa_agents (team_id, name, email, active) VALUES (?, ?, ?, 1)").run(TEAM, `Agent ${i}`, email);

// ── Dialpad Stats stub ──────────────────────────────────────────────────────
// 2026-09-15 local (MX, UTC-6) — the export's day. "now" = 2026-09-16 07:07 UTC.
const NOW = Date.parse("2026-09-16T07:07:00Z");
const dispositionCsv = () => {
  const cols = ["call_id", "disposition", "operator_email", "direction", "recording_url", "date_connected", "date_ended", "timezone"];
  const rows = [cols.join(",")];
  let n = 0;
  const add = (email, cat, sub, durS, rec = true) => {
    n++;
    const id = `${n}`.padStart(16, "9");
    const start = `2026-09-15 10:${String(n).padStart(2, "0")}:00.000000`;
    const endMin = n + Math.floor(durS / 60);
    const end = `2026-09-15 ${String(10 + Math.floor(endMin / 60)).padStart(2, "0")}:${String(endMin % 60).padStart(2, "0")}:${String(durS % 60).padStart(2, "0")}.000000`;
    rows.push([id, cat ? `${cat}${sub ? "~" + sub : ""}` : "", email, "inbound",
      rec ? `https://dialpad.com/rec/${id}` : "", start, end, "America/Mexico_City"].join(","));
    return id;
  };
  const ids = {};
  ids.a1 = add("a@x.com", "Billing", "Refund", 300);
  ids.a2 = add("a@x.com", "Access", "", 400);
  ids.a3 = add("a@x.com", "Billing", "Charge", 500);
  ids.b1 = add("b@x.com", "Access", "Lockout", 320);
  ids.b2 = add("b@x.com", "", "", 330); // undispositioned but eligible
  ids.bShort = add("b@x.com", "Access", "", 30); // too short
  ids.cNoRec = add("c@x.com", "Billing", "", 400, false); // no audio
  ids.zUnknown = add("zz@nowhere.com", "Billing", "", 400); // not on roster
  return { text: rows.join("\n"), ids, total: n };
};

const stats = { initiated: 0, ready: false, pollStatus: 200, csv: dispositionCsv() };
const fetchImpl = async (url, init = {}) => {
  const u = String(url);
  if (u.endsWith("/stats") && init.method === "POST") {
    stats.initiated++;
    return new Response(JSON.stringify({ request_id: `req-${stats.initiated}` }), { status: 200 });
  }
  if (u.includes("/stats/req-")) {
    if (stats.pollStatus !== 200) return new Response("nope", { status: stats.pollStatus });
    if (!stats.ready) return new Response(JSON.stringify({ status: "processing" }), { status: 200 });
    return new Response(JSON.stringify({ status: "complete", download_url: "https://storage.example/x.csv" }), { status: 200 });
  }
  if (u.startsWith("https://storage.example/")) return new Response(stats.csv.text, { status: 200 });
  throw new Error(`unexpected fetch ${u}`);
};

const env = { DIALPAD_API_KEY: "k" };
const req = new Request("https://qa-scoring.sandy.hellolanding.tech/_sandy/cron");
const step = (nowMs = NOW) => S.sweepDispositions(db, req, env, { nowMs, rng: () => 0.42, fetchImpl });
const row = () => raw.prepare("SELECT * FROM qa_disposition_pulls WHERE team_id = ? AND pull_date = '2026-09-15'").get(TEAM);

// ── 1. full cycle ───────────────────────────────────────────────────────────
await test("full cycle: bounded phases, more flips once, report shape", async () => {
  let r = await step();
  assert.equal(r[TEAM].phase, "poll");
  assert.equal(r[TEAM].more, true);
  assert.equal(row().status, "fetching");
  assert.equal(row().request_id, "req-1");

  r = await step(); // not ready
  assert.equal(r[TEAM].phase, "poll");
  assert.match(r[TEAM].note, /not ready/);
  assert.equal(stats.initiated, 1, "no re-initiate while merely processing");

  stats.ready = true;
  r = await step(); // ready → parsed → fill
  assert.equal(r[TEAM].phase, "fill");
  assert.equal(r[TEAM].rows_in_export, stats.csv.total);
  assert.equal(row().phase, "fill");
  assert.ok(row().export_json.length > 100);

  r = await step(); // fill (all dispositioned rows fit in one chunk) → select
  assert.equal(r[TEAM].phase, "select");
  assert.equal(JSON.parse(row().report).fill_missing, JSON.parse(row().report).with_disposition, "no cc rows mirrored in this fixture → nothing filled");

  r = await step(); // select → enqueue
  assert.equal(r[TEAM].phase, "enqueue");
  const rep = JSON.parse(row().report);
  assert.equal(rep.agents_unmatched, 1);
  assert.equal(rep.agents_matched, 2, "c has no audio → not eligible");
  assert.equal(rep.eligible, 5);
  assert.equal(rep.selected, 4, "per_agent 2 × 2 agents");
  assert.equal(r[TEAM].to_enqueue, 4);
  assert.equal(row().export_json, null, "export rows dropped at select");
  const picks = JSON.parse(row().picks);
  assert.equal(picks.length, 4);
  const aPicks = picks.filter((p) => p.agent_email === "a@x.com").map((p) => p.disposition_category);
  assert.deepEqual([...new Set(aPicks)].sort(), ["Access", "Billing"], "distinct-disposition preference");

  scoringStub.calls.length = 0;
  r = await step(); // enqueue 3
  assert.equal(r[TEAM].phase, "enqueue");
  assert.equal(r[TEAM].enqueued_so_far, 3);
  assert.equal(scoringStub.calls.length, 3);
  assert.equal(scoringStub.calls[0].suppressEmail, true);
  assert.equal(scoringStub.calls[0].managerEmail, "qa-system@hellolanding.com");
  assert.ok(scoringStub.calls[0].statsContext.connected_at?.endsWith("Z"), "stats context carries ISO UTC");
  assert.equal(r[TEAM].more, true);

  r = await step(); // enqueue last 1 → completed
  assert.equal(r[TEAM].status, "completed");
  assert.equal(r[TEAM].more, false);
  assert.equal(r[TEAM].enqueued, 4);
  assert.equal(r[TEAM].errors, 0);
  assert.equal(scoringStub.calls.length, 4);
  const done = row();
  assert.equal(done.status, "completed");
  assert.equal(done.phase, "done");
  assert.equal(done.picks, null);
  const final = JSON.parse(done.report);
  for (const k of ["rows_in_export", "with_disposition", "fill_updated", "evals_backfilled", "fill_missing",
    "agents_matched", "agents_unmatched", "eligible", "selected", "enqueued", "skipped_existing", "errors"])
    assert.ok(k in final, `report has ${k}`);

  r = await step(); // latch
  assert.equal(r[TEAM].skipped, "already_completed");
  assert.equal(r[TEAM].more, false);
});

// ── 2. expired export id → re-initiate ≤3 ───────────────────────────────────
await test("expired export id is re-initiated on the spot, bounded", async () => {
  raw.prepare("DELETE FROM qa_disposition_pulls").run();
  stats.initiated = 0;
  stats.ready = false;
  await step(); // initiate → req-1
  stats.pollStatus = 404;
  let r = await step();
  assert.match(r[TEAM].note, /re-initiated \(1\)/);
  assert.equal(row().request_id, "req-2");
  assert.equal(row().reinits, 1);
  await step();
  await step();
  assert.equal(row().reinits, 3);
  r = await step(); // 4th expiry → error
  assert.equal(r[TEAM].status, "error");
  assert.match(r[TEAM].error, /expired 4×/);
  assert.equal(r[TEAM].more, false);
  assert.equal(row().status, "error");
  // inside the window an error row gets a FRESH export on the same row
  stats.pollStatus = 200;
  r = await step();
  assert.equal(r[TEAM].phase, "poll");
  assert.equal(row().status, "fetching");
  assert.equal(row().reinits, 0);
  assert.equal(row().request_id, "req-5");
  const n = raw.prepare("SELECT COUNT(*) AS n FROM qa_disposition_pulls").get().n;
  assert.equal(n, 1, "same row, never a second one");
});

// ── 3. resume mid-enqueue never over-enqueues ───────────────────────────────
await test("cut step mid-enqueue: resume re-does at most one chunk, dedupes", async () => {
  raw.prepare("DELETE FROM qa_disposition_pulls").run();
  raw.prepare("DELETE FROM qa_score_queue").run();
  stats.initiated = 0;
  stats.ready = true;
  stats.pollStatus = 200;
  await step(); // initiate
  await step(); // poll → fill
  await step(); // fill → select
  await step(); // select → enqueue (4 picks)
  scoringStub.calls.length = 0;
  await step(); // enqueue 3, cursor 3
  assert.equal(row().cursor, 3);
  // simulate a cut step: the queue rows landed but the cursor UPDATE did not
  const picks = JSON.parse(row().picks);
  for (const p of picks.slice(0, 3))
    raw.prepare("INSERT INTO qa_score_queue (job_id, team_id, call_id, payload, status, enqueued_at) VALUES (?, ?, ?, '{}', 'queued', '2026-09-16T07:07:00Z')")
      .run(`score-${TEAM}-${p.call_id}`, TEAM, p.call_id);
  raw.prepare("UPDATE qa_disposition_pulls SET cursor = 0 WHERE team_id = ?").run(TEAM);
  scoringStub.calls.length = 0;
  const r = await step(); // re-does the first chunk — the stub records the calls;
  // the real trigger 409-dedupes on workflow_runs/evals; here the chunk is simply re-sent
  assert.equal(scoringStub.calls.length, 3);
  assert.equal(r[TEAM].enqueued_so_far, 3);
  // …and a resume from `select` re-computes picks against qa_score_queue (one-attempt rule)
  raw.prepare("UPDATE qa_disposition_pulls SET phase = 'select', cursor = 0, export_json = ? WHERE team_id = ?")
    .run(JSON.stringify(S.parseExportCsv(stats.csv.text)), TEAM);
  const r2 = await step();
  assert.equal(r2[TEAM].phase, "enqueue");
  const rep = JSON.parse(row().report);
  assert.equal(rep.selected, 1, "3 queued calls already consumed their agents' slots");
});

// ── 4. window + latch ───────────────────────────────────────────────────────
await test("outside the window nothing starts; in-flight rows resume anywhere", async () => {
  raw.prepare("DELETE FROM qa_disposition_pulls").run();
  let r = await step(Date.parse("2026-09-16T03:07:00Z"));
  assert.equal(r[TEAM].skipped, "outside_window");
  assert.equal(r[TEAM].more, false);
  await step(); // 07:07 → initiate
  r = await step(Date.parse("2026-09-16T13:07:00Z")); // outside window but fetching → resumes
  assert.equal(r[TEAM].phase, "fill");
});

// ── 5. aggregator ───────────────────────────────────────────────────────────
await test("sweepHasMore", () => {
  assert.equal(S.sweepHasMore(null), false);
  assert.equal(S.sweepHasMore({ skipped: "no_dialpad_key" }), false);
  assert.equal(S.sweepHasMore({ member_support: { more: false }, sales: { more: true } }), true);
  assert.equal(S.sweepHasMore({ member_support: { skipped: "outside_window", more: false } }), false);
});

console.log(`cron_ticker: ${pass} passed, ${failures.length} failed`);
for (const f of failures) console.log(`  ✗ ${f}`);
process.exit(failures.length ? 1 : 0);
