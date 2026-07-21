# PulpoConnection — SOP grounding via the Pulpo KB (RAG v2 design)

> Supersedes SopRag.md (RAG v1, unmerged draft on this PR). Landing is
> centralizing its knowledge base in **Pulpo** — an internal, RAG-focused
> KB platform for employees and AI agents alike, reachable over MCP.
> This kills our build-it-ourselves retrieval stack (bge-m3, HNSW,
> registry migrations, sync pipeline) and replaces it with a thin MCP
> client and a retrieval policy keyed on the verified call disposition
> that DispositionDesign C3 put in our hands. What v1 got right —
> corpus integrity gates, benchmark-before-belief, non-fatal failure
> semantics, shadow-then-flip rollout — carries forward unchanged.

| | |
|---|---|
| **Status** | v2 draft — 2026-07-21, replaces SopRag v1 wholesale |
| **First consumer** | the scoring prompt's SOP context (unchanged from v1) |
| **Corpus** | Pulpo library (`plasticity.heypulpo.com`) — coach cards migrate IN; Notion AND the coach-cards Sheet both retire |
| **Retrieval** | Pulpo MCP `search_knowledge_base` / `get_document`; embeddings, rerank, versioning, freshness are Pulpo's problem now |
| **Retrieval key** | the C3 disposition (verified, finite label space, pre-score) — the deterministic key v1's cascade was invented to synthesize |
| **Rollout** | `PULPO_SOP_MODE` off → shadow → on (the CC_GROUNDING_MODE pattern that just shipped cleanly) |
| **Related** | DispositionDesign.md §5 (grounding block, disposition stamps), [[project_sandy_qa_migration]] (thin-transport portability), LandGPT.md (annotation cascade deferred there) |

---

## 1. What Pulpo is (evidence: admin console, 2026-07-21)

An internal KB platform (built by a shareholder; we integrate, we don't
operate it). Facts gathered from the admin console:

- **Library**: ~73 docs today, tagged/sectioned (`nmt`, `ota`,
  `verifications`, `sofia`, …), each with an owner, updated timestamp,
  freshness tracking, and per-passage review flags. Collaborative,
  company-wide editing — the whole org curates one corpus instead of
  every team running its own Sheet/Notion.
- **Ask Pulpo KB**: chat-style Q&A over the corpus with numbered
  citations. An AI pre-layer checks retrieved data for inconsistencies,
  flags missing processes, and tailors answers to the asker's context
  (audience-, team-, even person-sensitive).
- **MCP endpoint**: `https://plasticity.heypulpo.com/api/mcp`,
  Streamable HTTP transport (no SSE-only, no stdio),
  `Authorization: Bearer <token>`. Revocation is instant.
- **Tools**:
  - `search_knowledge_base({ query | queries[], rerank?, limit? })` —
    semantic search; single query or batch of ≤20. Hits carry `id, url,
    title, excerpt, tags, owner, last-verified date, open_flag_count,
    score, score_type` (`cosine` or `rerank`).
  - `get_document({ id })` — full body + `open_flags` (each with
    `quote, body, suggestion, anchor_status`).
  - `flag_document({ id, quote, body?, suggestion? })` — flag an exact
    passage for review.
  - `upsert_document({ id?, title, body, private?, audiences? })` —
    create/update a doc. NOTE: no `tags` parameter exposed (§9 Q2).
- **Token model**: minted per agent with a bound **audience**
  (`Internal` / `Customer` — the agent cannot widen its own scope at
  call time) and an optional **acts-as person binding** (private-doc
  access + private-by-default writes). Unassigned tokens read/write
  public docs only.
- **Batch & cache mechanics**: `queries[]` shares one embedding call;
  query embeddings are cached 24h — repeated checklists cost almost
  nothing after the first run. `rerank` defaults ON for single queries,
  OFF for batches (it is the most expensive step, and "an agent reading
  the candidates itself rarely needs it" — that is exactly our case).
- **Rate limits**: 60/min + 2,000/day per token; 120/min + 5,000/day
  per org. A batch of N counts as N queries — batching saves latency
  and embedding cost, not quota. We are one tenant among the org's
  agents: caching and thresholds are citizenship, not just efficiency.
- **Recommended agent pattern** (their docs): search → `get_document`
  for everything you'll cite (excerpts are too short to synthesize
  from) → synthesize in YOUR model, warning inline when citing flagged
  passages. Retrieval is theirs; synthesis is ours — which suits us,
  since our synthesis call is the Gemini scoring call itself.

## 2. What changes vs. SopRag v1 — and what survives

