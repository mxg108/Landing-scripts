#!/usr/bin/env node
// Trigger-time SOP resolution tests (v0.64, resolveDeferredSop).
//
// Core guarantee: a deferred prompt resolved at trigger time is
// BYTE-IDENTICAL to what the legacy enqueue-time path would have built for
// the same retrieval — proven by building both from the worker's own
// modules against one stubbed Pulpo endpoint. Also covers: conservative
// missing-note on empty/failed retrieval, persist provenance stamps,
// marker removal, and pass-through of pre-v0.64 payloads.
//
//   node tests/sop_deferred.test.mjs        (from sandy-qa/)

import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";

execFileSync(
  "node_modules/wrangler/node_modules/esbuild/bin/esbuild",
  [
    "tests/entry_deferred.ts",
    "--bundle",
    "--format=esm",
    "--outfile=tests/.build/entry_deferred.mjs",
    "--platform=neutral",
  ],
  { stdio: "inherit" }
);
const {
  buildJudgePromptTemplate,
  buildScoringPrompt,
  sopBlockParts,
  SOP_BLOCK_PLACEHOLDER,
  fetchSopContext,
  resolveDeferredSop,
} = await import(new URL("./.build/entry_deferred.mjs", import.meta.url));

// ── minimal rubric config (PromptConfig shape) ──────────────────────────────

const CFG = {
  company: "Landing Living LLC",
  scoring_prompt: {
    system_prompt_template: "You are a QA evaluator at {company}.",
    confidence_levels_note: "- high / medium / low",
    sop_sections: [5, 6],
    long_call_focus_sections: [],
  },
  sections: [
    {
      id: "process_adherence", name: "Process Adherence", section_number: 5,
      score_type: "numeric", score_range: [1, 5], audio_dependent: false,
      na_applicable: false, rubric_question: "Followed process?",
    },
    {
      id: "call_resolution", name: "Call Resolution", section_number: 6,
      score_type: "numeric", score_range: [1, 5], audio_dependent: false,
      na_applicable: false, rubric_question: "Resolved?",
    },
  ],
};

// ── stubbed Pulpo endpoint (one doc corpus) ─────────────────────────────────

const DOC = {
  id: "doc-1",
  title: "NMT Extensions",
  body: "Extend the reservation per NMT policy.",
  tags: ["area:nmt"],
  score: 0.71,
};
const rpcOk = (id, result) =>
  new Response(JSON.stringify({ jsonrpc: "2.0", id, result }), {
    status: 200,
    headers: { "content-type": "application/json", "mcp-session-id": "s1" },
  });
globalThis.fetch = async (_url, init) => {
  const body = JSON.parse(init.body);
  if (body.method === "initialize")
    return rpcOk(body.id, { protocolVersion: "2025-03-26", serverInfo: {} });
  if (body.method === "notifications/initialized")
    return new Response("", { status: 202, headers: { "content-type": "application/json" } });
  const { name, arguments: args } = body.params;
  if (name === "search_knowledge_base")
    return rpcOk(body.id, {
      content: [{ type: "text", text: JSON.stringify({ batches: [{ query: args.queries[0], results: [
        { id: DOC.id, title: DOC.title, score: DOC.score, score_type: "cosine",
          last_verified: "2026-08-01", tags: DOC.tags },
      ] }] }) }],
    });
  if (name === "get_document")
    return rpcOk(body.id, {
      content: [{ type: "text", text: JSON.stringify({ document: {
        id: DOC.id, title: DOC.title, body: DOC.body, open_flags: [],
        last_verified: "2026-08-01",
      } }) }],
    });
  throw new Error(`unexpected tool ${name}`);
};

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

