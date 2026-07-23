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
| **Status** | v2.1 — 2026-07-22, §9 questions RESOLVED (owner + Pulpo's creator); build starts at P0 |
| **Vendor** | Pulpo (`heypulpo.com/knowledge-base`) — a side project of Landing shareholder Conor. An integration OUTSIDE Landing's walls, therefore behind an explicit **provider abstraction** (§4.1): easily switchable, nothing Pulpo-shaped escapes one module |
| **First consumer** | the scoring prompt's SOP context (unchanged from v1) |
| **Corpus** | Pulpo library (`plasticity.heypulpo.com`) — coach cards migrate IN (LAST, owner-gated dedup §5); Notion AND the coach-cards Sheet both retire |
| **Retrieval** | Pulpo MCP `search_knowledge_base` / `get_document`; embeddings, rerank, versioning, freshness are Pulpo's problem now |
| **Retrieval key** | the C3 disposition (verified, finite label space, pre-score) — the deterministic key v1's cascade was invented to synthesize |
| **Rollout** | `PULPO_SOP_MODE` off → shadow → on (the CC_GROUNDING_MODE pattern that just shipped cleanly) |
| **Related** | DispositionDesign.md §5 (grounding block, disposition stamps), [[project_sandy_qa_migration]] (thin-transport portability), LandGPT.md (annotation cascade deferred there), `data_provider.py` (the factory seam this mirrors) |

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
   **both-audience (Internal + Customer), unassigned** (no acts-as):
   retrieval must not vary by which analyst clicked Score, and evals
   must be re-derivable.
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
5. **The provider is a seam, not a dependency.** Pulpo is an external
   vendor (a shareholder's side project, however friendly) — the policy
   layer imports only a neutral `RagProvider` interface and neutral
   result types; the Pulpo module is the ONLY place Pulpo shapes exist.
   Swapping vendors later = one new module + one env value, exactly the
   `data_provider` factory pattern the read-path flip proved out. The
   Sandy re-platform carries the policy and swaps the transport, same
   as the webhook fold.

## 4. Design

### 4.1 Provider abstraction — `backend/services/rag/`

Pulpo lives outside Landing; the RAG provider is an explicit seam,
mirroring the `data_provider` factory:

```
backend/services/rag/
  provider.py   RagProvider protocol + NEUTRAL dataclasses:
                  RagHit(id, title, excerpt, score, tags,
                         updated_at, flag_count)
                  RagFlag(quote, note, suggestion)
                  RagDoc(id, title, body, flags, updated_at)
                Methods: search(queries, *, limit) ->
                  list[list[RagHit]]; get_document(id) -> RagDoc|None.
                flag/upsert are OPTIONAL capability methods —
                curation flows probe for them, never assume.
  pulpo.py      the only Pulpo-shaped module: MCP over Streamable
                HTTP (`initialize` + `tools/call` JSON-RPC POSTs,
                Bearer auth; httpx-async; connect 3s / read 8s; no
                MCP SDK dependency for four tool calls). Maps Pulpo's
                shapes (score_type, open_flags, anchor_status) into
                the neutral types AT THIS BOUNDARY.
  factory.py    get_rag_provider() keyed on RAG_PROVIDER env
                ('pulpo' | 'none'; 'none' = dev/tests, retrieval
                skipped). Vendor swap = one module + one env value.
```

The policy layer (§4.2) imports `provider.py` types only. Env:
`RAG_PROVIDER`, `PULPO_MCP_URL`
(`https://plasticity.heypulpo.com/api/mcp`), `PULPO_MCP_TOKEN`
(Railway env + `.env`; never in source — Pulpo's own admin page says
so; revocation is instant).

**Token minting (P0, operator)** — resolved semantics (§9 A1/A3/A5):
- **`QA-Scoring (prod)`**: BOTH audiences (Internal + Customer — an
  Internal-only token cannot see Customer-audience docs, and QA needs
  ALL docs), **unassigned** (an assigned token scopes retrieval to that
  person's private docs — the opposite of what scoring wants). No
  context-sensitivity comes from the token itself (only Ask-Pulpo's
  synthesis prompt tailors by context), so reproducibility holds
  structurally.
- **`QA-Benchmark`**: same audiences, unassigned — separate quota pool
  so harness runs never starve production scoring.

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

## 5. Corpus migration — coach cards → Pulpo (one-way, gated, LAST)

The coach-cards Sheet (222 cards, 14 categories, §2 of SopRag v1)
extends the MS QA corpus. **This slice deliberately runs last** (§9
A2): the owner must personally verify which coach-card processes are
ALREADY represented in Pulpo's existing library before upload — the
platform is collaboratively curated, and duplicate near-identical
processes would poison retrieval for everyone, not just QA. Until then,
retrieval runs against the existing corpus, with the τ threshold
routing uncovered dispositions to the `sop_context_missing` path
gracefully.

One operator-run script, v1's gate design intact:

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
4. **Tag/section curation**: section/tag **`member_support`**
   (confirmed with Pulpo's creator, §9 A1) + card category +
   `coach-card`. The tags feature is new and may be UI-only for now —
   Conor is working on exposing it via API (§9 A2); fallback is
   upserting + tagging in human-manageable batches through the admin
   UI. Owner's dedup verdicts (skip / merge-into-existing / upload)
   ride the manifest.
5. **Spot-check**: Ask Pulpo KB with a handful of trigger phrases and
   verify the migrated cards answer (the platform's own UI is the
   acceptance surface).

The Sheet stays read-only for Dialpad's live pop-ups until that
consumer is re-pointed or retired (out of scope here); it stops being
an SOP editing surface the day the migration lands. **Pulpo is the
single editing surface** — that is the centralization mandate.

## 6. Retrieval quality — two benchmarks, matched to the corpus timeline

Because the coach-cards migration runs LAST (§5), the quality gates
split in two:

**6.1 Pre-flip: the coverage probe** (`scripts/pulpo_coverage_probe.py`)
— the acceptance gate for `PULPO_SOP_MODE=on`, runnable against the
corpus AS IT EXISTS today. One query per disposition-taxonomy label
(~50 → 3 batches, rerank off, `QA-Benchmark` token): report each
label's best-hit score + title. Outputs:
- the **coverage report** (labels above/below τ) — the flip is safe at
  ANY coverage level because sub-τ labels fall through to
  `sop_context_missing`, but the report tells the owner exactly which
  call types gain grounding on day one, and doubles as the
  gap-filling worklist for Pulpo curation;
- the **τ calibration sample**: eyeball the top hits per label; a
  wrong-doc-above-τ finding raises τ before it ever misleads a score.
- The **shadow window on real calls remains the honest test** (v1's
  caveat): compare shadow logs' retrieved titles against what an
  analyst would consider the right SOP before flipping.

**Probe results — 2026-07-22 (P3 checkpoint, benchmark token, τ=0.55):**
74 distinct disposition labels observed in `command_center.calls`
(43,323 calls, the 3-month backfill). **73/74 labels covered (99%);
call-weighted coverage 43,288/43,323 (100%)**. Sole gap: *Unit Issues —
Pests* (best hit 0.528 and topically wrong — "Replacing Missing &
Damaged Items"): a genuine corpus gap, first entry on the curation
worklist. Calibration notes: hits ≥0.7 are strongly on-topic (e.g.
*OTA reservation change* → "OTA SOP - Shortenings and Cancellations"
at 0.807); the 0.55–0.60 band contains a few semantically weak matches
(*Routing & Operational* sub-labels landing on "Admin"/check-in docs) —
candidates for raising τ to ~0.60 after the shadow window's real-call
review. Rate-limit learning: the limiter rejects a batch exceeding the
REMAINING minute allowance — the probe paces 10-query batches 12s apart.

**6.2 Post-migration: trigger-phrase recall** (v1 §7, re-targeted) —
after §5 lands the cards, the 1,549 labeled (member phrase → card)
pairs benchmark properly: sampled ~300, batched 20/query-batch, rerank
off; report **recall@5 and MRR@5** per category; one rerank-on pass on
the sample to price the precision step before declining it. Spanish
slice via machine-translated phrases (ES→EN retrieval must hold).
Target recall@5 ≥ 0.9 (recalibrate after the first real run). The 24h
embedding cache makes re-runs nearly free — re-benchmark after large
corpus edits, not on a schedule.

## 7. Later consumers (designed-for, not built now)

- **SOP coverage audit**: the §6.1 probe, re-run periodically — flags
  labels whose best hit falls below τ ("dispositions with no SOP"),
  reporting coverage DELTAS to the owner as the org curates Pulpo.
  Pulpo's stated QA-sweep use case; near-free after the first run
  thanks to the 24h embedding cache.
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

## 9. Open questions — RESOLVED 2026-07-22 (owner + Pulpo's creator)

1. **Section/tag namespace** → **`member_support`** confirmed. No
   retrieval scoping per token exists except assigned (person-bound)
   tokens, which scope to that person's PRIVATE docs — the opposite of
   what scoring wants. Sections/tags are organizational.
2. **Tagging via API** → the tags feature is very new; the param may be
   UI-only for now. Conor is working on exposing it; fallback: upsert +
   tag in human-manageable batches through the admin UI. Additionally,
   the migration must wait for the owner's personal dedup pass over the
   existing library — **§5 runs LAST** as a consequence.
3. **Audience semantics** → an Internal-only token does NOT span all
   docs (it scopes to Internal-audience documents). The QA agent needs
   ALL docs → mint with **both audiences** (Internal + Customer). §4.1.
4. **Quota headroom** → not contested; upload volume is unconstrained
   for the foreseeable future.
5. **Context sensitivity** → NONE comes from the token — only the
   Ask-Pulpo synthesis system prompt tailors by time-of-day/context.
   Since scoring never uses their synthesis layer, retrieval is
   context-neutral structurally. Reproducibility (§3.2) holds.
6. **τ and k** → start τ=0.55 / k=5 / inject ≤3, tuned by §6.1's
   calibration sample. Approved.

## 10. Build plan (each slice one PR, pytest-gated)

| # | Slice | Checkpoint |
|---|---|---|
| P0 | Operator prereqs: mint `QA-Scoring (prod)` + `QA-Benchmark` tokens (both audiences, unassigned); `PULPO_MCP_URL`/`PULPO_MCP_TOKEN` into `.env` + Railway; `PULPO_SOP_MODE` unset (off) | tokens visible in the Pulpo admin console |
| P1 | `backend/services/rag/` — provider protocol + neutral types + `pulpo.py` MCP transport + factory + smoke script (`scripts/pulpo_smoke.py`) | unit tests with mocked transport (incl. neutral-shape mapping); live smoke prints titles + scores AND retrieves at least one Customer-audience doc (proves §9 A3 both-audience minting) |
| P2 | `sop_retrieval` policy + `PULPO_SOP_MODE` shadow wiring + provenance stamps + multi-doc SOP block | prompt-build pytest (disposition / absence / flagged-doc / empty-retrieval / provider='none'); shadow logs on real calls |
| P3 | Coverage probe (§6.1) + shadow-window report | per-disposition coverage report + τ calibration recorded IN THIS DOC; owner reviews shadow titles |
| P4 | Flip `PULPO_SOP_MODE=on` + Notion decommission (§8 grep gate) | shadow-vs-live compare read clean; no-notion gate green; sub-τ dispositions verifiably fall through to `sop_context_missing` |
| P5 | Corpus migration (§5 — owner dedup pass + tags path resolved first) + post-migration trigger-recall benchmark (§6.2) | manifest committed; recall@5 / MRR@5 recorded IN THIS DOC; Ask-Pulpo spot-check |
| P6 | Curation extras: periodic coverage audit, `flag_document` loop (governance owner-decided) | coverage deltas land with the owner |

P1–P3 carry zero scoring-path risk (shadow mode); P4 is the flip; P5
waits on the owner's dedup verdicts and the tags-API answer.
