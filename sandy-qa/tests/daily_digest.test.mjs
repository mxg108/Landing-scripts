#!/usr/bin/env node
// DailyDigest.md §6 — DD1/DD2 checkpoints.
//
// node:sqlite behind the D1 shim, migrations 0001→0021. Cases:
//   1. migration 0021: MS digest config present, next_at column exists
//   2. facts: cohort = finalized picks only, per-section stats (numeric +
//      yn, NA excluded), month window (LA calendar month), progression
//      order (5 prior + tonight flagged), pending-review count
//   3. prompt lists every section; summary parser accepts fenced JSON and
//      rejects missing section / bad trend / non-JSON
//   4. renderer: sections, headline, focus, month line, highlight, links;
//      facts-only fallback when the summary is missing
//   5. handler phase walk on stubs: await_scores (wait) → summarize
//      (insights payload) → await_summary (wait) → callback persist (ok +
//      failed item) → send (GAS stub, per-agent) → done; inactive agents
//      excluded; deadline path; missing GAS url → skipped
//   6. runner: wait_s parks the row (next_at), `more` false, resumes later
//
//   node tests/daily_digest.test.mjs        (from sandy-qa/)

import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";

for (const [entry, out, externals] of [
  ["src/lib/dailyDigest.ts", "tests/.build/dailyDigest.mjs", ["../routes/insights.js"]],
  ["src/lib/cronJobs.ts", "tests/.build/cronJobs_dd.mjs", ["../routes/insights.js", "./hrBonus.js", "./teamConfig.js", "./dailyDigest.js"]],
]) {
  execFileSync(
    "node_modules/wrangler/node_modules/esbuild/bin/esbuild",
    [entry, "--bundle", "--format=esm", `--outfile=${out}`, "--platform=neutral", ...externals.map((e) => `--external:${e}`)],
    { stdio: "inherit" }
  );
}
const D = await import(new URL("./.build/dailyDigest.mjs", import.meta.url));
const C = await import(new URL("./.build/cronJobs_dd.mjs", import.meta.url));

