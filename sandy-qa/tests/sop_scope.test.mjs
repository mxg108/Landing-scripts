#!/usr/bin/env node
// Tag-scope retrieval tests (migration 0013 / sopRetrieval.applyTagScope).
//
// Bundles the worker's own sopRetrieval.ts (parity-harness pattern) and runs
// it against a stubbed Pulpo MCP endpoint via a fetch shim — no network, no
// D1. Covers: exclude_tags (MS/Sales minus system:sofia), allow tags
// (sofia + system:sofia), fail-closed on unknown tag state, the
// scope-agnostic search cache, flowchart reading note, and provenance
// tag/body_format stamps.
//
//   node tests/sop_scope.test.mjs        (from sandy-qa/)
//
// Exit 0 = all pass; exit 1 = failures (printed).

import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";

execFileSync(
  "node_modules/wrangler/node_modules/esbuild/bin/esbuild",
  [
    "src/lib/sopRetrieval.ts",
    "--bundle",
    "--format=esm",
    "--outfile=tests/.build/sopRetrieval.mjs",
    "--platform=neutral",
  ],
  { stdio: "inherit" }
);
const { applyTagScope, fetchSopContext, renderSopBlock } = await import(
  new URL("./.build/sopRetrieval.mjs", import.meta.url)
);

// ── stub Pulpo MCP endpoint ─────────────────────────────────────────────────

const CORPUS = {
  "doc-ms": {
    id: "doc-ms",
    title: "NMT Extensions",
    body: "Extend the reservation per NMT policy.",
    tags: ["area:nmt", "reservation-edits"],
    score: 0.71,
  },
  "doc-sofia-sop": {
    id: "doc-sofia-sop",
    title: "Sofia SOP — Maintenance",
    body: "Sofia routes maintenance via create_maintenance_request.",
    tags: ["system:sofia", "engineering", "type:sop"],
    score: 0.74,
  },
  "doc-sofia-flow": {
    id: "doc-sofia-flow",
    title: "Sofia Subagent — Maintenance (Flow)",
    body: "flowchart TD\n  A-->B\n%% def A: triage the ticket",
    tags: ["system:sofia", "type:flow"],
    body_format: "flowchart",
    score: 0.68,
  },
  "doc-shared-sofia": {
    id: "doc-shared-sofia",
    title: "General Check-In Procedures",
    body: "Members check in with the app.",
    tags: ["sofia", "area:access"],
    score: 0.66,
  },
  "doc-untagged": {
    id: "doc-untagged",
    title: "Legacy Doc With No Tag Field",
    body: "Old doc.",
    tags: null, // simulates an API regression: tags field absent
    score: 0.64,
  },
};

let searchCalls = 0;
const rpcOk = (id, result) =>
  new Response(JSON.stringify({ jsonrpc: "2.0", id, result }), {
    status: 200,
    headers: { "content-type": "application/json", "mcp-session-id": "sess-1" },
  });

globalThis.fetch = async (_url, init) => {
  const body = JSON.parse(init.body);
  if (body.method === "initialize")
    return rpcOk(body.id, { protocolVersion: "2025-03-26", serverInfo: {} });
  if (body.method === "notifications/initialized")
    return new Response("", { status: 202, headers: { "content-type": "application/json" } });
  const { name, arguments: args } = body.params;
  if (name === "search_knowledge_base") {
    searchCalls += 1;
    const results = Object.values(CORPUS).map((d) => ({
      id: d.id,
      title: d.title,
      score: d.score,
      score_type: "cosine",
      last_verified: "2026-08-01",
      ...(d.tags !== null ? { tags: d.tags } : {}),
      ...(d.body_format ? { body_format: d.body_format } : {}),
    }));
    return rpcOk(body.id, {
      content: [
        {
          type: "text",
          text: JSON.stringify({ batches: [{ query: args.queries[0], results }] }),
        },
      ],
    });
  }
  if (name === "get_document") {
    const d = CORPUS[args.id];
    return rpcOk(body.id, {
      content: [
        {
          type: "text",
          text: JSON.stringify({
            document: {
              id: d.id,
              title: d.title,
              body: d.body,
              open_flags: [],
              last_verified: "2026-08-01",
              ...(d.body_format ? { body_format: d.body_format } : {}),
            },
          }),
        },
      ],
    });
  }
  throw new Error(`unexpected tool ${name}`);
};

// ── tests ───────────────────────────────────────────────────────────────────

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

const HITS = [
  { id: "a", tags: ["area:nmt"], title: "clean" },
  { id: "b", tags: ["system:sofia", "type:sop"], title: "sofia estate" },
  { id: "c", tags: ["sofia"], title: "shared sofia" },
  { id: "d", tags: [], title: "untagged" },
  { id: "e", title: "no tags field" }, // tags undefined
];

await test("applyTagScope: null scope passes everything through", () => {
  assert.equal(applyTagScope(HITS, null).length, HITS.length);
  assert.equal(applyTagScope(HITS, {}).length, HITS.length);
});