**Deleted wholesale** (Pulpo owns these concerns now):

| v1 machinery | Fate |
|---|---|
| bge-m3 local embedder, latency gates, CPU sizing | gone — Pulpo embeds |
| R1 registry migration (`sop_documents` deltas, `sop_triggers`, HNSW-1024) | never ships; `embeddings.*` (007) stays consumer-less, mothballed for LandGPT v2 to claim or a future migration to drop |
| §4 recurring sync pipeline (Sheet → registry, diff/hash/re-embed) | gone — Pulpo IS the editing surface; migration is one-way, once |
| Three-stage cascade **as a retrieval requirement** | gone — the C3 disposition is a better query than an annotated transcript, and it exists before any model call. Stage-A annotation remains a LandGPT-era idea (audio artifacts, re-score dividend) and moves to LandGPT.md's scope |
| `qa.evaluation_sop_documents` join table | deferred — provenance rides `dialpad_call_metadata` JSONB (house forward-compat pattern); promote to a table when analytics demand SQL joins |

**Carried forward** (v1 decisions that survive the platform swap):

- **First consumer: the scoring prompt.** Unchanged.
- **Integrity gates before the corpus moves** (§5): v1's hard/soft gate
  design applies to the one-time migration instead of a recurring sync.
- **Benchmark before belief** (§6): the Triggers tab's 1,549 labeled
  (member phrase → correct card) pairs remain our labeled set — now
  they benchmark Pulpo's retrieval instead of bge-m3's.
- **Non-fatal retrieval**: scoring NEVER blocks on SOP context — the
  `sop_context_missing` conservative path is the fallback, exactly as
  today (and exactly the cc_context doctrine).
- **Shadow-then-flip rollout**: `PULPO_SOP_MODE` mirrors
  `CC_GROUNDING_MODE`, which just carried C3 to production smoothly.
- **Notion decommission** (§8): still the final slice — now the
  coach-cards Sheet retires with it.

## 3. Principles

1. **Retrieval is Pulpo's; synthesis is Gemini's.** We follow Pulpo's
   recommended agent pattern with our scoring call as the synthesizer.
   We do not call their chat layer for scoring.
2. **Scoring retrieval must be reproducible.** The QA token is
   **Internal audience, unassigned** (no acts-as): retrieval must not
   vary by which analyst clicked Score, and evals must be re-derivable.
   Pulpo's context-sensitivity (shift-aware answers, person-bound
   private docs) is a feature for future *interactive* surfaces, and a
   hazard for scoring — we opt out by token construction, and record
   consulted doc ids + updated stamps as provenance since the corpus
   itself evolves.
3. **The disposition is the query.** A verified, finite label space
   (9 categories × ~50 subs) means retrieval is cacheable on both
   sides — Pulpo's 24h query-embedding cache and our own per-label TTL
   cache — and semantically exact ("what the agent said the call WAS").
4. **Be a good tenant.** Thresholds before fetches, caches before
   queries, batches for sweeps, `rerank=false` when Gemini reads the
   candidates anyway. Org quota is shared with every other Landing
   agent.
5. **Thin transport, portable policy.** The MCP client is one small
   module speaking Streamable-HTTP JSON-RPC; the retrieval policy is
   pure functions — the Sandy re-platform (TS, native MCP SDKs) carries
   the policy and swaps the transport, same as the webhook fold.

## 4. Design

### 4.1 `pulpo_client.py` — transport (thin)

