#!/usr/bin/env node
// Approve-path persistence tests (v0.66) — guards the owner doctrine of
// 2026-09-01: na_default governs the CREATION default only; at approval an
// analyst's explicit values on ANY section (manual ones included — scores,
// reasoning, confidence) persist and drive the formula. The v0.62 NA-LOCK
// regression (na_default manual sections coerced back to NA on every
// approval, analyst reasoning dropped) is what this suite would have
// caught.
//
// Runs the REAL approveEvaluation route against node:sqlite through a D1
// shim, on the real migration chain 0001→current (coaching_e2e house
// pattern, now committed).
//
//   node tests/approve_persist.test.mjs        (from sandy-qa/)

import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";

execFileSync(
  "node_modules/wrangler/node_modules/esbuild/bin/esbuild",
  [
    "tests/entry_approve.ts",
    "--bundle",
    "--format=esm",
    "--outfile=tests/.build/entry_approve.mjs",
    "--platform=neutral",
    "--define:process.env.NODE_ENV='\"test\"'",
  ],
  { stdio: "inherit" }
);
const { approveEvaluation, evaluateFormula, quantizeScore } = await import(
  new URL("./.build/entry_approve.mjs", import.meta.url)
);

// ── node:sqlite behind a D1 shim ────────────────────────────────────────────

const raw = new DatabaseSync(":memory:");
for (const f of readdirSync("migrations").sort()) {
  raw.exec(readFileSync(`migrations/${f}`, "utf8"));
}

const db = {
  prepare(sql) {
    const mk = (params) => ({
      first: async () => raw.prepare(sql).get(...params) ?? null,
      all: async () => ({ results: raw.prepare(sql).all(...params) }),
      run: async () => {
        const r = raw.prepare(sql).run(...params);
        return { meta: { changes: Number(r.changes) } };
      },
    });
    return { bind: (...params) => mk(params), ...mk([]) };
  },
};

// ── seeds: MS rubric + formula + agent + draft eval with sections ───────────

const RUBRIC = {
  rubric_version: "member_support_vT",
  scoring_prompt: {
    system_prompt_template: "You are a QA evaluator at {company}.",
    confidence_levels_note: "- high / medium / low",
    sop_sections: [5],
    long_form: false,
    long_call_focus_sections: [],
  },
  sections: [
    {
      id: "process_adherence", history_id: "process_adherence",
      name: "Process Adherence", section_number: 5, score_type: "numeric",
      score_range: [1, 5], audio_dependent: false, na_applicable: false,
      rubric_question: "Followed process?",
    },
    {
      id: "human_review_required", history_id: "human_review_required",
      name: "Human Review Required", section_number: 9, score_type: "manual",
      score_range: [1, 5], audio_dependent: false, na_applicable: true,
      rubric_question: "",
    },
  ],
};
const FORMULA = {
  formula_id: "ms_vT",
  scale: { min: 0.0, max: 100.0 },
  evaluation_order: ["hrr_na_spread", "weighted_sum"],
  normalization: {
    na: "redistribute_per_rules",
    binary_yn: { N: 0.0, Y: 1.0 },
    rating_1_5: { type: "linear", output: [0.0, 1.0], input_max: 5, input_min: 1 },
  },
  rules: [
    {
      id: "hrr_na_spread", type: "weight_redistribution", enabled: true,
      when: { section: "human_review_required", equals: "NA", frac_gte: null, frac_lte: null },
      effect: { from: "human_review_required", to: "active_sections", method: "equal_additive" },
      note: null,
    },
  ],
  sections: [
    { key: "process_adherence", label: "Process Adherence", weight: 60.0,
      score_type: "rating_1_5", na_default: false, binary_map: null,
      trigger: null, category: null },
    { key: "human_review_required", label: "Human Review Required", weight: 40.0,
      score_type: "rating_1_5_na", na_default: true, binary_map: null,
      trigger: null, category: null },
  ],
  human_review_triggers: [],
};

raw.prepare(
  "INSERT INTO qa_rubric_versions (team_id, rubric_version, rubric_json, effective_from) VALUES (?,?,?,?)"
).run("member_support", "member_support_vT", JSON.stringify(RUBRIC), "2026-08-01");
raw.prepare(
  "INSERT INTO qa_formula_versions (formula_version, team_id, formula_json, effective_from) VALUES (?,?,?,?)"
).run("ms_vT", "member_support", JSON.stringify(FORMULA), "2026-08-01");
raw.prepare(
  "INSERT INTO qa_agents (id, team_id, name, email, active) VALUES (10000050,'member_support','Test Agent','test.agent@hellolanding.com',1)"
).run();

