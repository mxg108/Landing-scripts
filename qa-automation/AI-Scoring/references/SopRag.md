# SOP RAG — Coach-Cards Registry + Three-Stage Scoring Cascade

> Design for RAG v1: the SOP retrieval subsystem (corpus, registry, sync, embeddings,
> benchmarks) and the three-stage scoring cascade that consumes it. Replaces the keyword
> matcher + whole-page Notion dump in `notion_service.py` (§9 decommission). Builds on the
> deployed-but-consumer-less `embeddings.*` schema (migration 007, SQLMigration.md §5) and
> the LandGPT integration shape (§8) — the cascade here is deliberately the LandGPT v2 shape
> run on Gemini today, so the Local AI cutover becomes a per-stage provider swap.

| | |
|---|---|
| **Status** | v1 draft — owner decisions locked 2026-07-08 |
| **First consumer** | the scoring pipeline's `sop_sections` prompt context |
| **Corpus** | Dialpad coach-cards Google Sheet (§2) — Notion fully retired |
| **Embedder** | bge-m3, local/CPU, 1024-d (`embedding_1024` column) |
| **Team scope** | Member Support first; Sales onboards as corpus + config |
| **Depends on** | pgvector ≥ 0.7 (007/008 shipped); LandGPT.md v2 scope (inherits this layer) |

**Owner decisions (2026-07-08):**

1. **First consumer: the scoring prompt, now.** Retrieval feeds Gemini's `sop_sections`
   context immediately; LandGPT v2 inherits the same retrieval layer.
2. **Embedder: bge-m3 local.** Open-source, CPU-deployable (the only kind the schema allows
   as `is_current`), strong ES/EN cross-lingual — and transcript-derived queries never leave
   Landing's network, consistent with the LandGPT rationale.
3. **Corpus: the coach-cards Sheet** (see §2) — human-curated, pre-chunked. The Sheet stays
   the operator editing surface; Postgres is the runtime source of truth. **Notion is
   retired** (§9).
4. **Only the call transcript is embedded** as the retrieval query — specifically the Stage-A
   *annotated* transcript (§5), because Dialpad's transcript AI is weak and cannot handle
   Spanish. Rubric questions are not part of the query.
5. **Provenance via join table** — every consulted card is recorded per evaluation (§3.3).
6. **Three-stage cascade** (§5): annotate → retrieve → score, as separate calls. This
   multiplies API calls, so **cost attribution and auditing are first-class** (§6).

---

## 1. Purpose & scope

**In scope**
- The SOP registry: card-granular lifecycle (lookup active / update / re-embed / retire /
  store) over the `embeddings.*` schema, with immutable version rows (§3).
- The sync pipeline from the coach-cards Sheet, with hard verification gates (§4).
- The three-stage scoring cascade and its persisted artifacts (§5).
- Cost/audit accounting for the extra calls (§6); retrieval benchmarks (§7); Notion
  decommission (§9); phased build plan (§10).

**Out of scope / non-goals (v1)**
- LandGPT inference itself (`landing-ai/LandGPT.md` owns it — this doc only keeps the seams
  provider-swappable).
