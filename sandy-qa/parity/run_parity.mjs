#!/usr/bin/env node
// Golden-fixture parity runner (PortManifest §11.3).
//
// Replays the WORKER'S OWN code (loadTeamConfig + fetchHistoryFrame +
// assembleTeamStats/Evals, bundled from src/ via esbuild) against the real
// D1 — through a db shim that routes every prepared statement to
// `sandy.py db query` — pinned to the fixture's max_eval_id and captured
// clock, then deep-diffs the outputs against the Python oracle fixture.
//
//   node parity/run_parity.mjs parity/fixture.json
//
// Exit 0 = parity; exit 2 = diffs (printed, capped).

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const APP_ID = "a2cc5b5a-df29-4ae7-9dbb-e270052015e7";
const SANDY = join(homedir(), ".claude/commands/scripts/sandy.py");
const lib = await import(new URL("./lib.mjs", import.meta.url));

const fixture = JSON.parse(readFileSync(process.argv[2] ?? "parity/fixture.json", "utf8"));
const PIN = fixture.max_eval_id;
const NOW = new Date(fixture.captured_at);

function d1Query(sql) {
  const out = execFileSync("python3", [SANDY, "db", "query", APP_ID, sql], {
    encoding: "utf8", maxBuffer: 256 * 1024 * 1024, timeout: 180_000,
  });
  return JSON.parse(out).data[0].results;
}

// D1Database shim: interpolates bind params, pins the eval-id snapshot.
const db = {
  prepare(sql) {
    return {
      bind(...params) {
        let final = sql;
        for (const p of params) {
          const litv = typeof p === "number" ? String(p) : `'${String(p).replace(/'/g, "''")}'`;
          final = final.replace("?", litv);
        }
        if (final.includes("FROM qa_evaluations e"))
          final = final.replace("ORDER BY e.id", `AND e.id <= ${PIN} ORDER BY e.id`);
        if (final.includes("FROM qa_v_history_long"))
          final += ` AND evaluation_id <= ${PIN}`;
        return {
          all: async () => ({ results: d1Query(final) }),
          first: async () => d1Query(final)[0] ?? null,
        };
      },
    };
  },
};

// ── deep diff with datetime + float tolerance ───────────────────────────────
const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:/;
const IGNORE_KEYS = new Set(["generated_at"]);
const diffs = [];

function record(path, a, b) {
  if (diffs.length < 40) diffs.push(`${path}: py=${JSON.stringify(a)} ts=${JSON.stringify(b)}`);
  else diffs.length === 40 && diffs.push("… (more diffs suppressed)");
}

function cmp(a, b, path) {
  if (a === null || a === undefined) {
    if (b === null || b === undefined) return;
    return record(path, a, b);
  }
  if (typeof a === "string" && typeof b === "string" && ISO_RE.test(a) && ISO_RE.test(b)) {
    if (Math.abs(Date.parse(a) - Date.parse(b)) > 1) record(path, a, b);
    return;
  }
  if (typeof a === "number" && typeof b === "number") {
    if (Math.abs(a - b) > 1e-9) record(path, a, b);
    return;
  }
  if (typeof a === "boolean" || typeof b === "boolean") {
    if (Boolean(a) !== Boolean(b)) record(path, a, b);
    return;
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return record(`${path}.length`, a.length, b.length);
    a.forEach((v, i) => cmp(v, b[i], `${path}[${i}]`));
    return;
  }
  if (typeof a === "object" && typeof b === "object") {
    for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) {
      if (IGNORE_KEYS.has(k)) continue;
      cmp(a[k], b[k], `${path}.${k}`);
    }
    return;
  }
  if (a !== b) record(path, a, b);
}

// frame rows from the TS side, serialized to the fixture's shape
function serializeTsFrame(rows) {
  const iso = (ms) => (ms === null ? null : new Date(ms).toISOString());
  return rows.map((r) => ({
    agent: r.agent, ts: iso(r.ts), eval_approved_at: iso(r.eval_approved_at),
    overall_score: r.overall_score, manager_email: r.manager_email,
    is_active: r.is_active, supervisor: r.supervisor, eval_id: r.eval_id,
    num: r.num, yn: r.yn,
  }));
}

let total = 0;
for (const [teamId, ref] of Object.entries(fixture.teams)) {
  const before = diffs.length;
  const config = await lib.loadTeamConfig(db, teamId);
  cmp(ref.rubric_version, config.rubric_version, `${teamId}.rubric_version`);
  const frame = await lib.fetchHistoryFrame(db, config);
  cmp(ref.frame.length, frame.length, `${teamId}.frame.length`);
  cmp(ref.frame, serializeTsFrame(frame), `${teamId}.frame`);
  for (const [days, refStats] of Object.entries(ref.stats)) {
    const got = lib.assembleTeamStats(frame, config, {
      days: Number(days), active_only: true, supervisor: "",
      date_from: null, date_to: null,
    }, NOW);
    cmp(refStats, got, `${teamId}.stats[days=${days}]`);
  }
  const gotEvals = lib.assembleTeamEvals(
    frame, config, ref.evals_current_month.year_month, true, "");
  cmp(ref.evals_current_month, gotEvals, `${teamId}.evals`);
  total += diffs.length - before;
  console.log(`${teamId}: frame=${frame.length} rows, ${diffs.length - before} diff(s)`);
}

if (diffs.length) {
  console.log("\n== DIFFS ==");
  for (const d of diffs) console.log(" ", d);
  console.log(`\nRESULT: ${diffs.length} mismatch(es)`);
  process.exit(2);
}
console.log("\nRESULT: FULL PARITY — Python oracle == TypeScript port");