let nextEvalId = 10000101;
function seedDraftEval(callId) {
  const id = nextEvalId++;
  raw.prepare(
    `INSERT INTO qa_evaluations (id, team_id, agent_id, agent_name_raw, agent_email,
       state, source, dialpad_call_id, models_used, formula_version, rubric_version, scoring_status)
     VALUES (?, 'member_support', 10000050, 'Test Agent', 'test.agent@hellolanding.com',
       'draft', 'ai', ?, '{}', 'ms_vT', 'member_support_vT', 'complete')`
  ).run(id, callId);
  raw.prepare(
    `INSERT INTO qa_evaluation_sections (id, evaluation_id, section_id, section_number,
       score_type, numeric_score, binary_value, score_source, ai_provider, confidence, reasoning)
     VALUES (?, ?, 'process_adherence', 5, 'numeric', 4, NULL, 'ai', 'gemini', 'high', 'ai reasoning')`
  ).run(id * 100 + 5, id);
  raw.prepare(
    `INSERT INTO qa_evaluation_sections (id, evaluation_id, section_id, section_number,
       score_type, numeric_score, binary_value, score_source, ai_provider, confidence, reasoning)
     VALUES (?, ?, 'human_review_required', 9, 'manual_numeric', NULL, 'NA', 'manual_default', NULL, NULL, NULL)`
  ).run(id * 100 + 9, id);
  return id;
}

function approveRequest(callId, sections) {
  return new Request(`http://test/api/member_support/score/${callId}/approve`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ evaluator_email: "analyst@hellolanding.com", sections }),
  });
}

const sectionRow = (evalId, sectionId) =>
  raw.prepare(
    "SELECT * FROM qa_evaluation_sections WHERE evaluation_id=? AND section_id=?"
  ).get(evalId, sectionId);

const failures = [];
async function test(name, fn) {
  try {
    await fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failures.push(name);
    console.log(`FAIL ${name}\n     ${e.message}`);
  }
}

// ── the regression case: analyst fills the na_default manual section ───────

await test("analyst value on an na_default manual section persists and drives the formula", async () => {
  const id = seedDraftEval("call-A");
  const resp = await approveEvaluation(
    approveRequest("call-A", [
      { id: "process_adherence", score: 4, score_type: "numeric", yn_value: null,
        confidence: "high", reasoning: "kept as scored" },
      { id: "human_review_required", score: 2, score_type: "numeric", yn_value: null,
        confidence: "medium", reasoning: "borderline verification — send to supervisor" },
    ]),
    db, "member_support", "call-A", { editOfFinalized: false }
  );
  const out = await resp.json();
  assert.equal(resp.status, 200, JSON.stringify(out).slice(0, 200));

  const hrr = sectionRow(id, "human_review_required");
  assert.equal(hrr.numeric_score, 2, "analyst score must persist");
  assert.equal(hrr.binary_value, null);
  assert.equal(hrr.score_source, "manual");
  assert.equal(hrr.reasoning, "borderline verification — send to supervisor");
  assert.equal(hrr.confidence, "medium");

  const expected = quantizeScore(
    evaluateFormula(FORMULA, { process_adherence: 4, human_review_required: 2 }).final_score
  );
  const ev = raw.prepare("SELECT overall_score, state FROM qa_evaluations WHERE id=?").get(id);
  assert.equal(ev.overall_score, expected, "overall must be computed FROM the analyst value");
  assert.equal(ev.state, "finalized");
});

// ── the default case: untouched section stays NA with the spread rule ──────

await test("untouched na_default section stays NA (manual_default) and NA-spread applies", async () => {
  const id = seedDraftEval("call-B");
  const resp = await approveEvaluation(
    approveRequest("call-B", [
      { id: "process_adherence", score: 4, score_type: "numeric", yn_value: null,
        confidence: "high", reasoning: "kept as scored" },
      { id: "human_review_required", score: null, score_type: "numeric", yn_value: "NA",
        confidence: null, reasoning: null },
    ]),
    db, "member_support", "call-B", { editOfFinalized: false }
  );
  assert.equal(resp.status, 200);

  const hrr = sectionRow(id, "human_review_required");
  assert.equal(hrr.numeric_score, null);
  assert.equal(hrr.binary_value, "NA");
  assert.equal(hrr.score_source, "manual_default", "unchanged NA keeps its creation source");

  const expected = quantizeScore(
    evaluateFormula(FORMULA, { process_adherence: 4, human_review_required: "NA" }).final_score
  );
  const ev = raw.prepare("SELECT overall_score FROM qa_evaluations WHERE id=?").get(id);
  assert.equal(ev.overall_score, expected, "NA path must use the weight-redistribution rule");
});

// ── AI-section edits persist too (reasoning + confidence + score) ──────────

await test("edited AI section persists score/reasoning/confidence with source=manual", async () => {
  const id = seedDraftEval("call-C");
  const resp = await approveEvaluation(
    approveRequest("call-C", [
      { id: "process_adherence", score: 5, score_type: "numeric", yn_value: null,
        confidence: "high", reasoning: "analyst raised after SOP check" },
      { id: "human_review_required", score: null, score_type: "numeric", yn_value: "NA",
        confidence: null, reasoning: null },
    ]),
    db, "member_support", "call-C", { editOfFinalized: false }
  );
  assert.equal(resp.status, 200);
  const pa = sectionRow(id, "process_adherence");
  assert.equal(pa.numeric_score, 5);
  assert.equal(pa.score_source, "manual");
  assert.equal(pa.reasoning, "analyst raised after SOP check");
  assert.equal(pa.confidence, "high");
});

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log("\nall tests passed");
