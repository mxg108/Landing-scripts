#!/usr/bin/env node
// CC4 (CronContinuation.md §2.6): one-shot cron jobs as ticker steps.
//
// node:sqlite behind the D1 shim, migrations 0001→0018; handlers are
// injected stubs (the real ones — qa-insights batch trigger, HR GAS
// dispatch — need the network). Cases:
//   1. enqueue is idempotent per (job, key)
//   2. multi-step job advances one cursor per step, completes, `more` flips
//   3. a throwing handler retries up to 3 attempts, then error (message kept)
//   4. unknown job → error row, the queue moves on
//   5. FIFO across jobs; nothing pending → more:false
//
//   node tests/cron_jobs.test.mjs        (from sandy-qa/)

import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";

execFileSync(
  "node_modules/wrangler/node_modules/esbuild/bin/esbuild",
  [
    "src/lib/cronJobs.ts",
    "--bundle",
    "--format=esm",
    "--outfile=tests/.build/cronJobs.mjs",
    "--platform=neutral",
    "--external:../routes/insights.js",
    "--external:./hrBonus.js",
    "--external:./teamConfig.js",
  ],
  { stdio: "inherit" }
);
const C = await import(new URL("./.build/cronJobs.mjs", import.meta.url));

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
const req = new Request("https://qa-scoring.sandy.hellolanding.tech/api/v1/callbacks/qa-cron-ticker");
const rows = () => raw.prepare("SELECT job, key, status, cursor, attempts, report FROM qa_cron_jobs ORDER BY id").all();

await test("enqueue is idempotent per (job, key)", async () => {
  assert.deepEqual(await C.enqueueCronJob(db, "hr_bonus", "2026-08"), { queued: true });
  assert.deepEqual(await C.enqueueCronJob(db, "hr_bonus", "2026-08"), { queued: false });
  assert.deepEqual(await C.enqueueCronJob(db, "eom_assessments", "2026-08"), { queued: true });
  assert.equal(rows().length, 2);
});

await test("multi-step job advances one cursor per step; more flips on the last", async () => {
  const seen = [];
  const handlers = {
    hr_bonus: async (_db, _req, _env, row, report) => {
      seen.push(row.cursor);
      const teams = ["member_support", "sales", "sofia"];
      const out = { ...report, [teams[row.cursor]]: { ok: true } };
      return { done: row.cursor + 1 >= teams.length, cursor: row.cursor + 1, report: out };
    },
    eom_assessments: async () => ({ done: true, report: { triggered: true, items: 12 } }),
  };
  let r = await C.runCronJobsStep(db, req, {}, handlers);
  assert.equal(r.job, "hr_bonus", "FIFO: the older row first");
  assert.equal(r.status, "running");
  assert.equal(r.cursor, 1);
  assert.equal(r.more, true);
  assert.equal(rows()[0].status, "running");
  r = await C.runCronJobsStep(db, req, {}, handlers);
  assert.equal(r.cursor, 2);
  r = await C.runCronJobsStep(db, req, {}, handlers);
  assert.equal(r.status, "completed");
  assert.equal(r.more, true, "eom_assessments still pending");
  assert.deepEqual(seen, [0, 1, 2]);
  assert.deepEqual(Object.keys(JSON.parse(rows()[0].report)), ["member_support", "sales", "sofia"]);
  r = await C.runCronJobsStep(db, req, {}, handlers);
  assert.equal(r.job, "eom_assessments");
  assert.equal(r.status, "completed");
  assert.equal(r.more, false);
  assert.equal(JSON.parse(rows()[1].report).items, 12);
  r = await C.runCronJobsStep(db, req, {}, handlers);
  assert.deepEqual(r, { more: false });
});

await test("throwing handler retries 3×, then error with the message kept", async () => {
  await C.enqueueCronJob(db, "hr_bonus", "2026-09");
  const handlers = { hr_bonus: async () => { throw new Error("GAS 502"); } };
  let r;
  for (let i = 1; i <= 3; i++) {
    r = await C.runCronJobsStep(db, req, {}, handlers);
    assert.equal(r.attempts, i);
    assert.equal(r.status, i < 3 ? "retry" : "error");
    assert.equal(r.error, "GAS 502");
  }
  const row = rows().find((x) => x.key === "2026-09");
  assert.equal(row.status, "error");
  assert.equal(JSON.parse(row.report).last_error, "GAS 502");
  assert.equal(r.more, false);
});

await test("unknown job → error row; queue moves on", async () => {
  await C.enqueueCronJob(db, "nope", "x");
  await C.enqueueCronJob(db, "hr_bonus", "2026-10");
  let r = await C.runCronJobsStep(db, req, {}, { hr_bonus: async () => ({ done: true }) });
  assert.equal(r.job, "nope");
  assert.equal(r.status, "error");
  assert.equal(r.more, true);
  r = await C.runCronJobsStep(db, req, {}, { hr_bonus: async () => ({ done: true, report: { ok: 1 } }) });
  assert.equal(r.job, "hr_bonus");
  assert.equal(r.status, "completed");
  assert.equal(r.more, false);
});

await test("default handler map names the monthly jobs + the daily digest", () => {
  assert.deepEqual(Object.keys(C.DEFAULT_HANDLERS).sort(), ["daily_digest", "eom_assessments", "hr_bonus"]);
});

console.log(`cron_jobs: ${pass} passed, ${failures.length} failed`);
for (const f of failures) console.log(`  ✗ ${f}`);
process.exit(failures.length ? 1 : 0);