let pass = 0;
const failures = [];
const test = async (name, fn) => {
  try {
    await fn();
    pass++;
  } catch (err) {
    failures.push(`${name}: ${err.stack?.split("\n").slice(0, 6).join(" | ") ?? err.message}`);
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
const TEAM = "member_support";
const PULL = "2026-09-17";
const NOW = Date.parse("2026-09-18T10:00:00Z"); // 04:00 MX, before the 13:30 UTC deadline

// ── seeds ──────────────────────────────────────────────────────────────────
const RUBRIC = {
  rubric_version: "member_support_vT",
  scoring_prompt: { system_prompt_template: "x", confidence_levels_note: "x", sop_sections: [5], long_call_focus_sections: [] },
  sections: [
    { id: "caller_id", history_id: "caller_id", name: "Caller ID", section_number: 2, score_type: "yn", score_range: null, audio_dependent: false, na_applicable: true, rubric_question: "" },
    { id: "process_adherence", history_id: "process_adherence", name: "Process Adherence", section_number: 5, score_type: "numeric", score_range: [1, 5], audio_dependent: false, na_applicable: false, rubric_question: "" },
    { id: "efficiency", history_id: "efficiency", name: "Efficiency", section_number: 8, score_type: "numeric", score_range: [1, 5], audio_dependent: true, na_applicable: false, rubric_question: "" },
    { id: "human_review_required", history_id: "human_review_required", name: "Human Review Required", section_number: 9, score_type: "manual", score_range: [1, 5], audio_dependent: false, na_applicable: true, rubric_question: "" },
  ],
};
const FORMULA = {
  formula_id: "ms_vT", scale: { min: 0, max: 100 }, evaluation_order: ["weighted_sum"],
  normalization: { na: "redistribute_per_rules", binary_yn: { N: 0, Y: 1 }, rating_1_5: { type: "linear", output: [0, 1], input_max: 5, input_min: 1 } },
  rules: [],
  sections: [
    { key: "caller_id", label: "Caller ID", weight: 20, score_type: "binary_yn_na", na_default: false, binary_map: null, trigger: null, category: null },
    { key: "process_adherence", label: "Process Adherence", weight: 40, score_type: "rating_1_5", na_default: false, binary_map: null, trigger: null, category: null },
    { key: "efficiency", label: "Efficiency", weight: 40, score_type: "rating_1_5", na_default: false, binary_map: null, trigger: null, category: null },
  ],
  human_review_triggers: [],
};
raw.prepare("INSERT INTO qa_rubric_versions (team_id, rubric_version, rubric_json, effective_from) VALUES (?,?,?,?)").run(TEAM, "member_support_vT", JSON.stringify(RUBRIC), "2026-08-01");
raw.prepare("INSERT INTO qa_formula_versions (formula_version, team_id, formula_json, effective_from) VALUES (?,?,?,?)").run("ms_vT", TEAM, JSON.stringify(FORMULA), "2026-08-01");
raw.prepare("INSERT INTO qa_agents (id, team_id, name, canonical_name, email, supervisor_email, active) VALUES (9001,?,'ana perez','Ana Perez','ana@x.com','sup@x.com',1)").run(TEAM);
raw.prepare("INSERT INTO qa_agents (id, team_id, name, email, active) VALUES (9002,?,'Ben','ben@x.com',1)").run(TEAM);
raw.prepare("INSERT INTO qa_agents (id, team_id, name, email, active) VALUES (9003,?,'Cy','cy@x.com',0)").run(TEAM);

let nextId = 10000201;
function seedEval({ agent, call, ts, overall, state = "finalized", status = "complete", pa = 4, eff = 3, yn = "Y", meta = null }) {
  const id = nextId++;
  const a = { 9001: ["Ana Perez", "ana@x.com"], 9002: ["Ben", "ben@x.com"], 9003: ["Cy", "cy@x.com"] }[agent];
  raw.prepare(
    `INSERT INTO qa_evaluations (id, team_id, agent_id, agent_name_raw, agent_email, state, source, dialpad_call_id,
       models_used, formula_version, rubric_version, scoring_status, call_connected_at, overall_score,
       dialpad_disposition_category, dialpad_disposition, call_summary, key_strengths, opportunities, dialpad_call_metadata,
       evaluator_email, approved_at, finalized_at)
     VALUES (?, ?, ?, ?, ?, ?, 'ai', ?, '{}', 'ms_vT', 'member_support_vT', ?, ?, ?, 'Parking', 'permit', 'summary', 'strong', 'opps', ?, ?, ?, ?)`
  ).run(id, TEAM, agent, a[0], a[1], state, call, status, ts, overall, meta,
        state === "finalized" ? "qa-system@hellolanding.com" : null, state === "finalized" ? ts : null, state === "finalized" ? ts : null);
  const ins = raw.prepare(
    `INSERT INTO qa_evaluation_sections (id, evaluation_id, section_id, section_number, score_type, numeric_score, binary_value, score_source, ai_provider, confidence, reasoning)
     VALUES (?, ?, ?, ?, ?, ?, ?, 'ai', 'gemini', 'medium', ?)`
  );
  ins.run(id * 100 + 2, id, "caller_id", 2, "binary", null, yn, "id reasoning");
  ins.run(id * 100 + 5, id, "process_adherence", 5, "numeric", pa, null, "Per SOP 1 (Parking Flow), you registered the vehicle correctly.");
  ins.run(id * 100 + 8, id, "efficiency", 8, "numeric", eff, null, "One 90 s unannounced hold.");
  return id;
}
// Ana: 3 prior evals (2 this LA-month, 1 last month), 2 finalized picks tonight, 1 flagged pick.
const anaPrior = [
  seedEval({ agent: 9001, call: "c-aug", ts: "2026-08-20T15:00:00Z", overall: 60, pa: 3, eff: 2 }),
  seedEval({ agent: 9001, call: "c-sep1", ts: "2026-09-05T15:00:00Z", overall: 70, pa: 3, eff: 3 }),
  seedEval({ agent: 9001, call: "c-sep2", ts: "2026-09-10T15:00:00Z", overall: 80, pa: 4, eff: 3, yn: "NA" }),
];
const anaTonight = [
  seedEval({ agent: 9001, call: "c-t1", ts: "2026-09-17T20:00:00Z", overall: 88, pa: 5, eff: 3, meta: JSON.stringify({ pulpo_docs: [{ title: "Parking (Flow)" }] }) }),
  seedEval({ agent: 9001, call: "c-t2", ts: "2026-09-17T22:00:00Z", overall: 72, pa: 4, eff: 2, yn: "N" }),
];
const anaFlagged = seedEval({ agent: 9001, call: "c-t3", ts: "2026-09-17T23:00:00Z", overall: null, state: "draft", status: "flagged_human_review" });
const benTonight = seedEval({ agent: 9002, call: "c-b1", ts: "2026-09-17T21:00:00Z", overall: 55, pa: 2, eff: 2 });
const cyTonight = seedEval({ agent: 9003, call: "c-c1", ts: "2026-09-17T21:00:00Z", overall: 90 });
const PICKS = ["c-t1", "c-t2", "c-t3", "c-b1", "c-c1", "c-missing"];
raw.prepare("INSERT INTO qa_disposition_pulls (team_id, pull_date, status, phase, report) VALUES (?,?,'completed','done',?)").run(TEAM, PULL, JSON.stringify({ enqueued: 6, picked_call_ids: PICKS }));
for (const c of PICKS) raw.prepare("INSERT INTO qa_score_queue (job_id, team_id, call_id, payload, status, enqueued_at) VALUES (?,?,?,'{}',?,?)").run(`score-${c}`, TEAM, c, c === "c-missing" ? "running" : "done", "2026-09-18T06:10:00Z");


// ── 1. migration ───────────────────────────────────────────────────────────
await test("0021: MS digest config + next_at column", async () => {
  const t = raw.prepare("SELECT provider_config FROM teams WHERE id = ?").get(TEAM);
  const cfg = D.digestConfigFor(t.provider_config);
  assert.equal(cfg?.enabled, true);
  assert.equal(cfg?.send_deadline_utc, 13.5);
  assert.equal(cfg?.model, "claude-sonnet-5");
  assert.ok(raw.prepare("PRAGMA table_info(qa_cron_jobs)").all().some((c) => c.name === "next_at"));
  assert.equal(D.digestConfigFor(JSON.stringify({ nightly_sweep: { digest: { enabled: false } } })), null);
  assert.equal(D.digestConfigFor(null), null);
});

// ── 2. facts ───────────────────────────────────────────────────────────────
let anaFacts;
await test("facts: cohort, section stats, month window, progression, pending", async () => {
  const { finalized, pendingReviewByAgent } = await D.nightCohort(db, TEAM, PICKS);
  assert.deepEqual(finalized.map((e) => e.id).sort(), [...anaTonight, benTonight, cyTonight].sort());
  assert.deepEqual(pendingReviewByAgent, { 9001: 1 });
  // A TeamConfig shaped like loadTeamConfig's output for the fields the facts builder reads.
  const cfg = {
    rubric_version: "member_support_vT",
    sections_by_number: RUBRIC.sections.map((s) => ({ ...s, auto_value: null })),
  };
  const ana = finalized.filter((e) => e.agent_id === 9001);
  anaFacts = await D.buildAgentFacts(db, cfg, TEAM, PULL, { id: 9001, name: "Ana Perez", email: "ana@x.com" }, ana, 1, NOW);
  assert.equal(anaFacts.tonight.n, 2);
  assert.equal(anaFacts.tonight.avg, 80);
  assert.deepEqual(anaFacts.sections.map((s) => s.id), ["caller_id", "process_adherence", "efficiency"], "manual section excluded");
  assert.equal(anaFacts.tonight.section_stats.process_adherence.avg, 4.5);
  assert.equal(anaFacts.tonight.section_stats.efficiency.avg, 2.5);
  assert.deepEqual(anaFacts.tonight.section_stats.caller_id, { avg: null, yes: 50, n: 2 });
  // Month (America/Los_Angeles Sep 2026): 2 priors in Sep + 2 tonight = 4; August excluded.
  assert.equal(anaFacts.month.label, "2026-09");
  assert.equal(anaFacts.month.n, 4);
  assert.equal(anaFacts.month.avg, 77.5);
  assert.equal(anaFacts.month.section_stats.caller_id.n, 3, "NA excluded from the yn denominator");
  assert.equal(anaFacts.month.section_stats.process_adherence.avg, 4);
  // Progression: 3 priors (oldest first) then tonight's two, flagged.
  assert.deepEqual(anaFacts.recent.map((r) => [r.overall, r.tonight]), [[60, false], [70, false], [80, false], [88, true], [72, true]]);
  assert.equal(anaFacts.pending_review, 1);
  assert.deepEqual(anaFacts.tonight.evals[0].sop_references, ["SOP 1: Parking (Flow)"]);
  assert.equal(anaFacts.tonight.evals[0].scores.process_adherence, 5);
  assert.equal(anaFacts.tonight.evals[1].scores.caller_id, "N");
  assert.match(anaFacts.tonight.evals[0].reasoning.process_adherence, /Parking Flow/);
});

// ── 3. prompt + parser ─────────────────────────────────────────────────────
const GOOD = {
  headline: "Tonight you scored 88 and 72; your month-to-date average is 77.5.",
  sections: [
    { section_id: "caller_id", trend: "down", note: "50% Y tonight vs 67% this month — the second call skipped the verification step." },
    { section_id: "process_adherence", trend: "up", note: "4.5 vs 4.0: per SOP 1 (Parking Flow) you registered the vehicle correctly." },
    { section_id: "efficiency", trend: "flat", note: "2.5 vs 2.7: unannounced holds again." },
  ],
  focus: "Announce every hold before you place it.\nGive a time estimate.\nCheck back within 60 seconds.",
};
await test("prompt lists every section; parser validates", async () => {
  const { system, prompt } = D.buildDigestPrompts(anaFacts);
  assert.match(system, /second person/);
  for (const id of ["caller_id", "process_adherence", "efficiency"]) assert.ok(prompt.includes(`- ${id}:`));
  assert.ok(!prompt.includes("- human_review_required:"));
  assert.match(prompt, /ONLY a JSON object/);
  const parsed = D.parseDigestSummary(anaFacts, "```json\n" + JSON.stringify(GOOD) + "\n```");
  assert.equal(parsed.sections.length, 3);
  assert.equal(parsed.sections[1].trend, "up");
  assert.throws(() => D.parseDigestSummary(anaFacts, "not json"), /not JSON/);
  assert.throws(() => D.parseDigestSummary(anaFacts, JSON.stringify({ ...GOOD, sections: GOOD.sections.slice(1) })), /missing section 'caller_id'/);
  assert.throws(() => D.parseDigestSummary(anaFacts, JSON.stringify({ ...GOOD, sections: [{ ...GOOD.sections[0], trend: "sideways" }, ...GOOD.sections.slice(1)] })), /bad trend/);
});

// ── 4. renderer ────────────────────────────────────────────────────────────
await test("renderer: with summary and facts-only", async () => {
  const m = D.renderDigestEmail(anaFacts, GOOD);
  assert.equal(m.subject, "Nightly QA Digest — Ana Perez — 2026-09-17");
  for (const s of ["Caller ID", "Process Adherence", "Efficiency"]) assert.ok(m.html.includes(s), s);
  assert.ok(!m.html.includes("Human Review Required"));
  assert.ok(m.html.includes(GOOD.headline.replace(/'/g, "&#39;")) || m.html.includes(GOOD.headline));
  assert.ok(m.html.includes("Announce every hold before you place it.<br>Give a time estimate."));
  assert.ok(m.html.includes("Month to date (2026-09)"));
  assert.ok(m.html.includes("77.5"));
  assert.equal((m.html.match(/&#9668;/g) || []).length, 2, "both tonight rows highlighted");
  assert.ok(m.html.includes("/datapoint/member_support/c-t1"));
  assert.ok(m.html.includes("1 more call is with a QA analyst"));
  assert.ok(!m.html.includes("<style"));
  assert.match(m.text, /Process Adherence: tonight 4.50, month 4.00 \[up\]/);
  const f = D.renderDigestEmail(anaFacts, null);
  assert.ok(f.html.includes("Trend summary unavailable tonight"));
  assert.ok(f.html.includes("Hi Ana, here is how tonight"));
  assert.ok(!f.html.includes("Focus for your next shift"));
});

// ── 5. handler phase walk ──────────────────────────────────────────────────
const insightsCalls = [];
const gasCalls = [];
let gasStatus = "ok";
const fetchStub = async (url, init) => {
  gasCalls.push({ url, body: JSON.parse(init.body) });
  return new Response(JSON.stringify({ status: gasStatus, message: `stub ${gasStatus}` }), { status: 200 });
};
let nowMs = NOW;
const handler = D.makeDailyDigestHandler({
  now: () => nowMs,
  triggerInsights: async (_db, _req, payload) => {
    insightsCalls.push(payload);
    return { ok: true, runId: `run-${insightsCalls.length}` };
  },
  fetchImpl: fetchStub,
});
const env = { GAS_WEBAPP_URL_MS: "https://gas.example/exec" };
const row = (over = {}) => ({ id: 1, job: "daily_digest", key: `${TEAM}:${PULL}`, status: "running", phase: null, cursor: 0, attempts: 0, report: null, ...over });
const digests = () => raw.prepare("SELECT id, agent_id, agent_email, email_status, email_message, summary, summary_error, eval_ids FROM qa_agent_digests ORDER BY id").all();

await test("await_scores waits while a pick is still scoring (before deadline)", async () => {
  const r = await handler(db, req, env, row(), {});
  assert.equal(r.done, false);
  assert.equal(r.phase, "await_scores");
  assert.equal(r.wait_s, 300);
  assert.equal(r.report.pending_scores, 1);
});
let report = {};
await test("await_scores → summarize once the queue drains", async () => {
  raw.prepare("UPDATE qa_score_queue SET status='done' WHERE call_id='c-missing'").run();
  const r = await handler(db, req, env, row(), {});
  assert.equal(r.phase, "summarize");
  assert.equal(r.cursor, 0);
  report = r.report;
});
await test("summarize: rows for active agents only, one insights run", async () => {
  const r = await handler(db, req, env, row({ phase: "summarize", cursor: 0 }), report);
  assert.equal(r.phase, "summarize");
  assert.equal(r.cursor, 1);
  report = r.report;
  assert.equal(report.rows_built, 2, "Ana + Ben; Cy inactive");
  assert.equal(report.finalized_evals, 4);
  assert.equal(report.agents_inactive_or_unmatched, 1);
  const d = digests();
  assert.deepEqual(d.map((x) => x.agent_id), [9001, 9002]);
  assert.deepEqual(JSON.parse(d[0].eval_ids), anaTonight);
  assert.equal(insightsCalls.length, 1);
  const p = insightsCalls[0];
  assert.equal(p.mode, "daily_digest");
  assert.deepEqual(p.model, { model: "claude-sonnet-5", max_tokens: 6000 });
  assert.equal(p.items.length, 2);
  assert.deepEqual(p.items[0].ref, { kind: "daily_digest", digest_id: d[0].id, team_id: TEAM, pull_date: PULL, agent_id: 9001 });
  assert.match(p.items[0].prompt, /"pull_date": "2026-09-17"/);
  assert.deepEqual(report.summary_runs, [{ chunk: 0, items: 2, run_id: "run-1" }]);
});
await test("summarize cursor past the chunks → await_summary", async () => {
  const r = await handler(db, req, env, row({ phase: "summarize", cursor: 1 }), report);
  assert.equal(r.phase, "await_summary");
  assert.ok(r.report.summary_started_at);
  report = r.report;
});
await test("await_summary waits, then callback persist (ok + failed item)", async () => {
  let r = await handler(db, req, env, row({ phase: "await_summary" }), report);
  assert.equal(r.phase, "await_summary");
  assert.equal(r.wait_s, 60);
  assert.equal(r.report.summaries_waiting, 2);
  const d = digests();
  await D.persistDigestSummary(db, { kind: "daily_digest", digest_id: d[0].id }, { ok: true, text: JSON.stringify(GOOD), model: "claude-sonnet-5", usage: { input_tokens: 5000, output_tokens: 300 } });
  await D.persistDigestSummary(db, { kind: "daily_digest", digest_id: d[1].id }, { ok: false, error: "gateway 529" });
  const d2 = digests();
  assert.equal(JSON.parse(d2[0].summary).headline, GOOD.headline);
  assert.equal(d2[0].summary_error, null);
  assert.equal(d2[1].summary, null);
  assert.equal(d2[1].summary_error, "gateway 529");
  r = await handler(db, req, env, row({ phase: "await_summary" }), report);
  assert.equal(r.phase, "send");
  report = r.report;
});
await test("send: one agent per step through GAS html_email, then done", async () => {
  let r = await handler(db, req, env, row({ phase: "send", cursor: 0 }), report);
  assert.equal(r.phase, "send");
  assert.equal(r.done, false);
  report = r.report;
  assert.equal(gasCalls.length, 1);
  const m = gasCalls[0].body.html_email;
  assert.equal(gasCalls[0].url, env.GAS_WEBAPP_URL_MS);
  assert.equal(m.to, "ana@x.com");
  assert.equal(m.cc, undefined, "cc_supervisor false by default");
  assert.ok(m.html.includes("Focus for your next shift"));
  r = await handler(db, req, env, row({ phase: "send", cursor: 1 }), report);
  report = r.report;
  assert.equal(gasCalls[1].body.html_email.to, "ben@x.com");
  assert.ok(gasCalls[1].body.html_email.html.includes("Trend summary unavailable tonight"), "facts-only for the failed summary");
  r = await handler(db, req, env, row({ phase: "send", cursor: 2 }), report);
  assert.equal(r.done, true);
  assert.equal(r.phase, "done");
  assert.deepEqual(r.report.sent, { ok: 2 });
  assert.deepEqual(digests().map((x) => x.email_status), ["ok", "ok"]);
});
await test("re-run is idempotent: nothing pending → done without re-sending", async () => {
  const before = gasCalls.length;
  const r = await handler(db, req, env, row({ phase: "send", cursor: 0 }), {});
  assert.equal(r.done, true);
  assert.equal(gasCalls.length, before);
});
await test("deadline: pending scores past 13:30 UTC proceed with deadline_hit", async () => {
  raw.prepare("INSERT INTO qa_disposition_pulls (team_id, pull_date, status, phase, report) VALUES (?,?,'completed','done',?)").run(TEAM, "2026-09-16", JSON.stringify({ picked_call_ids: ["c-late"] }));
  raw.prepare("INSERT INTO qa_score_queue (job_id, team_id, call_id, payload, status, enqueued_at) VALUES ('score-c-late',?,'c-late','{}','running','2026-09-17T06:10:00Z')").run(TEAM);
  nowMs = Date.parse("2026-09-17T13:31:00Z");
  const r = await handler(db, req, env, row({ key: `${TEAM}:2026-09-16` }), {});
  assert.equal(r.phase, "summarize");
  assert.equal(r.report.deadline_hit, true);
  const r2 = await handler(db, req, env, row({ key: `${TEAM}:2026-09-16`, phase: "summarize" }), r.report);
  assert.equal(r2.done, true, "no finalized evals → done");
  assert.match(r2.report.note, /no finalized evals/);
  nowMs = NOW;
});
await test("missing GAS url → skipped receipts, job still completes", async () => {
  raw.prepare("UPDATE qa_agent_digests SET email_status='pending' WHERE agent_id=9002").run();
  const r = await handler(db, req, {}, row({ phase: "send" }), {});
  assert.equal(r.done, false);
  assert.deepEqual(r.report.sent, { skipped: 1 });
  assert.match(digests()[1].email_message, /GAS_WEBAPP_URL_MS/);
});
await test("no picks recorded → done with a note", async () => {
  raw.prepare("INSERT INTO qa_disposition_pulls (team_id, pull_date, status, phase, report) VALUES (?,?,'completed','done','{}')").run(TEAM, "2026-09-15");
  const r = await handler(db, req, env, row({ key: `${TEAM}:2026-09-15` }), {});
  assert.equal(r.done, true);
  assert.match(r.report.note, /no picks/);
});

// ── 6. runner: wait_s parks the row ────────────────────────────────────────
await test("runner: wait_s → next_at in the future, more:false, resumes when due", async () => {
  await C.enqueueCronJob(db, "daily_digest", `${TEAM}:runner`);
  let calls = 0;
  const handlers = {
    daily_digest: async () => {
      calls++;
      return calls === 1 ? { done: false, phase: "await_scores", wait_s: 300, report: { waited: true } } : { done: true, report: { ok: true } };
    },
  };
  let r = await C.runCronJobsStep(db, req, {}, handlers);
  assert.equal(r.status, "running");
  assert.ok(r.next_at, "next_at returned");
  assert.equal(r.more, false, "a parked job is not 'more'");
  const stored = raw.prepare("SELECT next_at, phase, status FROM qa_cron_jobs WHERE key = ?").get(`${TEAM}:runner`);
  assert.ok(Date.parse(stored.next_at) > Date.now() + 200_000);
  r = await C.runCronJobsStep(db, req, {}, handlers);
  assert.equal(r.more, false);
  assert.equal(calls, 1, "not picked while parked");
  raw.prepare("UPDATE qa_cron_jobs SET next_at = '2000-01-01T00:00:00Z' WHERE key = ?").run(`${TEAM}:runner`);
  r = await C.runCronJobsStep(db, req, {}, handlers);
  assert.equal(r.status, "completed");
  assert.equal(calls, 2);
  assert.equal(raw.prepare("SELECT next_at FROM qa_cron_jobs WHERE key = ?").get(`${TEAM}:runner`).next_at, null);
});

for (const f of failures) console.log("FAIL", f);
console.log(`${pass} passed, ${failures.length} failed`);
process.exit(failures.length ? 1 : 0);
