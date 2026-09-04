#!/usr/bin/env node
// SR1 (ShiftReport §10.3 / §2.1): the shared Dialpad Stats client.
//
// Pins the helpers lifted out of dispositionSweep.ts (parseCsv,
// naiveLocalToIso, localDay, tzOffsetMs, naiveToMs, csvRecords) with fixtures
// shaped like the live exports (fixtures/dialpad_stats_headers.md), and the
// initiate / poll / pollMany contract against a fetch stub — no network.
// Also asserts dispositionSweep still re-exports the names it used to own.
//
//   node tests/dialpad_stats.test.mjs        (from sandy-qa/)

import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";

execFileSync(
  "node_modules/wrangler/node_modules/esbuild/bin/esbuild",
  [
    "src/lib/dialpadStats.ts",
    "--bundle",
    "--format=esm",
    "--outfile=tests/.build/dialpadStats.mjs",
    "--platform=neutral",
  ],
  { stdio: "inherit" }
);
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
const S = await import(new URL("./.build/dialpadStats.mjs", import.meta.url));
const sweep = await import(new URL("./.build/dispositionSweep.mjs", import.meta.url));

let pass = 0;
const failures = [];
const test = async (name, fn) => {
  try {
    await fn();
    pass++;
  } catch (err) {
    failures.push(`${name}: ${err.message}`);
  }
};

// ── CSV ────────────────────────────────────────────────────────────────────

await test("parseCsv handles quotes, escaped quotes, CRLF and blank tail", () => {
  const rows = S.parseCsv('a,b,c\r\n1,"x, y","say ""hi"""\r\n2,,\r\n\r\n');
  assert.deepEqual(rows, [["a", "b", "c"], ["1", "x, y", 'say "hi"'], ["2", "", ""]]);
});

await test("csvRecords maps header → value and pads short rows", () => {
  const recs = S.csvRecords("call_id,category,direction\n1,abandoned,inbound\n2,incoming\n");
  assert.deepEqual(recs, [
    { call_id: "1", category: "abandoned", direction: "inbound" },
    { call_id: "2", category: "incoming", direction: "" },
  ]);
  assert.deepEqual(S.csvRecords(""), []);
});

// ── timezone helpers ───────────────────────────────────────────────────────

await test("naiveLocalToIso localizes Mexico City (fixed UTC-6) and rejects junk", () => {
  assert.equal(S.naiveLocalToIso("2026-09-01 00:05:19.528115", "America/Mexico_City"), "2026-09-01T06:05:19.528Z");
  assert.equal(S.naiveLocalToIso("2026-09-01 23:59:59", "UTC"), "2026-09-01T23:59:59.000Z");
  assert.equal(S.naiveLocalToIso("", "UTC"), null);
  assert.equal(S.naiveLocalToIso("yesterday", "UTC"), null);
});

await test("naiveToMs is the fixed frame used for window math", () => {
  assert.equal(S.naiveToMs("2026-09-01 06:00:00"), Date.UTC(2026, 8, 1, 6, 0, 0));
  assert.equal(S.naiveToMs("2026-09-01 06:00:00.5"), Date.UTC(2026, 8, 1, 6, 0, 0, 500));
  assert.equal(S.naiveToMs("nope"), null);
});

await test("localDay + tzOffsetMs", () => {
  const t = Date.UTC(2026, 8, 3, 3, 30); // 03:30 UTC = 21:30 the day before in Mexico City
  assert.equal(S.localDay("America/Mexico_City", new Date(t)), "2026-09-02");
  assert.equal(S.localDay("UTC", new Date(t)), "2026-09-03");
  assert.equal(S.tzOffsetMs("America/Mexico_City", t), -6 * 3600_000);
});

// ── Stats API contract against a fetch stub ────────────────────────────────

const calls = [];
const stub = (script) => async (url, init = {}) => {
  calls.push({ url: String(url), method: init.method ?? "GET", body: init.body ? JSON.parse(init.body) : null });
  const step = script.shift();
  if (!step) throw new Error(`unexpected fetch ${url}`);
  return {
    ok: step.status < 400,
    status: step.status,
    json: async () => step.json,
    text: async () => (typeof step.json === "string" ? step.json : JSON.stringify(step.json)),
  };
};

