#!/usr/bin/env node
// estimateEvalCost tests (v0.72) — per-eval cost stamps from pipeline usage.
//   node tests/model_costs.test.mjs        (from sandy-qa/)

import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";

execFileSync(
  "node_modules/wrangler/node_modules/esbuild/bin/esbuild",
  ["src/lib/modelCosts.ts", "--bundle", "--format=esm",
   "--outfile=tests/.build/modelCosts.mjs", "--platform=neutral"],
  { stdio: "inherit" }
);
const { estimateEvalCost, MODEL_PRICES_PER_MTOK } = await import(
  new URL("./.build/modelCosts.mjs", import.meta.url)
);

const failures = [];
function test(name, fn) {
  try { fn(); console.log(`ok   ${name}`); }
  catch (e) { failures.push(name); console.log(`FAIL ${name}\n     ${e.message}`); }
}

test("two-stage eval: gemini annotate (audio split) + sonnet-5 judge", () => {
  const cost = estimateEvalCost({
    annotator_model: "gemini-2.5-flash",
    annotate_diag: { prompt_tokens: 25000, prompt_tokens_audio: 20000,
                     output_tokens: 6000, thoughts_tokens: 4000 },
    scorer_provider: "anthropic",
    scorer_model: "claude-sonnet-5",
    judge_usage: { input_tokens: 12000, output_tokens: 3000 },
  });
  const g = MODEL_PRICES_PER_MTOK["gemini-2.5-flash"];
  const a = MODEL_PRICES_PER_MTOK["claude-sonnet-5"];
  const expected =
    (20000 * g.in_audio + 5000 * g.in + 10000 * g.out) / 1e6 +
    (12000 * a.in + 3000 * a.out) / 1e6;
  assert.equal(cost, Math.round(expected * 1e5) / 1e5);
});

test("pre-v0.4 result (no audio split, no judge usage): annotate-only lower bound", () => {
  const cost = estimateEvalCost({
    annotator_model: "gemini-2.5-flash",
    annotate_diag: { prompt_tokens: 10000, output_tokens: 2000, thoughts_tokens: 1000 },
    scorer_provider: "anthropic",
    scorer_model: "claude-sonnet-5",
    judge_usage: null,
  });
  const g = MODEL_PRICES_PER_MTOK["gemini-2.5-flash"];
  assert.equal(cost, Math.round(((10000 * g.in + 3000 * g.out) / 1e6) * 1e5) / 1e5);
});

test("Plan B single-stage counts once (no judge double-count)", () => {
  const g = MODEL_PRICES_PER_MTOK["gemini-2.5-flash"];
  const cost = estimateEvalCost({
    annotator_model: "gemini-2.5-flash",
    annotate_diag: { prompt_tokens: 1000, output_tokens: 0, thoughts_tokens: 0 },
    scorer_provider: "gemini",
    scorer_model: "gemini-2.5-flash",
    judge_diag: null,
    single_diag: { prompt_tokens: 8000, prompt_tokens_audio: 6000, output_tokens: 3000 },
  });
  const expected =
    (1000 * g.in) / 1e6 + (6000 * g.in_audio + 2000 * g.in + 3000 * g.out) / 1e6;
  assert.equal(cost, Math.round(expected * 1e5) / 1e5);
});

test("no usage anywhere → null (never 0)", () => {
  assert.equal(estimateEvalCost({ scorer_provider: "anthropic" }), null);
  assert.equal(estimateEvalCost({}), null);
});

test("unknown model prices contribute nothing", () => {
  const cost = estimateEvalCost({
    annotator_model: "gemini-9-ultra",
    annotate_diag: { prompt_tokens: 1000, output_tokens: 100 },
    scorer_provider: "anthropic",
    scorer_model: "claude-sonnet-5",
    judge_usage: { input_tokens: 1000, output_tokens: 100 },
  });
  const a = MODEL_PRICES_PER_MTOK["claude-sonnet-5"];
  assert.equal(cost, Math.round(((1000 * a.in + 100 * a.out) / 1e6) * 1e5) / 1e5);
});

if (failures.length) { console.log(`\n${failures.length} FAILED`); process.exit(1); }
console.log("\nall tests passed");