- Employee-facing SOP Q&A surfaces; editing SOPs anywhere but the Sheet.
- Sub-card chunking (§2 shows cards fit in one chunk) and multi-modal embeddings.
- Dialpad coach-card pop-up management (the Sheet's original consumer) — unchanged.

---

## 2. Corpus — the Dialpad coach-cards Sheet

Spreadsheet `1z9bNOQKcaE2tWcMr_pq7_VwogaU2j7IhskcVjGbNqTo` ("Coach Vards V0.5"), shared with
the QA service account. Built as live agent pop-up cards (historically dismissed mid-call);
repurposed here as the QA scoring SOP corpus — consulted *after* the call, where it can't
distract anyone.

**Tabs** (per its Instructions tab, which documents the operator workflow we preserve):

| Tab | Shape | Role |
|---|---|---|
| `Cards` | 222 rows × 10 cols — `card_id (key)`, Category, #, Name, Description, Language, Trigger phrases (auto), Rep response (auto), Sources (info), Entered? | The assembled card. `card_id` is the join key across tabs. "auto" columns are formulas over Steps/Triggers |
| `Steps` | 1,390 rows — card_id, step_num, step_text | Source of truth for card body; median 7 steps/card |
| `Triggers` | 1,549 rows — card_id, trigger_phrase | Real member phrases → card; median 7/card. **Doubles as the labeled benchmark set (§7)** |
| `Instructions` | prose | Operator how-to; count claims live here (drift-checked at sync) |

**Profile (2026-07-08):** 14 categories × 222 cards, all `English (US)`. Full-card text
(name + description + steps) median ≈480 tokens, p90 ≈665, max ≈965 — **one card = one
chunk**, no sub-chunking (bge-m3 window is 8192). Spanish calls still retrieve correctly via
bge-m3's cross-lingual embedding (ES query → EN card); Spanish card variants can later
coexist via the existing `language` column.

**Known integrity drift (2026-07-08), motivating §4's gates:** Instructions claims 235 cards
vs 222 present; 1 orphan Steps card_id; 8 orphan Triggers card_ids; 4 cards with no steps;
5 with no triggers.

The `Sources (info)` column (old Notion guide names) is ingested as inert provenance
metadata only.

---

## 3. SOP Registry — schema deltas (one `embeddings.*` migration)

Migration 007 shipped a "few large documents" model; the corpus reality is "many small
cards". One migration (next free number) adapts it — cheap now because the schema is
consumer-less (Wave2Plan §3: deployed, no consumer).

### 3.1 `embeddings.sop_documents` — card-granular lifecycle

New columns:

| Column | Type | Notes |
|---|---|---|
| `source_key` | TEXT NOT NULL | the Sheet's `card_id` — stable business key across versions |
| `category` | TEXT | Cards.Category (also `heading_path[0]` on the chunk) |
| `description` | TEXT | Cards.Description |
| `status` | TEXT NOT NULL DEFAULT 'active' | `active` / `retired` (CHECK) |
| `retired_at` | TIMESTAMPTZ | set when the card vanishes from the Sheet (§4) |
| `content_hash` | TEXT NOT NULL | sha256 of the assembled card text — the §4 diff key |
| `source` | TEXT NOT NULL DEFAULT 'coach_cards_sheet' | future corpora coexist |
| `source_ref` | TEXT | inert provenance (the card's `Sources (info)` text) |

Constraint changes:
- **Drop** `uq_sop_documents_current_per_team_language` (one current doc per team+language —
  incompatible with 222 concurrent cards).
- **Add** partial UNIQUE: one `is_current=TRUE` per `(team_id, source_key, language)`.
- `version_tag` becomes the per-revision tag (`<source_key>@v<n>`); UNIQUE
  `(team_id, version_tag)` already accommodates it.

**Immutable versioning, house pattern** (`formula_versions` / `rubric_versions`): a card
revision inserts a NEW row (version n+1), flips `is_current`, and never mutates the old row —
so `sop_used_document_id` and the §3.3 join table reproduce "what the scorer saw" forever,
including for since-retired cards. **Retirement flips `status`, never deletes.**

### 3.2 `embeddings.sop_triggers` — labeled phrases

`(id, document_id FK→sop_documents ON DELETE CASCADE, phrase TEXT NOT NULL)`. Re-synced with
its card version. Two consumers: the §7 benchmark harness, and a future lexical/hybrid
retrieval channel (v1 retrieval is vector-only; the table just preserves the signal).

### 3.3 `qa.evaluation_sop_documents` — retrieval provenance (join table)

| Column | Type | Notes |
|---|---|---|
| `evaluation_id` | BIGINT FK qa.evaluations ON DELETE CASCADE | |
| `sop_document_id` | BIGINT FK embeddings.sop_documents | the exact version row consulted |
| `rank` | SMALLINT NOT NULL | 1..k by similarity |
| `similarity` | NUMERIC(6,5) | cosine similarity at retrieval time |
| `used_in_prompt` | BOOLEAN NOT NULL | retrieved-and-injected vs retrieved-and-cut |
| `created_at` | TIMESTAMPTZ | |

UNIQUE `(evaluation_id, sop_document_id)`. The legacy scalar
`qa.evaluations.sop_used_document_id` (§3.4 of SQLMigration) is written with the **rank-1**
row for dashboard convenience; the join table is the real record.

Chunks: one `sop_chunks` row per card version (`chunk_index 0`,
`heading_path = [Category, Card Name]`); embeddings land in `sop_chunk_embeddings.embedding_1024`
under the bge-m3 row in `embedding_models` (`is_current`, modality `text`). HNSW index on
the 1024 column ships with the migration if 008 didn't already cover it.

---

## 4. Sync pipeline — Sheet → registry, diff-based, gated

`scripts/sync_coach_cards.py` (operator-run; cron candidate once boring). Modeled on the
migration scripts' verification-gate pattern.

1. **Pull** all 4 tabs via the service account.
2. **Validate — hard gates** (abort, no writes): duplicate `card_id`s; empty card name/body;
   malformed rows. **Soft gates** (warn in report, proceed): orphan step/trigger card_ids;
   cards missing steps or triggers; Instructions count drift. (All five soft findings exist
   today — §2 — so the report is exercised from run one.)
3. **Assemble + hash** each card: name + description + ordered steps → canonical text →
   sha256.
4. **Diff against registry by `source_key`:**
   - **New** → insert version 1 (+ chunk + triggers) and embed.
   - **Changed hash** → insert version n+1 (+ chunk + triggers), embed, flip `is_current`.
   - **Unchanged** → no-op (no re-embed — this is what makes frequent syncs cheap).
   - **Vanished from Sheet** → `status='retired'`, `retired_at=NOW()`. History intact.
   - **Reappeared** (retired key returns) → new version row, `status='active'`.
5. **Mass-retirement guard:** if >20% of active cards would retire in one run, ABORT —
   suspected sheet corruption/truncation, not editorial intent. Override flag for real purges.
6. **Report + audit:** per-run `embedding_runs` row (counts, tokens, duration, status) and a
   printed summary (new/changed/retired/unchanged, gate findings, drift).

Re-embedding the whole corpus (embedder upgrade) = same pipeline with `--model` targeting a
new `embedding_models` row — the chunk/embedding split (§5.5/§5.6 of SQLMigration) was built
for exactly this A/B.

**Lifecycle surfaces:** the Sheet + sync gives update/retire/store; a read-only
`GET /api/{team_id}/rag/sops` (list active cards, filter by category; `?include=versions`
for history) gives lookup. Mutation endpoints beyond the Sheet are deliberately deferred.

---

## 5. Three-stage scoring cascade

Today: one Gemini call (audio + Dialpad transcript + rubric → scorecard). Dialpad's
transcript is poor and has no Spanish, and single-call scoring can't retrieve. The cascade
splits the work into separate calls with persisted artifacts between them:

| Stage | Call | Input | Output (persisted) | Provider today → LandGPT v2 |
|---|---|---|---|---|
| **A — Annotate** | cloud, audio | call audio + Dialpad transcript (as a hint, not ground truth) | `qa.evaluations.annotated_transcript` JSONB (§8.2 shape: turns, speaker, emotion, pace, language_detected) | Gemini → Qwen2-Audio |
| **B — Retrieve** | local, free | annotated transcript text (turns concatenated) | top-k cards → §3.3 join table + `sop_used_document_id` | bge-m3 (unchanged) |
| **C — Score** | cloud, text-only | annotated transcript + General Landing Rules + retrieved cards (for `sop_sections`) + rubric | scorecard (existing Stage-1 draft path) | Gemini → Gemma |

This is **deliberately the LandGPT cascade shape** (SQLMigration §8): `models_used.audio`
records Stage A's provider, `models_used.text` Stage C's — the existing JSONB shape already
models it. A free-form `models_used.retrieval` entry records the embedder + k + skip
reasons. §8.2's "annotated_transcript is NULL for Gemini-scored evaluations" note is
superseded: Gemini now produces it, with `schema_version: "gemini_annotate_v1"` (same turn
shape, same Pydantic validator — `schema_version` exists precisely so producers can vary).
Queue the SQLMigration v1.5 doc update alongside the §3.8 one Wave2Plan already owes.

**Why this shape wins:**
- **Spanish works**: Stage A listens to the audio; Dialpad's English-only transcript is just
  a hint. `language_detected` lands on the row; bge-m3 retrieves EN cards from ES queries.
- **Re-scoring never re-annotates**: formula iterations, v6 re-ships, appeals, and Stage-C
  retries reuse the persisted transcript — the expensive audio call runs once per call, ever.
- **Auditability**: the annotated transcript is the inspectable artifact for score disputes
  (§8.2's claim) — we get it before LandGPT ships.
- **Retrieval gets a real query**: cards are matched against what was actually said, not
  keyword heuristics.

**Failure semantics:**
- **A fails** (after retries): `scoring_status='errored'`; no degraded single-call fallback —
  during rollout the old path still exists behind the flag (below).
- **B fails or returns nothing** (embedder down, no index): **non-fatal** — score without SOP
  context (exactly today's Notion-fetch behavior), join table empty,
  `models_used.retrieval.skipped_reason` set.
- **C fails**: retry from the persisted artifact; audio is never re-uploaded.

**Rollout — house pattern:** per-team flag (`scoring_pipeline: 'single_call' | 'cascade'`,
operator-flipped like `scoring_owner`), preceded by a **shadow window**: cascade runs on a
sample alongside the live single-call path, scores compared side by side (the
historic-compliance-sweep playbook) before any flip. MS first; Sales after its corpus lands.

Retrieval parameters to start: **k=5** retrieved and recorded; all 5 injected (≈2.4k tokens
at the ≈480-token median) with `used_in_prompt=TRUE`; tune with §7 numbers, not vibes.

---

## 6. Cost + audit — first-class, because calls multiply

Per-call cloud work goes from one audio call to **A (audio) + C (text)**; B is local. Before
optimizing, **measure**:

- **Per-stage audit rows**: `qa.api_audit_log` gets one row per stage (`action` =
  `annotate` / `score`; retrieval logs only on skip/error — it's free and local), each with
  its own `estimated_cost_usd` and `duration_ms`. `qa.evaluations.estimated_cost_usd`
  becomes the per-evaluation SUM across stages.
- **Cost visibility before cost cutting**: a `/team/costs`-style rollup (or SQL in the
  runbook) splits monthly spend by stage and model. Only then pick optimizations — candidates,
  in likely order: Stage-A prompt/output-token trims (the §8.2 schema is already terse),
  cheaper text model for Stage C, batch windows. **Not** candidates: dropping artifact
  persistence (it's what pays the pipeline back on every re-score).
- **The re-score dividend, made explicit**: any re-run that starts from a persisted
  annotated transcript skips Stage A. The August v6 re-ship and every formula iteration
  sweep become text-only workloads.
- Existing knobs unchanged: `ai_provider_primary` for dashboard splits; LandGPT-era rows
  leave cloud cost NULL per §8.1.

---

## 7. Retrieval quality — benchmarks before belief

The Triggers tab ships 1,549 labeled (member phrase → correct card) pairs. The §5.8
methodology becomes concrete:

1. **Harness** (`scripts/benchmark_sop_retrieval.py`): for each trigger phrase, embed, run
   HNSW top-5, check the expected card's rank. Report **recall@5 and MRR@5**, per category
   and overall. Baseline before wiring Stage B; re-run per embedder candidate and per corpus
   sync (cheap).
2. **Cross-lingual slice**: machine-translate a sample of trigger phrases to Spanish, verify
   ES→EN retrieval doesn't collapse (bge-m3's headline claim, our Spanish-call reality).
3. **Latency gate** (§5.8): embed-a-query p95 < 100ms on a 2-vCPU CPU-only container;
   failures cannot be `is_current`.
4. **Acceptance to enter the shadow window**: recall@5 ≥ 0.9 on the English set (tune after
   first real numbers; the point is a number exists before the cascade flips).

Trigger phrases are *near-duplicates of real member speech* — expect benchmark numbers to be
optimistic vs. full-transcript queries. The shadow window (§5) is the honest test.

---

## 8. Sales extension (deferred)

Everything is config + corpus: a Sales cards corpus (new Sheet or new tab-set) syncs under
`team_id='sales'`; retrieval is already team-scoped; `sop_sections` for Sales
(`landing_guarantee`, `pricing`) are declared in its scoring prompt config. No code changes
expected. Blocked on a Sales corpus existing.

---

## 9. Notion decommission (final slice, explicit)

- Delete `backend/services/notion_service.py` and its call sites in the scoring pipeline
  (keyword classifier + whole-page fetch).
- Delete the `rag/notion_sync/` stub (and `rag/embeddings/embed.py` stub once R3's real
  module lands).
- Remove `NOTION_API_KEY` from `.env` / `.env.example` / Railway.
- Grep-gate: no `notion` reference remains under `qa-automation/AI-Scoring/backend/` (a unit
  test can assert this the same way the `qa_scoring` regression guard does).
- The Sheet's `Sources (info)` Notion titles remain as inert `source_ref` strings — history,
  not integration.

---

## 10. Build plan (each slice one PR, pytest-gated per [[feedback_design_doc_first]])

| # | Slice | Checkpoint |
|---|---|---|
| R1 | Registry migration (§3: sop_documents deltas, sop_triggers, qa.evaluation_sop_documents, HNSW-1024) | integration tests per house `test_migration_*` pattern: constraints, partial uniques, version-flip semantics |
| R2 | Sync pipeline (§4) — registry rows only, no embeddings yet | unit tests on gates/diff/hash with synthetic tabs; first real ingest lands 222 active cards + the drift report |
| R3 | Embedder service (bge-m3, local) + embed pass + §7 benchmark harness | latency gate green; recall@5/MRR@5 baseline recorded in the doc |
| R4 | Stage A behind the team flag: annotate + persist `annotated_transcript` (`gemini_annotate_v1`) | Pydantic round-trip tests; per-stage audit rows; shadow rows accumulating |
| R5 | Stages B+C: retrieval into the scoring prompt, join-table provenance, GLR + cards in the text call | golden prompt-shape tests; non-fatal-retrieval tests; shadow comparison report |
| R6 | Flip MS to cascade + Notion decommission (§9) + cost rollup | smoke per §5 rollout; no-notion grep gate; cost dashboard shows per-stage split |

R1–R3 are pure subsystem work (no scoring-path risk); R4–R6 touch scoring and ride the flag.

---

## 11. Open questions

1. **GLR content** — the General Landing Rules bullet points for Stage C are still owed by
   the owner (same item the v6 bundle has been waiting on).
2. **bge-m3 hosting** — inside the Railway backend container (simplest; watch image size and
   cold-start) vs. a sidecar service. Decide in R3 with the latency-gate numbers.
3. **Stage-A prompt** — how hard to lean on Dialpad's transcript hint before it hurts more
   than helps on Spanish calls; iterate during R4's shadow window.
4. **k and injection budget** — start k=5/inject-5; revisit with §7 data.
5. **Very long calls** — transcripts beyond the embedder window: truncate head+tail vs.
   windowed mean-pooling. Decide with real length distributions from R4.
6. **Sync cadence** — operator-run vs. weekly cron once gates prove stable.