await test("initiateExport posts the generalized payload (records/is_today, stats/group_by)", async () => {
  calls.length = 0;
  const f = stub([{ status: 200, json: { request_id: "req-1" } }, { status: 200, json: { request_id: "req-2" } }]);
  const a = await S.initiateExport("K", {
    exportType: "records", statType: "calls", timezone: "America/Mexico_City", targetId: "5699048497577984", isToday: true,
  }, f);
  const b = await S.initiateExport("K", {
    exportType: "stats", statType: "calls", timezone: "America/Mexico_City", targetId: 42, daysAgo: [1, 2], groupBy: "date",
  }, f);
  assert.equal(a, "req-1");
  assert.equal(b, "req-2");
  assert.deepEqual(calls[0].body, {
    export_type: "records", stat_type: "calls", timezone: "America/Mexico_City",
    target_type: "callcenter", target_id: 5699048497577984, is_today: true,
  });
  assert.deepEqual(calls[1].body, {
    export_type: "stats", stat_type: "calls", timezone: "America/Mexico_City",
    target_type: "callcenter", target_id: 42, days_ago_start: 1, days_ago_end: 2, group_by: "date",
  });
  assert.ok(calls[0].url.endsWith("/api/v2/stats"));
});

await test("initiateExport surfaces HTTP errors and missing request ids", async () => {
  await assert.rejects(
    S.initiateExport("K", { exportType: "records", statType: "calls", timezone: "UTC", targetId: 1, daysAgo: [1, 1] },
      stub([{ status: 401, json: { error: "nope" } }])),
    /stats initiate HTTP 401/
  );
  await assert.rejects(
    S.initiateExport("K", { exportType: "records", statType: "calls", timezone: "UTC", targetId: 1, daysAgo: [1, 1] },
      stub([{ status: 200, json: {} }])),
    /no request_id/
  );
  await assert.rejects(
    S.initiateExport("K", { exportType: "records", statType: "calls", timezone: "UTC", targetId: 1 }, stub([])),
    /daysAgo or isToday/
  );
});

await test("pollAndDownload returns null while processing and the CSV once complete (signed URL → no auth)", async () => {
  calls.length = 0;
  const f = stub([
    { status: 200, json: { status: "processing" } },
    { status: 200, json: { status: "complete", download_url: "https://storage.googleapis.com/x.csv" } },
    { status: 200, json: "a,b\n1,2\n" },
  ]);
  const csv = await S.pollAndDownload("K", "req-1", f, 3, 0);
  assert.equal(csv, "a,b\n1,2\n");
  assert.equal(calls.length, 3);
  const dl = calls[2];
  assert.equal(dl.url, "https://storage.googleapis.com/x.csv");
  const still = await S.pollAndDownload("K", "req-1", stub([{ status: 200, json: { status: "processing" } }]), 1, 0);
  assert.equal(still, null);
  await assert.rejects(S.pollOnce("K", "req-1", stub([{ status: 200, json: { status: "failed" } }])), /export failed/);
});

await test("pollMany completes ready exports and leaves the rest for the next tick", async () => {
  // URL-routed (pollMany polls concurrently — a sequential script would interleave)
  const routed = async (url) => {
    url = String(url);
    if (url.endsWith("/stats/ra")) return { ok: true, status: 200, json: async () => ({ status: "complete", download_url: "https://storage.googleapis.com/a.csv" }), text: async () => "" };
    if (url.endsWith("/stats/rb")) return { ok: true, status: 200, json: async () => ({ status: "processing" }), text: async () => "" };
    if (url === "https://storage.googleapis.com/a.csv") return { ok: true, status: 200, json: async () => "A", text: async () => "A" };
    throw new Error(`unexpected ${url}`);
  };
  const got = await S.pollMany("K", { a: "ra", b: "rb" }, routed, 2, 0);
  assert.deepEqual(got, { a: "A" });
});

// ── the sweep still exposes what it used to own ────────────────────────────

await test("dispositionSweep re-exports parseCsv / naiveLocalToIso / localDay and keeps parseExportCsv", () => {
  assert.equal(typeof sweep.parseCsv, "function");
  assert.equal(typeof sweep.naiveLocalToIso, "function");
  assert.equal(typeof sweep.localDay, "function");
  const rows = sweep.parseExportCsv(
    "call_id,disposition,operator_email,direction,recording_url,date_connected,date_ended,timezone\n" +
    "1,Cat~Sub,A@X.com,inbound,https://r,2026-09-01 10:00:00,2026-09-01 10:05:00,America/Mexico_City\n"
  );
  assert.equal(rows.length, 1);
  assert.equal(rows[0].disposition_category, "Cat");
  assert.equal(rows[0].operator_email, "a@x.com");
  assert.equal(rows[0].duration_s, 300);
  assert.equal(rows[0].connected_at, "2026-09-01T16:00:00.000Z");
});

console.log(`${pass} passed, ${failures.length} failed`);
for (const f of failures) console.log("  ✗ " + f);
process.exit(failures.length ? 1 : 0);