MCP over Streamable HTTP: `initialize` handshake + `tools/call`
requests as JSON-RPC POSTs with the Bearer token; httpx-async, short
timeouts (connect 3s, read 8s). No MCP SDK dependency for three tool
calls — a ~100-line client keeps the Railway image lean and the Sandy
port trivial. Env: `PULPO_MCP_URL`, `PULPO_MCP_TOKEN` (Railway env +
`.env`; never in source — Pulpo's own admin page says so).

Exposes exactly: `search(queries, *, limit, rerank)`, `get_document(id)`,
`flag_document(...)`, `upsert_document(...)` — typed thin wrappers, no
policy.

### 4.2 `sop_retrieval.py` — policy (pure where possible)

At Stage 1, alongside the CC grounding fetch (both are pre-score
context gathering):

1. **Query build**: disposition present →
   `"{category} — {subdisposition}"` (the human label IS the semantic
   query; no prompt-engineering wrapper). Absence path → fall back to
   the first ~600 chars of transcript as the query; no transcript
   either → skip retrieval (`sop_context_missing`).
2. **Search**: `search_knowledge_base({query, limit: 5, rerank: false})`
   — Gemini reads full bodies, so we skip Pulpo's expensive precision
   pass (their own guidance). Threshold on `score`: start τ = 0.55
   cosine, **set properly by the §6 benchmark, not vibes** (v1 rule).
3. **Fetch**: `get_document` for the top ≤3 hits above τ.
4. **Flags**: docs with `open_flags` are still injected (best available
   SOP) but the block appends a one-line caution per flagged passage —
   mirroring Pulpo's own citation-warning behavior — and the flag count
   lands in provenance.
5. **Inject**: the existing `SOP_CONTEXT_BLOCK` grows a numbered
   multi-doc shape (title + body per doc, ≤3 docs, ~4k-token cap;
   truncate lowest-scoring first). `sop_title`/`sop_used` carry the
   top doc's title — existing analytics columns unaffected.
6. **Provenance**: `dialpad_call_metadata.pulpo_docs =
   [{id, title, score, score_type, updated_at, open_flag_count}]`
   stamped in the same Stage-1 write (the C3 reproducibility instinct).
7. **Cache**: in-process per-query-string TTL cache (60 min) of search
   results + doc bodies. Steady state, a day of MS scoring costs a
   handful of quota units, not hundreds.

**Failure semantics**: any transport/tool error → log, return empty,
scoring proceeds on the conservative path. Timeouts are short; there is
no retry loop inside the scoring request (the next call retries by
existing).

### 4.3 Rollout — `PULPO_SOP_MODE`

`off` (default) → `shadow` (retrieve + log the would-be block +
provenance stamps, prompt still uses the Notion path) → `on` (Pulpo
block replaces Notion fetch). Env-gated per the C3 playbook: flip is a
Railway env change, no deploy. Shadow window doubles as the honest
retrieval test on real call traffic (v1 §5's point exactly).

### 4.4 Query volume budget

Today (~30 scored calls/day): ≤60 queries/day worst case, ~a dozen with
the label cache. Post-automation (~500 calls/day): ≤1,000 worst case,
but the finite label space caps unique queries near the taxonomy size
(~50/day steady state after caching). Both fit 2,000/day/token with
room; the org cap (5,000/day) is why the cache is a requirement, not an
optimization. Benchmarks and sweeps use a **separate token** so a
harness run can never starve production scoring.

## 5. Corpus migration — coach cards → Pulpo (one-way, gated)

The coach-cards Sheet (222 cards, 14 categories, §2 of SopRag v1) is
the seed corpus for MS QA. One operator-run script, v1's gate design
intact:

1. **Pull + validate**: hard gates abort (duplicate `card_id`, empty
   name/body); soft gates report (orphan steps/triggers, count drift —
   all five known findings from the 2026-07-08 profile).
2. **Assemble** one Pulpo doc per card: `title` = card name; `body` =
   description + ordered steps + a trailing **"Member phrases"** section
   from the Triggers tab (median 7 real member phrasings per card —
   embedding them in the body is free retrieval signal for
   member-speech-shaped queries).
3. **Upsert** via `upsert_document` (public, Internal audience),
   recording `card_id → pulpo_doc_id` in a local manifest JSON (the
   migration's idempotency key: re-runs upsert by `id`, never
   duplicate).
4. **Tag/section curation**: pending §9 Q2 (no tags param on the MCP
   tool) — either admin-UI bulk tagging after upload or an API the
   Pulpo team exposes. Proposed convention: section `member-support`,
   tags per card category + `coach-card`.
5. **Spot-check**: Ask Pulpo KB with a handful of trigger phrases and
   verify the migrated cards answer (the platform's own UI is the
   acceptance surface).

The Sheet stays read-only for Dialpad's live pop-ups until that
consumer is re-pointed or retired (out of scope here); it stops being
an SOP editing surface the day the migration lands. **Pulpo is the
single editing surface** — that is the centralization mandate.

## 6. Retrieval quality — benchmark before flip (v1 §7, re-targeted)

`scripts/benchmark_pulpo_retrieval.py`: for a sample of trigger phrases
(start ~300 of the 1,549; full run is most of a day's token quota),
batch `queries[]` (20/batch, rerank off) against Pulpo, check the
expected card's rank. Report **recall@5 and MRR@5**, per category and
overall; run once with `rerank: true` on the sample to price the
precision pass before declining it.

- **Acceptance to flip `on`**: recall@5 ≥ 0.9 on the English sample
  (v1's number; recalibrate after the first real run).
- **Spanish slice**: machine-translate a phrase sample, verify ES→EN
  retrieval holds (Pulpo's multilingual claim, our Spanish-call
  reality).
- The 24h embedding cache makes re-runs nearly free — re-benchmark
  after any large corpus edit, not on a schedule.
- Trigger phrases are near-duplicates of member speech, so numbers will
  flatter; the shadow window on real calls is the honest test (v1's
  caveat, unchanged).

## 7. Later consumers (designed-for, not built now)

- **SOP coverage audit**: a periodic batch — one query per disposition
  taxonomy label (fits in 3 batches) — flags labels whose best hit
  falls below τ: "dispositions with no SOP". Report to the owner; feeds
  Pulpo gap-filling. This is Pulpo's stated QA-sweep use case and our
  cheapest high-value follow-up.
- **`flag_document` loop**: when human review overturns an AI score
  that cited an SOP passage, the reviewer can flag that exact passage
  with the eval as context — QA becomes a curation input to the
  company KB. Governance (who flags, wording) owner-decided; the seam
  is one client call.
- **Interactive analyst assistant** (scorecard editor "Ask Pulpo"
  panel): would use per-person acts-as tokens and Pulpo's
  context-sensitivity — precisely the features scoring opts out of.
  Post-Sandy candidate.
- **One-pager / assessment coaching context**: retrieval by an agent's
  weak sections → relevant coach cards cited in the assessment. After
  the scoring path proves the corpus.

## 8. Decommissions (final slice, explicit)

- Delete `backend/services/notion_service.py` + call sites
  (`fetch_sop_for_call` keyword classifier and whole-page fetch);
  remove `NOTION_API_KEY` everywhere; grep-gate `notion` out of
  `backend/` (v1 §9 verbatim).
- Retire the coach-cards Sheet as an editing surface (§5); its
  Dialpad pop-up consumer is unchanged and out of scope.
- `embeddings.*` (007/008): document as mothballed pending LandGPT v2;
  no new consumers.

## 9. Open questions (owner / Pulpo team)

1. **Section/tag namespace**: coordinate with other Pulpo admins before
   the migration lands 222 docs — proposed section `member-support`,
   tags = card categories + `coach-card`. Also: do sections drive any
   retrieval scoping per token, or are they purely organizational?
2. **Tagging via API**: `upsert_document` exposes no `tags` param —
   admin-UI bulk tagging after migration, or can the Pulpo team expose
   tags on upsert? (Blocking for §5 step 4 only, not for retrieval.)
3. **Audience semantics for search**: confirm an Internal token's
   `search_knowledge_base` spans all public Internal docs org-wide —
   i.e., our QA queries can also hit non-QA docs (`nmt`, `ota`, …).
   Likely desirable (real SOP answers live there too); if noisy,
   does Pulpo support scoping search to sections?
4. **Rate-limit headroom post-automation**: confirm the per-org
   5,000/day pool won't be contended once other Landing agents scale;
   ask whether per-token quotas are adjustable.
5. **Shift/context sensitivity**: confirm scoring's unassigned token
   receives context-NEUTRAL retrieval (no time-of-day tailoring) — the
   reproducibility requirement in §3.2. If the platform tailors by
   token metadata anyway, we need to know what varies.
6. **τ and k**: start τ=0.55 / k=5 / inject ≤3; set from §6 numbers.

## 10. Build plan (each slice one PR, pytest-gated)

| # | Slice | Checkpoint |
|---|---|---|
| P1 | `pulpo_client` transport + smoke script (`scripts/pulpo_smoke.py`: search one query, fetch one doc, print) | unit tests with mocked transport; live smoke against the real endpoint prints titles + scores |
| P2 | `sop_retrieval` policy + `PULPO_SOP_MODE` shadow wiring + provenance stamps + multi-doc SOP block | prompt-build pytest (disposition / absence / flagged-doc / empty-retrieval); shadow logs on real calls |
| P3 | Corpus migration script (gates + manifest + upsert) → 222 cards live in Pulpo | gate unit tests on synthetic tabs; live spot-check via Ask Pulpo; manifest committed |
| P4 | Benchmark harness (batch, sampled, separate token) | recall@5 / MRR@5 recorded IN THIS DOC; rerank on/off comparison; Spanish slice |
| P5 | Flip `PULPO_SOP_MODE=on` + Notion decommission + Sheet retirement note | shadow-vs-live compare read clean; no-notion grep gate green |
| P6 | Coverage audit batch job (deferred until P5 settles) | per-disposition coverage report lands with the owner |

P1–P4 carry zero scoring-path risk (shadow mode); P5 is the flip.