await test("applyTagScope: exclude_tags drops system:sofia, keeps bare sofia + untagged", () => {
  const out = applyTagScope(HITS, { exclude_tags: ["system:sofia"] });
  assert.deepEqual(out.map((h) => h.id), ["a", "c", "d"]); // e fail-closed
});

await test("applyTagScope: exclusion is case-insensitive", () => {
  const out = applyTagScope(HITS, { exclude_tags: ["SYSTEM:SOFIA"] });
  assert.deepEqual(out.map((h) => h.id), ["a", "c", "d"]);
});

await test("applyTagScope: allow match=any (sofia scope)", () => {
  const out = applyTagScope(HITS, { tags: ["sofia", "system:sofia"], match: "any" });
  assert.deepEqual(out.map((h) => h.id), ["b", "c"]);
});

await test("applyTagScope: allow match=all", () => {
  const out = applyTagScope(HITS, { tags: ["system:sofia", "type:sop"], match: "all" });
  assert.deepEqual(out.map((h) => h.id), ["b"]);
});

await test("applyTagScope: hit with no tags array is dropped under any active scope", () => {
  assert.equal(applyTagScope([HITS[4]], { exclude_tags: ["x"] }).length, 0);
  assert.equal(applyTagScope([HITS[4]], { tags: ["x"] }).length, 0);
});

const BASE = {
  pulpoUrl: "https://stub.example/mcp",
  pulpoToken: "stub-token",
  transcriptText: "",
};

await test("fetchSopContext: exclude scope keeps Sofia's estate out of the block", async () => {
  const ctx = await fetchSopContext({
    ...BASE,
    dispositionCategory: "Unit Issues",
    disposition: "Maintenance request",
    scope: { exclude_tags: ["system:sofia"] },
  });
  assert.equal(ctx.skipped_reason, "");
  const titles = ctx.provenance.map((p) => p.title);
  assert.ok(titles.includes("NMT Extensions"), `got ${titles}`);
  assert.ok(!titles.some((t) => t.startsWith("Sofia")), `leaked: ${titles}`);
  assert.ok(!ctx.block_text.includes("Sofia SOP"), "Sofia SOP body in block");
  // provenance carries the scope-audit stamps
  for (const p of ctx.provenance) {
    assert.ok(Array.isArray(p.tags), "provenance missing tags");
    assert.ok("body_format" in p, "provenance missing body_format");
  }
});

await test("fetchSopContext: search cache is scope-agnostic (1 search, 2 scopes)", async () => {
  const before = searchCalls;
  const unscoped = await fetchSopContext({
    ...BASE,
    dispositionCategory: "Unit Issues",
    disposition: "Maintenance request",
    scope: null,
  });
  assert.equal(searchCalls, before, "cache miss on repeated query");
  const titles = unscoped.provenance.map((p) => p.title);
  assert.ok(titles.some((t) => t.startsWith("Sofia")), "unscoped should see Sofia docs");
});

await test("fetchSopContext: allow scope (sofia) retrieves only her families", async () => {
  const ctx = await fetchSopContext({
    ...BASE,
    dispositionCategory: "Sofia call",
    disposition: "maintenance",
    scope: { tags: ["sofia", "system:sofia"], match: "any" },
  });
  assert.equal(ctx.skipped_reason, "");
  for (const p of ctx.provenance) {
    assert.ok(
      p.tags.some((t) => ["sofia", "system:sofia"].includes(t)),
      `out-of-scope doc: ${p.title}`
    );
  }
});

await test("fetchSopContext: scope excluding every hit falls to conservative path", async () => {
  const ctx = await fetchSopContext({
    ...BASE,
    dispositionCategory: "Anything",
    disposition: "at all",
    scope: { tags: ["no-such-team-tag"], match: "any" },
  });
  assert.equal(ctx.skipped_reason, "no_hits_in_team_scope");
  assert.equal(ctx.block_text, "");
  assert.equal(ctx.provenance.length, 0);
});

await test("renderSopBlock: flowchart docs get the reading note", () => {
  const block = renderSopBlock([
    {
      doc: {
        id: "x",
        title: "Check-In (Flow)",
        body: "flowchart TD\n A-->B",
        body_format: "flowchart",
        flags: [],
      },
      hit: {},
    },
  ]);
  assert.ok(block.includes("process flowchart"), "missing flowchart note");
  assert.ok(block.includes('%% def'), "note should explain %% def lines");
});

await test("renderSopBlock: prose docs carry no flowchart note", () => {
  const block = renderSopBlock([
    { doc: { id: "y", title: "NMT Extensions", body: "Prose.", flags: [] }, hit: {} },
  ]);
  assert.ok(!block.includes("process flowchart"));
});

if (failures.length) {
  console.log(`\n${failures.length} FAILED`);
  process.exit(1);
}
console.log("\nall tests passed");