const EXTRAS = {
  agentName: "Ana",
  teamContext: "TEAM CONTEXT: member support line.",
  callContextText: "=== CALL CONTEXT ===\ndisposition: X",
};
const CREDS = { pulpoUrl: "https://stub.example/mcp", pulpoToken: "t" };
const deferredInputs = () => ({
  disposition_category: "Reservation & Stay Changes",
  disposition: "Extension / renewal",
  transcript_head: "",
  summary_query: null,
  scope: { exclude_tags: ["system:sofia"] },
  block_parts: sopBlockParts(CFG),
});
const mkPayload = () => ({
  judge: { prompt_template: buildJudgePromptTemplate(CFG, { ...EXTRAS, sopDeferred: true }) },
  single_stage: { prompt: buildScoringPrompt(CFG, "transcript text", { ...EXTRAS, sopDeferred: true }) },
  persist: { sop_used: null, pulpo_docs: [], sop_skipped_reason: "deferred_to_trigger" },
  sop_deferred: deferredInputs(),
});

await test("deferred templates carry the placeholder", () => {
  const p = mkPayload();
  assert.ok(p.judge.prompt_template.includes(SOP_BLOCK_PLACEHOLDER));
  assert.ok(p.single_stage.prompt.includes(SOP_BLOCK_PLACEHOLDER));
});

await test("resolved judge prompt is BYTE-IDENTICAL to the legacy enqueue path", async () => {
  // legacy: what enqueue-time retrieval would have produced
  const sop = await fetchSopContext({
    ...CREDS,
    dispositionCategory: "Reservation & Stay Changes",
    disposition: "Extension / renewal",
    transcriptText: "",
    scope: { exclude_tags: ["system:sofia"] },
  });
  assert.equal(sop.skipped_reason, "", `retrieval failed: ${sop.skipped_reason}`);
  const legacyJudge = buildJudgePromptTemplate(CFG, {
    ...EXTRAS, sopTitle: sop.sop_title, sopContent: sop.block_text,
  });
  const legacySingle = buildScoringPrompt(CFG, "transcript text", {
    ...EXTRAS, sopTitle: sop.sop_title, sopContent: sop.block_text,
  });
  const resolved = await resolveDeferredSop(mkPayload(), CREDS);
  assert.equal(resolved.judge.prompt_template, legacyJudge);
  assert.equal(resolved.single_stage.prompt, legacySingle);
});

await test("resolution stamps persist provenance and drops the marker", async () => {
  const resolved = await resolveDeferredSop(mkPayload(), CREDS);
  assert.equal(resolved.persist.sop_used, DOC.title);
  assert.equal(resolved.persist.pulpo_docs.length, 1);
  assert.equal(resolved.persist.pulpo_docs[0].id, DOC.id);
  assert.deepEqual(resolved.persist.pulpo_docs[0].tags, DOC.tags);
  assert.equal(resolved.persist.sop_skipped_reason, null);
  assert.ok(!("sop_deferred" in resolved));
  assert.ok(!resolved.judge.prompt_template.includes(SOP_BLOCK_PLACEHOLDER));
});

await test("empty retrieval resolves to the conservative missing note (byte-parity)", async () => {
  const p = mkPayload();
  p.sop_deferred.scope = { tags: ["no-such-tag"], match: "any" }; // filters all hits
  const resolved = await resolveDeferredSop(p, CREDS);
  const legacy = buildJudgePromptTemplate(CFG, EXTRAS); // no sopContent → missing note
  assert.equal(resolved.judge.prompt_template, legacy);
  assert.equal(resolved.persist.sop_skipped_reason, "no_hits_in_team_scope");
  assert.equal(resolved.persist.pulpo_docs.length, 0);
});

await test("missing creds resolve conservatively, never throw", async () => {
  const resolved = await resolveDeferredSop(mkPayload(), {});
  assert.equal(resolved.persist.sop_skipped_reason, "no_provider");
  assert.ok(!resolved.judge.prompt_template.includes(SOP_BLOCK_PLACEHOLDER));
  assert.ok(resolved.judge.prompt_template.includes("No SOP context loaded"));
});

await test("pre-v0.64 payload (no marker) passes through untouched", async () => {
  const legacy = {
    judge: { prompt_template: "already rendered" },
    persist: { sop_used: "X", pulpo_docs: [{ id: "old" }], sop_skipped_reason: null },
  };
  const before = JSON.stringify(legacy);
  const out = await resolveDeferredSop(legacy, CREDS);
  assert.equal(JSON.stringify(out), before);
});

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log("\nall tests passed");
