# SQL Migration — Design & Implementation Reference

**Status:** v1.3 — Rubric versioning lands as a first-class concern. Until now we versioned **formulas** (`qa.formula_versions`) but the **rubric** (section definitions, AI scoring prompt, per-section metadata) lived only in `config/teams/<team>.json` as file-source-of-truth. v1.3 promotes the rubric to the database (`qa.rubric_versions`, §3.19) and shifts the configuration model to **DB-as-source**: the JSON file becomes a generated export artifact, not authoritative. Each evaluation now stamps both `formula_version` AND `rubric_version` (§3.4), making `compute_overall_score()` fully reproducible from the row alone (§3.6) regardless of how many times the rubric is reshaped later. `scoring_prompt` lives inside `rubric_json` (one row versions the AI scoring contract end-to-end — the prompt's `long_call_focus_sections` and `sop_sections` arrays reference section_ids and must be inseparable from their sections per Q1.a). Rubric ↔ formula compatibility is **hard-fail** validated at the write API (Q2.a — saving a rubric that drops a section the active formula references is rejected; the formula must update first). `public.teams` absorbs the operational config (`stats_config`, `gemini_config`, `excluded_test_agents`, plus `sheets_config` as a legacy holdover dropped at Phase D) so the JSON file is fully DB-generable. New migration `010_rubric_versioning.sql` lands in Wave 1 with seeds for both teams' current rubrics; the prior 010 placeholder → 011, 011/012/013 → 012/013/014. Spec sections added: §3.19. Updated: §3.4, §3.6, §6, §7.6, §10, §11.1.

**Prior status (v1.2):** Ops VP review (2026-06-24) adds three coupled concerns to the qa schema: (a) a **manager-driven coaching workflow** modeled as `qa.coachings` (1 session, M:N to evaluations via `qa.coaching_evaluations` — escalations from TL → Manager → HR can revisit the same eval and one session can cover multiple evals); (b) a **normalized tag taxonomy** in `qa.tags` + `qa.evaluation_tags` (4 initial human-review-focus tags: sop / soft_skills / hard_skills / efficiency; future categories `compliance`, `operational`, `product`, `outcome` ship as rows-not-migrations; per-tag-source provenance ready for LandGPT auto-tagging); (c) a **pipeline trigger** that flags evaluations for human review when `process_adherence` or `call_resolution` scores ≤ 3, surfaced via a new `'flagged_human_review'` value on `qa.evaluations.scoring_status`. The `documentation` section is deprecated (clean break — new evals never write it; historical rows keep their `evaluation_sections` rows forever) and replaced by `human_review_required` (numeric 1–5, NA-by-default → na-redistribute via the existing rule pipeline). Migrations renumbered: new `009_vp_review_additions.sql` lands in Wave 1; the prior 009/011/012 shift to 010/012/013. Spec sections added: §3.14–§3.18.

**Prior status (v1.1):** Phase A.5 reframed (again). v1's "parity report with three outcome paths" framing is replaced: the migration **ships a revised `formula_version` from day one** rather than waiting on a parity comparison to decide. Epsilon's role narrows to a **historic-compliance signal** — sweeping backfilled rows under the new formula, flagging those whose recomputed score diverges from the stored sheet score by > ε, and surfacing flagged-row patterns to iterate the new formula. The shadow-the-sheet column on `qa.evaluations` (`overall_score_parity_check_value`) is replaced by an immutable `qa.formula_compliance_sweeps` table (§3.13) so iterative re-sweeps under successive formula versions are preserved instead of overwritten. Migration file plan (§7.6), runbook (§7.7), tests (§11), and Phase ordering (§7.1) all updated to match.

**Scope:** A single Railway Postgres instance hosts three logical concerns separated by schema namespace: `qa` (replaces the Sheets-as-DB QA pipeline + houses analytics points + persists LandGPT cascade artifacts), `command_center` (Dialpad real-time state via webhook replay), and `embeddings` (model-agnostic, multi-language RAG groundwork for LandGPT v2). This doc defines the table-level shape across all three, the migration sequencing for the QA cutover, and the analytics-layer migration off Sheets.

**Prerequisite for:** Command Center Phase 1 (`qa-automation/AI-Scoring/references/LandingOpsCommandCenter.md` §9 Phase 0).

**Provider for:** LandGPT v1 (`landing-ai/LandGPT.md`). The QA pipeline's transition from Gemini to the local Qwen2-Audio + Gemma 4 cascade lands inside the same `qa.evaluations` row — provider provenance per evaluation (and per section for Plan B fallback) is first-class in v0.4 (§3.4, §3.5, §8).

**Supersedes:** `database/migrations/003_qa_scoring_schema.sql` (stub) — gap analysis in §3.1.

**Companion (delete after v1.1 sign-off):** `database/SQLMigration-v0.3-inputs.md`.

**Doc-location convention.** This doc lives in `database/` per the schema-folder convention (schema-bearing design docs colocate with their migrations; application-flow design docs stay in `qa-automation/AI-Scoring/references/`).

**Identity-column convention.** All new tables use `BIGINT GENERATED ALWAYS AS IDENTITY` rather than `SERIAL`/`BIGSERIAL`. Existing `mass_notifications` tables are not retro-migrated.

---

## 1. Purpose and non-goals

### Purpose

1. Replace Google Sheets (FR-AI → Score destination → Analyst_History) as the QA pipeline's source of truth; keep Sheets as a *projected view* until GAS scorecard rendering is migrated.
2. Reimplement `overall_score` server-side in Postgres post-cutover (§3.6 + §3.8). Both teams' Sheet formulas weight string values (`Y`/`N`/`NA`) into other sections — reimplementing as a deterministic, versioned pure function against `qa.evaluation_sections` makes the pipeline auditable end-to-end.
3. Provide Postgres tables that Command Center derives runtime state from, so a Railway redeploy never loses a hold timer, a repeated-caller count, or a webhook dedupe entry. CC keeps its persistence layer minimal: an append-only `webhook_events` log is the truth, `call_state.py` rebuilds in-memory state by replay (§4.1).
4. Establish a model-agnostic embeddings layout for cross-lingual A/B-testing across multiple embedding models with different dimensions (§5). **Primary consumer is LandGPT v2 RAG**, not the current pipeline — sized accordingly.
5. Persist `get_call_details()` output completely (§3.7) and persist LandGPT cascade artifacts — the Qwen2-Audio annotated transcript and per-section provider provenance — so any score is reproducible from the row alone (§8).
6. Migrate as much of the analytics layer as possible off Sheets and onto Railway Postgres (§9 — read-path indexes + `agent_stat_points` incremental EWMA/SPC).

### Non-goals (v1)

- **Not** dropping the Sheets writes. Stage 1–4 continues to write to Sheets post-cutover; Sheets becomes a downstream projection.
- **Not** modeling Mass Notifications. Already in production, out of scope.
- **Not** picking the embedding model. §5 specifies a model-agnostic layout; the choice is gated on the §5.8 benchmark, which runs alongside LandGPT v2 not v1.
- **Not** introducing LISTEN/NOTIFY between QA and CC. They share a process (CC §6); in-process bus is simpler. Reserve LISTEN/NOTIFY for the day CC splits into its own Railway service.
- **Not** building an ORM layer. Direct SQL via `asyncpg`/`psycopg`; Pydantic stays at the API boundary.
- **Not** modeling LandGPT-internal state. LandGPT runs its own service (`landing-ai/server/`) with its own audit log; this schema only captures what the QA pipeline observes about the cascade (which models scored what, what the annotated transcript said, which provider scored each section). LandGPT's own DB is its problem.

---

## 2. Why one Postgres, three schemas

| Schema | Owner | Write rate | Read pattern | Retention |
|---|---|---|---|---|
| `qa` | AI-Scoring backend | ~50–200 evaluations/day per team; analytics points 1:1 with finalize | Agent-history + team-stats + lookup + analytics + cascade-artifact reads (indexed) | Forever for evaluations + sections + stat points + formula versions + api_audit_log (§3.10); `qa.score_audit` 6 months hot + permanent archive (§3.9) |
| `command_center` | CC backend | Bursty webhook fan-in for events; ~600 calls/day/team materialized into `calls` | Append + replay for state derivation; SSE snapshot reads; calls-received-vs-scored ratio queries | `webhook_events` 90 days; `calls` permanent; `chiclets` permanent; `frequent_callers_cache` two snapshots |
| `embeddings` | RAG indexer (LandGPT v2, offline batch) | Batch on SOP change or new model registered | KNN at scoring time once LandGPT v2 ships | Versioned per SOP × model |

**Single instance because:** shared `public.teams` table, shared connection pool, shared backup story, shared Railway add-on cost. CC mounts into the AI-Scoring FastAPI process (CC §6).

**Cross-schema FKs minimal but no longer zero (v0.5).** `team_id` columns reference `public.teams.id`. **New in v0.5:** `qa.evaluations.command_center_call_id` is a NULLABLE FK to `command_center.calls.id` (§3.4 + §4.2) — nullable because (a) Phase B backfilled rows pre-date CC, and (b) CC outages must never block QA writes. The QA writer uses INSERT ... ON CONFLICT to upsert a `command_center.calls` row when scoring an evaluation whose call CC never saw live (e.g. happened before CC deploy, or QA scored an old call). The denormalized `dialpad_call_id` TEXT column on `qa.evaluations` remains the durable join key — the FK is the fast path, not the only path.

---

## 3. Schema `qa` — table-level shape

### 3.1 Why the §003 stub doesn't survive

Gaps inherited from `003_qa_scoring_schema.sql` that we fix here (ordered by silent-failure impact per [[feedback_silent_failure_ordering]]):

1. **No `eval_approved_at`.** Post-PR-1, sheet col C is `call_connected`; approval clock lives on a new trailing column. Stub only had auto-`created_at`, conflating draft-insert with approval.
2. **`evaluator_email NOT NULL` + `overall_score NOT NULL`** break the Stage 1 draft write. v0.3 models state explicitly via a `state` enum + tiered CHECK (§3.4) rather than NULLability.
3. **`improvements` vs `opportunities`** naming mismatch with sheet/code/Pydantic. Renamed.
4. **Section-score display strings vs normalized values.** Sheet writes "Yes"/"No"/"Not Applicable"; DB stores canonical (`Y`/`N`/`NA` for binary, `SMALLINT 1..5` for numeric). Translation in the writer.
5. **No per-section provenance** (`ai` vs `manual` vs `auto_value` vs `manual_default_value`). Extended in v0.4 with `ai_provider` for LandGPT Plan B routing (§3.5).
6. **No `teams` table or FK** despite `team_id TEXT` scattered.
7. **Score_Audit vs `audit_log` overlap.** Different consumers, different retention. Split clean in §3.9 / §3.10.
8. **No persisted Dialpad raw metadata.** §003 captured ~6 fields and dropped the rest. v0.3+ carries `dialpad_call_metadata JSONB` end-to-end (§3.7).
9. **No persisted cascade artifacts** (v0.4). With LandGPT in the picture, the annotated transcript from Qwen2-Audio is the integration contract between two models and the single artifact that determines scoring quality — it has to be reproducible from the row (§8.2).

### 3.2 Stage 1–4 → row transitions

In Postgres, a single row exists from Stage 1 onward; each stage transitions `state` and fills more columns. No row copy.

| Stage | Sheet effect (today) | Postgres effect |
|---|---|---|
| 1 — `write_draft_to_fr_ai` | Append/overwrite FR-AI row | `INSERT INTO qa.evaluations (..., state='draft', dialpad_call_metadata=<full payload>, models_used=<cascade JSON>, annotated_transcript=<Qwen2-Audio output OR NULL>)` + bulk `INSERT INTO qa.evaluation_sections` with per-section `ai_provider`. Eager-resolve `dialpad_agent_id` per §3.11. |
| 1.5 — analyst edits | `batch_update` per-section cells | `UPDATE qa.evaluation_sections WHERE evaluation_id = ?`; update KS/Opp on `evaluations`. Flip `source` `ai → ai_reviewed` if any section changed. |
| 2 — `write_to_score_destination` | Append to Scores tab + write `evaluator_email` to FR-AI col D | **Pre-cutover:** `UPDATE qa.evaluations SET evaluator_email=?, state='approved', approved_at=NOW()`. **Post-cutover:** same UPDATE also runs `compute_overall_score()`, writes `overall_score`, stamps `formula_version` (§3.8/§3.12) — Stage 3 collapses into a parity check, then a tombstone. |
| 3 — `read_score_and_writeback` | Poll ARRAYFORMULA → FR-AI col F | **Pre-cutover:** `UPDATE qa.evaluations SET overall_score=?` from Sheet readback. **Post-cutover (formula day-one):** stage retired; `compute_overall_score()` ran inside Stage 2 under the new `formula_version`. No two-week shadow window. |
| 4 — `finalize_to_analyst_history` | Append to Analyst_History | `UPDATE qa.evaluations SET agent_email=?, state='finalized', finalized_at=NOW()` + insert one row into `qa.agent_stat_points` (§9.2). |

**Implication:** the Sheets writes are idempotent projections of the DB row. Stage 1 still writes to Sheets to keep dashboards alive; per-team truth-flip [[feedback_railway_isolation]] makes this safe.

### 3.3 `qa.agents`

Replaces the per-team Mails sheet. One row per agent per team.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `team_id` | TEXT FK → `public.teams.id` | |
| `name` | TEXT | Mails col A |
| `canonical_name` | TEXT | Mails col D — used by GAS scorecard renderer |
| `email` | TEXT | Mails col B |
| `supervisor_email` | TEXT NULL | Mails col C |
| `dialpad_agent_id` | TEXT NULL | Eager-resolved at Stage 1 per §3.11; nightly sweep as backstop |
| `active` | BOOLEAN | |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

UNIQUE `(team_id, LOWER(name))`. Case-insensitive lookup matches `_lookup_agent_email`.

### 3.4 `qa.evaluations`

One row per scored call across its entire lifecycle (draft → approved → finalized). Replaces the FR-AI / Score destination / Analyst_History row triplet.

| Column | Type | Filled at | Notes |
|---|---|---|---|
| `id` | BIGINT IDENTITY PK | 1 | |
| `team_id` | TEXT FK | 1 | |
| `agent_id` | BIGINT FK NULL | 1 (may stay NULL until 4) | |
| `agent_name_raw` | TEXT | 1 | What Dialpad/owner returned |
| `agent_email` | TEXT NULL | 4 | Mails-lookup at finalize |
| `evaluator_email` | TEXT NULL | 2 | NOT NULL once `state='approved'` (CHECK) |
| `state` | TEXT | 1..4 | `draft` → `approved` → `finalized` |
| `source` | TEXT | 1 / 1.5 | `ai`, `manual`, `ai_reviewed` |
| `call_connected_at` | TIMESTAMPTZ NULL | 1 | From `date_connected` |
| `call_started_at` | TIMESTAMPTZ NULL | 1 | From `date_started` |
| `call_ended_at` | TIMESTAMPTZ NULL | 1 | From `date_ended` |
| `call_duration_ms` | INTEGER NULL | 1 | From `total_duration` |
| `call_type` | TEXT NULL | 1 | |
| `language` | TEXT | 1 | `en` / `es` / future |
| `dialpad_call_id` | TEXT NULL | 1 | Master call id; dedupe; durable join key |
| `dialpad_master_call_id` | TEXT NULL | 1 | From `master_call_id` (§3.7) |
| `dialpad_entry_point_call_id` | TEXT NULL | 1 | Drives `build_dialpad_link` |
| `dialpad_link` | TEXT | 1 | Derived; transition-period dedupe (§3.4.1) |
| `command_center_call_id` | BIGINT FK NULL → `command_center.calls.id` | 1 | New in v0.5. UPSERT into CC.calls if missing; NULL on Phase B backfilled rows. Flips `command_center.calls.scored = TRUE` in the same transaction. |
| `mos_score` | NUMERIC(3,2) NULL | 1 | |
| `recording_urls` | JSONB NULL | 1 | Normalized shape `{audio: [...], screen: [...]}` (§3.4.2) |
| `dialpad_call_metadata` | JSONB NULL | 1 | Full `get_call_details` payload — forward-compat catch-all |
| `caller_name`, `caller_phone`, `caller_email` | TEXT NULL | 1 | From `get_call_details` |
| `call_summary` | TEXT NULL | 1 | Audio/text model-generated 2–4 sentences |
| `annotated_transcript` | JSONB NULL | 1 | LandGPT Qwen2-Audio output: per-turn `speaker / text / emotion / paraphrase_intent / pace_marker / interruption`. NULL pre-LandGPT-cutover. Schema in §8.2. |
| `key_strengths` | TEXT NULL | 1 / 1.5 | |
| `opportunities` | TEXT NULL | 1 / 1.5 | (was `improvements` in §003) |
| `needs_coaching` | TEXT NULL | 2 / 4 | New in v1.2. `Y` / `N` / NULL. Manager-set flag; intent only — no FK enforcement to `qa.coachings` (a `pending` coaching row is manager-triggered via the frontend per §3.17). Independent of `tags`: a manager can set `needs_coaching='N'` and still apply tags. |
| `action_plan` | TEXT NULL | 2 / 4 | New in v1.2. The evaluator's initial proposed plan at score time. Snapshotted into `qa.coachings.action_plan` when a coaching row is created (§3.17); the coaching's plan is what's actually agreed in the session and may diverge. |
| `human_review_required_at` | TIMESTAMPTZ NULL | 1 | New in v1.2. Set by the AI scorer when the §3.14 trigger condition fires (`process_adherence` or `call_resolution` ≤ 3 — configurable per team). Pairs with `scoring_status='flagged_human_review'`. |
| `human_review_completed_at` | TIMESTAMPTZ NULL | 1.5 | New in v1.2. Set when a human reviewer fills the `human_review_required` section score and the eval proceeds toward approval. NULL when `human_review_required_at` is NULL (the auto-flow case). |
| `overall_score` | NUMERIC(5,1) NULL | 3 pre-cutover / 2 post-cutover | See §3.4.3 (relaxed CHECK). Pre-cutover rows hold the Sheet ARRAYFORMULA value; post-cutover rows hold `compute_overall_score()` under the row's `formula_version`. |
| `formula_version` | TEXT NULL | 2 post-cutover | FK-by-string → `qa.formula_versions.formula_version` (§3.12). Backfilled historic rows: NULL (the sheet's implicit formula has no version row). Post-cutover rows: the new formula version stamped at score-compute time. |
| `rubric_version` | TEXT NULL | 1 (v1.3+) | FK-by-string → `qa.rubric_versions.rubric_version` (§3.19). Stamped at Stage 1 from the team's currently-active rubric. Pairs with `formula_version` so `compute_overall_score()` can load both archived artifacts from the row alone. Backfilled historic rows: NULL — pre-v1.3 evals reference whatever rubric was in the team JSON file at scoring time (no DB archive). |
| `models_used` | JSONB NOT NULL | 1 | Cascade provenance — see §8.1 |
| `ai_provider_primary` | TEXT NULL | 1 | `gemini` / `landgpt` / `landgpt_with_gemini_fallback` (Plan B). Convenience indexable summary of `models_used`. |
| `estimated_cost_usd` | NUMERIC(8,4) NULL | 1 | Cloud API cost (Gemini); NULL for pure-LandGPT runs. Cost-attribution for the local cascade is amortized hardware, tracked off-row. |
| `csat_score` | NUMERIC(3,1) NULL | future | |
| `sop_used_document_id` | BIGINT FK NULL | future (LandGPT v2 RAG) | `embeddings.sop_documents.id` |
| `sampling_status` | TEXT | 1 | `not_sampled` default |
| `scoring_status` | TEXT | 1 | `complete` / `flagged_long_call` / `errored` / `landgpt_unavailable_routed_to_gemini` (Plan B telemetry) / `flagged_human_review` (v1.2 — see §3.14) |
| `created_at` | TIMESTAMPTZ | 1 | Draft-insert time |
| `approved_at` | TIMESTAMPTZ NULL | 2 | Was sheet `eval_approved_at` |
| `finalized_at` | TIMESTAMPTZ NULL | 4 | |

#### 3.4.1 Dedupe — keep both UNIQUEs through Phase B

Backfilled rows have NULL `dialpad_call_metadata`; legacy `dialpad_call_id` coverage is unproven; `dialpad_link` is what `_find_row_by_dialpad_link` matches today. Ship both as partial UNIQUE indexes; Phase B reconciliation decides which to drop:

```sql
CREATE UNIQUE INDEX uq_eval_team_call_id ON qa.evaluations (team_id, dialpad_call_id)
    WHERE dialpad_call_id IS NOT NULL;
CREATE UNIQUE INDEX uq_eval_team_link    ON qa.evaluations (team_id, dialpad_link)
    WHERE dialpad_link IS NOT NULL;
```

#### 3.4.2 `recording_urls` shape

Fixed internal shape, validated by Pydantic at the writer boundary:

```json
{"audio": ["https://..."], "screen": ["https://..."]}
```

Raw originals (recording_details with ids, durations, start_time) remain in `dialpad_call_metadata` for forensics.

#### 3.4.3 State CHECKs — relaxed for pre-cutover, tightened post-cutover

Pre-cutover, every evaluation passes through an `approved`-with-NULL-`overall_score` window because Stage 3 is a separate call (the ARRAYFORMULA readback). Ship v0.4 with the relaxed shape:

```sql
CHECK (state IN ('draft','approved','finalized'))
CHECK (state != 'finalized' OR overall_score IS NOT NULL)
CHECK (state = 'draft' OR (evaluator_email IS NOT NULL AND approved_at IS NOT NULL))
CHECK (state != 'finalized' OR finalized_at IS NOT NULL)
-- v1.2 additions:
CHECK (needs_coaching IS NULL OR needs_coaching IN ('Y','N'))
CHECK (scoring_status IN ('complete','flagged_long_call','errored',
                          'landgpt_unavailable_routed_to_gemini',
                          'flagged_human_review'))
CHECK (human_review_completed_at IS NULL
       OR human_review_required_at IS NOT NULL)
```

The last CHECK is the pair invariant: a `human_review_completed_at` timestamp cannot exist without a corresponding `human_review_required_at` — it's nonsensical to complete a review that was never started.

**Post-cutover tightening.** Once `compute_overall_score()` is truth (Phase B+), it runs inside the Stage 2 approval transaction (§3.2), and `state='approved' → overall_score IS NOT NULL` becomes naturally true. Reinstate the strict constraint with `ADD CONSTRAINT ... NOT VALID` + `VALIDATE CONSTRAINT`. The constraint history itself documents the cutover.

### 3.5 `qa.evaluation_sections`

Normalized section scores. One row per section per evaluation.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `evaluation_id` | BIGINT FK ON DELETE CASCADE | |
| `section_id` | TEXT | from team config |
| `section_number` | SMALLINT | rubric position (never shifts) |
| `score_type` | TEXT | `numeric` / `binary` / `manual_numeric` / `manual_binary` / `auto_value` |
| `numeric_score` | SMALLINT NULL | 1..5 |
| `binary_value` | TEXT NULL | `Y` / `N` / `NA` |
| `score_source` | TEXT | `ai` / `manual` / `auto_value` / `manual_default` |
| `ai_provider` | TEXT NULL | `gemini` / `landgpt` — populated when `score_source='ai'`. Per-section because Plan B (`landing-ai/LandGPT.md` §Plan B) can route individual sections (e.g. Section 4 Matching the Moment) back to Gemini while others stay on LandGPT. NULL for manual/auto_value sections. |
| `model` | TEXT NULL | Specific model identifier within the provider, e.g. `gemini-2.5-flash` or `gemma-4-27b-q4`. NULL for non-AI sections. |
| `confidence` | TEXT NULL | AI-only |
| `reasoning` | TEXT NULL | AI-only or analyst-typed |

CHECKs enforce "exactly one of `numeric_score`/`binary_value` populated, matching `score_type`" and "`ai_provider` populated iff `score_source='ai'`". UNIQUE `(evaluation_id, section_id)`. INDEX on `(section_id, evaluation_id)` for category trend queries (§9.1).

**v1.2 — `documentation` section deprecation, `human_review_required` introduced.** The `documentation` section is dropped from per-team configs going forward (`config/scoring/<team>.json` removes its entry). Historical `evaluation_sections` rows with `section_id='documentation'` are preserved forever — `section_id` is TEXT, no enum to update, no migration touches them. New evaluations from the deprecation date onward write a `human_review_required` section instead: `score_type='numeric'` (1–5), `na_applicable=true`, NA by default. Its score semantics:

- **1** = "Agent handled this interaction poorly; section weight contributes 0 points." Standard `numeric_score=1` → `1/5 × weight = 0.2 × weight` under the normal formula; the *effective zero* comes from the rubric, not a special rule.
- **5** = "Agent handled this call well; this was a false flag; award the full section weight (~10 points)."
- **NA** = "The auto-flow trigger never fired; redistribute this section's weight via the existing `na_redistribution` rule." This is the common case — the section exists in the rubric but is only filled when §3.14's pipeline trigger flags the eval for human review.

No formula-engine change is required for the section itself — it behaves like any other numeric section with NA support. The trigger logic lives separately in §3.14.

### 3.6 Overall-score reimplementation — new formula day-one, ε as historic-compliance signal (reframed in v1.1)

The migration is a forcing function to make the scoring intent explicit and versioned. v1's "two-week parity shadow" gave that decision a window; v1.1 commits earlier: **a revised formula ships at the start of Phase A.5 as a new `qa.formula_versions` row**, and from that point new scoring uses the new formula. Epsilon's role shrinks from "decides whether to promote Python" to **"signals which historic rows fall outside the new formula's tolerance band."**

Pipeline:

1. Capture each team's formula as JSON per §3.8. **The new formula is authored, not transcribed** — design conversations with QA leadership are part of this step, not deferred to a post-shadow decision.
2. Implement `compute_overall_score(evaluation_id) → NUMERIC(5,1)` in Python — pure function that loads everything it needs from the evaluation row alone. Signature reframed in v1.3: the function now reads `formula_version` AND `rubric_version` from `qa.evaluations`, fetches both archived JSONs from `qa.formula_versions` (§3.12) and `qa.rubric_versions` (§3.19), and applies the formula against the rubric and the row's `qa.evaluation_sections`. Pseudocode:

    ```python
    def compute_overall_score(evaluation_id: int) -> Decimal:
        eval     = SELECT formula_version, rubric_version, ... FROM qa.evaluations WHERE id = $1
        formula  = SELECT formula_json FROM qa.formula_versions WHERE formula_version = $eval.formula_version
        rubric   = SELECT rubric_json  FROM qa.rubric_versions  WHERE rubric_version = $eval.rubric_version
        sections = SELECT * FROM qa.evaluation_sections WHERE evaluation_id = $1
        return apply_formula(formula, rubric, sections)
    ```

   Every score is reproducible forever — the row's `id` is the only input needed; everything else is fetched from immutable archives. The historic-compliance sweep (§3.13) recomputes under any *new* `formula_version` while keeping the row's `rubric_version` constant; the rubric pinning is intentional (you don't re-score under a rubric the analyst didn't actually evaluate under). For sweeps that span a rubric change as well (e.g. measuring the impact of a section being deprecated), the sweep variant `compute_overall_score_with_overrides(evaluation_id, formula_version, rubric_version)` is the escape hatch — same pure function, explicit overrides.
3. **Phase A.5 — formula ship + historic-compliance sweep:**
    - Insert the new formula JSON into `qa.formula_versions` (§3.12 write path fires automatically when the JSON lands in `config/scoring/<team>/overall_formula.json` and FastAPI starts).
    - From this commit forward, every new Stage 2 approval runs `compute_overall_score()` under the new `formula_version` and writes the result into `qa.evaluations.overall_score` directly. The Sheet ARRAYFORMULA continues to fire (dual-write semantics) but its output is no longer the truth for new rows.
    - Run the historic-compliance sweep: for each backfilled historic evaluation, recompute its score under the new formula via `compute_overall_score()`, persist the result into `qa.formula_compliance_sweeps` (§3.13), and flag rows where `|recomputed - original| > ε`.
    - Surface flagged-row distributions per team. Patterns drive formula iteration — a new `qa.formula_versions` row, a new sweep against historic data, flag rate compared to the prior version. The loop terminates when QA leadership accepts the flagged set.
4. Cutover per team (Phase C): unchanged — Stage 3 was already retired in step 3.

**Why immediate vs shadow:** The two-week shadow assumed the existing formula is canonical and Python's job is to match it. The reframe acknowledges the opposite: the cutover is the moment to *fix* the formula, not preserve it. Shadowing for two weeks just delays the decision the team is going to make anyway. Shipping the new formula immediately makes the iteration loop tighter — within hours of a flag-pattern emerging, a revised JSON triggers a new sweep, and the cycle continues until convergence.

**What ε flags surface (and what they do not):**

- Flags are evidence for a formula conversation, not bugs to chase to zero. A flag of "this historic row was 78 under the sheet and 82 under the new formula because the new formula awards partial credit on identity_validation=NA where the sheet awarded zero" is exactly the kind of historic outcome the new formula was authored to change. Acceptance is the right answer for those.
- Flags whose pattern reveals an *unintended* formula behavior (e.g. weight transfer cascades that overshoot when three rules fire together on rare historic rows) are the iteration triggers.
- Flags that cluster on specific agents or specific time windows are NOT scoring concerns — they're an artifact of agent skill or process changes during that period. Surface but don't iterate on those.

**v1.2 — `human_review_required` interacts with formula iteration.** The new section ships as a sectioned numeric with NA-redistribute. Historic rows from before the deprecation date have `section_id='documentation'` rows in `evaluation_sections`; the new formula references `human_review_required`. The §3.13 historic-compliance sweep handles this naturally — `compute_overall_score()` runs against the row's actual sections (whatever ID they have), so a historic row with `documentation` evaluates under whatever weight that section had at its time, and a new row with `human_review_required` evaluates under the new weight. Sweep flags from the documentation→human_review_required transition are *design-intent* flags (the formula is doing what it's supposed to). The runbook (§7.7) categorizes them accordingly.

### 3.7 Dialpad metadata persistence

`dialpad_client.py:362` `get_call_details()` (expanded in v0.2 of this doc) returns every payload field — flat top-level keys for stable consumers, plus `raw`. Writer responsibility:

- **`qa.evaluations`**: Stage 1 writes structured columns AND `dialpad_call_metadata=<entire get_call_details return>`.
- **`command_center.webhook_events`** (§4.1): `raw_payload JSONB` holds the entire Dialpad webhook body.

JSONB columns are queryable for fields not yet promoted to first-class (e.g. `operator_call_id`, `group_id`, `proxy_target`).

### 3.8 `overall_formula.json` — rules pipeline

Per-team formulas live at `config/scoring/<team>/overall_formula.json` (schema-coupled config). Modeled as `sections` + an ordered `rules` pipeline, with NA semantics explicit per rule:

```json
{
  "formula_version": "member_support_v2",
  "scale": { "numeric_min": 1, "numeric_max": 5, "output_max": 100 },
  "sections": [
    { "section_id": "greeting",               "kind": "numeric", "weight": 10 },
    { "section_id": "identity_validation",    "kind": "binary",  "weight": 10,
      "binary_map": { "Y": 1.0, "N": 0.0 } },
    { "section_id": "human_review_required",  "kind": "numeric", "weight": 10,
      "na_default": true }
  ],
  "rules": [
    { "type": "hard_zero",
      "if": { "section": "identity_validation", "equals": "N" } },

    { "type": "na_redistribution",
      "if": { "section": "feature_toggle_x", "equals": "NA" },
      "mode": "proportional", "targets": "remaining" },

    { "type": "na_redistribution",
      "if": { "section": "human_review_required", "equals": "NA" },
      "mode": "proportional", "targets": "remaining" },

    { "type": "weight_transfer",
      "if": { "section": "upsell_offered", "equals": "N" },
      "from": "upsell_quality",
      "to": { "resolution": 0.6, "empathy": 0.4 } }
  ],
  "human_review_triggers": [
    { "section_id": "process_adherence", "max_score_to_trigger": 3 },
    { "section_id": "call_resolution",   "max_score_to_trigger": 3 }
  ]
}
```

Design decisions encoded:

1. **Rules are an ordered pipeline; `hard_zero` short-circuits.** Order-dependence explicit, not emergent — Sales' cascading transfers are expressible without special cases.
2. **NA is a rule, not a magic value.** Two NA behaviors (exclude-and-redistribute vs count-as-zero) become distinct rule types, not an ambiguity rediscovered during the parity gate.
3. **Pydantic-validate at load:** weights sum to 100 pre-rules, every `section_id` referenced exists in team config, `binary_map` present iff `kind=binary`, transfer fractions sum to 1.0. Startup check cross-validates section IDs against team config. Formula bugs become startup failures, not score drift.
4. **Golden fixtures over synthetic tests.** Per team, extract 30–50 real Analyst_History rows *with their sheet-computed scores* into `tests/fixtures/overall_formula/<team>.json`. Phase A.5 then has a deterministic offline core plus the two-week live shadow; fixtures stay as regression armor.
5. **`formula_version` on `qa.evaluations`** (§3.4), stamped at score-compute time. Every score is reproducible: row + sections + versioned formula → same number. **Future revisions of the formula schema must extend, not replace, this shape.** Adding new `rule.type` values, new `sections.kind` values, new `scale` fields is forward-compatible; renaming or repurposing existing keys is not — the `qa.formula_versions` archive (§3.12) is the load-bearing constraint that makes this matter.
6. **`human_review_triggers` is config, not a rule** (v1.2). The array lives at the top level alongside `sections` and `rules`. It does NOT participate in `compute_overall_score()` — score computation runs after the trigger has decided whether the eval flows to auto-finalize or pauses at `state='draft' / scoring_status='flagged_human_review'`. Pydantic-validate at load: every `section_id` exists in `sections` and `max_score_to_trigger` is in `[1, scale.numeric_max]`. Per-team override of *which* sections trigger keeps the human-review queue tunable without a schema change.
7. **`sections[].na_default` is presentational, not a rule** (v1.2). Marks sections (currently only `human_review_required`) where the writer should auto-fill `binary_value='NA'` / `numeric_score=NULL` when the section is created at Stage 1 absent a real score. The pre-existing `na_redistribution` rule still does the weight redistribution; `na_default: true` just tells the writer where to start.

### 3.9 `qa.score_audit` (6 months hot) + `qa.score_audit_archive` (permanent)

Action-level log; distinct from §3.10's request-level audit. Maps 1:1 to today's `Score_Audit` sheet (`append_score_audit_row`). Column-for-column from `_build_score_audit_row`.

**New action value in v0.6: `evaluation_orphaned`.** When a `qa.evaluations` row is deleted (admin-only path; should be rare post-cutover), a `score_audit` row is written with `action='evaluation_orphaned'`, `result_row=<orphaned eval id>`, and `notes` carrying the deletion reason. The corresponding `command_center.calls.evaluation_orphaned` flag flips TRUE in the same transaction (§4.2) — both surfaces fire because dashboards alert on the flag while compliance reads the audit row.

**Retention:** rows older than 180 days are *moved* (not deleted) to `qa.score_audit_archive` by a daily cron. Search endpoint UNIONs both tables.

**Manager-facing access:**

- `GET /api/{team_id}/score_audit/search?from=…&to=…&agent=…`.
- Custom GAS menu (`Score Audit → Lookup history…`) on each team's sheet.
- AI-Scoring frontend `/team/<team>/audit`.

**Auth:** `KEY_ROLE_AUDIT_READER` — distinct from the write-side audit role.

- GET-only; valid solely for `/api/{team_id}/score_audit/search`.
- Bound to a single `team_id` (handler verifies key↔team binding).
- One key per team's sheet; per-team rotation.
- Every search request logs to `qa.api_audit_log`.
- Rate limit ≈ 30 req/min/key.
- **Provisioning UX:** documented in a future ops runbook (resolves v0.3 §9 Q4) — GAS Script Properties is per-script, rotation needs a Script-Properties edit + webhook redeploy. Runbook is operational, not schema-level.

### 3.10 `qa.api_audit_log` (request-level, **permanent retention** — changed in v0.5)

`qa.api_audit_log (id, timestamp, team_id, endpoint, method, status_code, duration_ms, api_key_team, api_key_role, action, model, estimated_cost_usd, call_id, agent_name, error_detail)`.

**Permanent retention (v0.5).** The v0.4 1-year rolling spec is dropped. Reason: this is the only place we have row-level cost attribution per cloud-API call across time, and we need multi-year visibility for decisions like "a new SOTA-but-expensive Anthropic/Gemini model just landed — what would it have cost us last quarter, last year, last two years if we'd been routing the audio-dependent sections to it instead of Qwen2-Audio?" That comparison is dead the moment we age rows out. Storage cost: at ~50 rows/team/day × 2 teams × 365 days × ~500B/row = ~18MB/year, ~180MB across a decade. Negligible.

**Tripwire wiring:** CC's startup replay-duration log line lands here with `endpoint='cc.startup_replay'`, `action='replay'`, and `duration_ms` carrying the measured replay time. Single observability surface; no separate ops table.

### 3.11 `dialpad_agent_id` eager resolution at Stage 1

After the Stage 1 evaluation insert succeeds:

1. If `agent_id` resolved AND `qa.agents.dialpad_agent_id IS NULL` for that row:
   ```sql
   UPDATE qa.agents
      SET dialpad_agent_id = $1, updated_at = now()
    WHERE id = $2 AND dialpad_agent_id IS NULL
   ```
   `IS NULL` guard makes concurrent Stage-1 writes idempotent.
2. Conflict (existing row has a different `dialpad_agent_id`): log loudly, don't overwrite.
3. Failure swallowed and logged; never fails a Stage 1 draft write.

**Nightly reconciliation sweep:** join `qa.agents WHERE dialpad_agent_id IS NULL AND active` against `command_center.dialpad_agents` and finalized evaluations' `dialpad_call_metadata`.

### 3.12 `qa.formula_versions` — archived formula JSONs (new in v0.4)

Resolves v0.3 §9 Q6. The §3.8 startup check validates the formula references existing sections — but what about *old rows* scored under a formula whose section was later deprecated? `formula_version` (§3.4) makes the row reproducible *if* the original formula JSON is recoverable. This table is that recovery path.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `formula_version` | TEXT UNIQUE NOT NULL | Matches `qa.evaluations.formula_version` |
| `team_id` | TEXT FK | |
| `formula_json` | JSONB NOT NULL | Full §3.8 formula at the moment it went live |
| `effective_from` | TIMESTAMPTZ NOT NULL | First evaluation scored under this version |
| `effective_until` | TIMESTAMPTZ NULL | Set when a successor version goes live |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | |

INDEX on `(team_id, effective_from DESC)`.

**Write path:** the formula loader at FastAPI startup hashes the loaded JSON; if no `qa.formula_versions` row matches `(team_id, formula_version, hash)`, it inserts one and marks any prior `effective_until = NOW()`. Reproducing a historical score then becomes: read the eval's `formula_version` → SELECT `formula_json` → run `compute_overall_score()` against archived shape. Old rows are reproducible forever, even after their section IDs are gone from the active team config. **In v1.1, this same write path is how a Phase A.5 formula iteration goes live** — dropping a revised JSON into `config/scoring/<team>/overall_formula.json` and restarting FastAPI is the entire "ship a new formula version" ceremony.

### 3.13 `qa.formula_compliance_sweeps` — historic-compliance signal (new in v1.1)

The persistent record of every historic-compliance sweep run during Phase A.5. One row per (evaluation, formula version swept). Replaces v1's transient `overall_score_parity_check_value` column on `qa.evaluations` — sweeps under successive formula versions are preserved here rather than overwritten on the evaluation row.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `evaluation_id` | BIGINT NOT NULL FK → `qa.evaluations(id)` ON DELETE CASCADE | |
| `swept_formula_version` | TEXT NOT NULL FK → `qa.formula_versions(formula_version)` | The formula version the sweep ran under |
| `recomputed_score` | NUMERIC(5,1) NOT NULL | Result of `compute_overall_score(evaluation_id, swept_formula_version)` |
| `original_score` | NUMERIC(5,1) NOT NULL | Snapshot of `qa.evaluations.overall_score` at sweep time (the Sheet ARRAYFORMULA result for historic rows) |
| `delta` | NUMERIC(6,2) GENERATED ALWAYS AS (recomputed_score - original_score) STORED | Signed delta — sign matters for pattern-spotting |
| `epsilon` | NUMERIC(4,2) NOT NULL | ε pinned per sweep for reproducibility (default 0.05) |
| `flagged` | BOOLEAN NOT NULL | `|delta| > epsilon` evaluated at sweep time, persisted |
| `swept_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | |

UNIQUE `(evaluation_id, swept_formula_version)` — re-running the sweep for the same eval under the same formula version is a no-op (use ON CONFLICT DO NOTHING).

Indexes:

```sql
CREATE INDEX idx_sweeps_formula_flagged
    ON qa.formula_compliance_sweeps (swept_formula_version, flagged);

CREATE INDEX idx_sweeps_eval
    ON qa.formula_compliance_sweeps (evaluation_id);
```

**Why a separate table rather than columns on `qa.evaluations`:** Phase A.5 iterates. Each new formula version triggers a fresh sweep over the same historic rows. Storing sweeps as immutable rows lets us answer "did v2 of the formula reduce the flag rate vs v1?" with a single GROUP BY query. Columns on `qa.evaluations` would either be overwritten on each sweep (losing iteration history) or proliferate (`v1_recomputed_score`, `v2_recomputed_score`, …).

**Write path:** the historic-compliance sweep script, run per team per formula version. Pseudocode:

```python
for eval_row in qa.evaluations:  # backfilled historic rows only
    recomputed = compute_overall_score(eval_row.id, new_formula_version)
    delta = recomputed - eval_row.overall_score
    INSERT INTO qa.formula_compliance_sweeps
        (evaluation_id, swept_formula_version, recomputed_score,
         original_score, epsilon, flagged)
    VALUES
        (eval_row.id, new_formula_version, recomputed,
         eval_row.overall_score, 0.05, abs(delta) > 0.05)
    ON CONFLICT (evaluation_id, swept_formula_version) DO NOTHING
```

Idempotent — restart-safe. Sweeps for *new* (post-Phase A.5) evaluations are not needed because those rows are scored under the new formula at write time.

**Retention:** permanent. Sweeps are a load-bearing audit artifact ("here's why we picked formula v3 over v2"); we keep them as long as the corresponding formula version rows exist.

### 3.14 Pipeline trigger — human-review flagging (new in v1.2)

The Ops VP review (2026-06-24) introduced an explicit "stop the auto-flow if the call looks bad" gate. The pipeline now decides at Stage 1 whether the eval can finalize automatically or must wait for a human reviewer.

**Trigger condition.** After the AI cascade returns per-section scores, the writer evaluates each entry in `human_review_triggers` (§3.8). If any configured section's `numeric_score ≤ max_score_to_trigger`, the eval flags. Default config (member_support, sales): `process_adherence ≤ 3 OR call_resolution ≤ 3`. Per-team config can add or remove sections.

**State transitions at Stage 1:**

| Trigger fires? | `state` | `scoring_status` | `human_review_required_at` | `human_review_required` section |
|---|---|---|---|---|
| No (auto-flow) | `draft` | `complete` | NULL | inserted with `binary_value='NA'` (writer default per `na_default: true`) |
| Yes (paused) | `draft` | `flagged_human_review` | `NOW()` | inserted with `binary_value='NA'` and waits for reviewer to overwrite to a 1–5 numeric score |

The eval stays at `state='draft'` in both cases. Auto-flow proceeds through Stage 2 approval (manager opens, accepts, signs) normally. Paused evals require a human reviewer to:

1. Open the eval in the AI-Scoring frontend (a dedicated "Human Review" queue lists evals where `scoring_status='flagged_human_review'`).
2. Listen to the call, review the AI-generated transcript and per-section scores.
3. Set the `human_review_required` section to a 1–5 numeric score reflecting the reviewer's assessment (1 = full agree with the AI flag; 5 = false flag, agent handled well).
4. Set `human_review_completed_at = NOW()`, transition `scoring_status` back to `'complete'`.
5. Proceed with normal Stage 2 approval.

**The trigger is recomputed at finalize-time, not just Stage 1.** Manager edits to `process_adherence` or `call_resolution` during Stage 1.5 (analyst edits) re-evaluate the trigger. If the analyst raises a triggering score above 3, the flag clears (`scoring_status` flips back to `'complete'`, `human_review_required_at` NULL'd); if the analyst lowers a score into trigger range, the flag fires (`scoring_status='flagged_human_review'`).

**Where the policy lives.** Trigger config is per-team in `overall_formula.json` (§3.8 `human_review_triggers`). Schema records the outcome (the `scoring_status` value + timestamp), not the policy itself.

**Indexes.** New partial index on the queue (`009`):

```sql
CREATE INDEX idx_eval_human_review_queue
    ON qa.evaluations (team_id, human_review_required_at)
    WHERE state = 'draft'
      AND scoring_status = 'flagged_human_review';
```

This is the read pattern for the human-review queue endpoint (`GET /api/{team_id}/human_review_queue`).

### 3.15 `qa.tags` — controlled tag taxonomy (new in v1.2)

The Ops VP review locked in a normalized, category-aware tag taxonomy that ships expansion-ready. The current frontend exposes only the 4 initial human-review-focus tags; future broader categories (compliance, operational, product, outcome) ship as new rows, not new migrations.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `slug` | TEXT UNIQUE NOT NULL | Stable identifier — e.g. `sop`, `soft_skills`, `compliance.profanity_agent` (dotted slugs for nested categories) |
| `category` | TEXT NOT NULL | `human_review_focus` / `compliance` / `soft_skills` / `operational` / `product` / `outcome` (and future). Drives `WHERE category = …` analytics. |
| `label` | TEXT NOT NULL | Display string for the frontend |
| `description` | TEXT NULL | Optional longer description for tooltips and reviewer training |
| `active` | BOOLEAN NOT NULL DEFAULT TRUE | Soft-delete — managers can stop seeing a tag in dropdowns without losing historical references |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | |

UNIQUE on `slug`. INDEX on `(category) WHERE active = TRUE` for dropdown lookups.

**Seed (4 rows, all `category='human_review_focus'`):**

| slug | label | description |
|---|---|---|
| `sop` | SOP | Standard operating procedure / process compliance |
| `soft_skills` | Soft Skills | Communication, empathy, tone, customer experience |
| `hard_skills` | Hard Skills | Product knowledge, tool usage, technical execution |
| `efficiency` | Efficiency | Call structure, hold time use, escalation appropriateness, resource leverage |

**Future expansion** is row-additive: e.g. `INSERT INTO qa.tags (slug, category, label) VALUES ('compliance.pii_exposure', 'compliance', 'PII Exposure')`. No schema migration needed.

**Why `category='human_review_focus'` for the 4 initial tags:** they're distinct from analytical category tags (compliance, operational, etc.) that LandGPT v2 may auto-emit. Future analytics queries `WHERE category != 'human_review_focus'` cleanly exclude the manager-driven coaching axes from AI-driven taxonomies.

### 3.16 `qa.evaluation_tags` — M:N join with provenance (new in v1.2)

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `evaluation_id` | BIGINT FK → `qa.evaluations(id)` ON DELETE CASCADE | |
| `tag_id` | BIGINT FK → `qa.tags(id)` | |
| `source` | TEXT NOT NULL | `manager` / `ai` / `auto` (CHECK). `manager` is the only source today; `ai` lands when LandGPT v2's annotated-transcript path emits tag suggestions (§8.6); `auto` is reserved for future rule-based tagging (e.g. profanity detector flags `compliance.profanity_agent` without human or AI involvement). |
| `created_by` | TEXT NULL | Email — populated when `source='manager'`. NULL for `ai`/`auto`. |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | |

UNIQUE `(evaluation_id, tag_id, source)` — the same tag can land on an eval from multiple sources (manager and AI both say `soft_skills`), and that's information worth preserving. INDEX on `(evaluation_id)` for "what tags does this eval have"; INDEX on `(tag_id, source)` for "which evals got tagged X by AI".

**Tags are independent of `needs_coaching`.** A manager can tag without scheduling coaching (`needs_coaching='N'`), and a coaching can exist without tags. The two flag systems serve different consumers — tags drive analytics + future auto-tagging; `needs_coaching` drives the coaching workflow.

### 3.17 `qa.coachings` — coaching workflow records (new in v1.2)

One row per coaching session. Many-to-many with evaluations via §3.18 — a session can cover multiple evals, and an eval can be revisited across multiple sessions (escalation: TL → Manager → HR).

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `agent_id` | BIGINT NOT NULL FK → `qa.agents(id)` | The agent being coached |
| `team_id` | TEXT NOT NULL FK → `public.teams(id)` | |
| `conducted_by_role` | TEXT NOT NULL CHECK | `team_lead` / `manager` / `hr` / `external` — escalation level is derived from this role |
| `conducted_by_email` | TEXT NULL | Who actually ran it (free text email; not FK'd to a roles table) |
| `status` | TEXT NOT NULL DEFAULT `'pending'` CHECK | `pending` / `completed` / `cancelled` |
| `action_plan` | TEXT NULL | The plan agreed in (or planned for) this session. Distinct from `qa.evaluations.action_plan` (evaluator's initial proposal) — the coaching's plan supersedes once agreed. |
| `action_plan_deadline` | TIMESTAMPTZ NULL | When the agent has committed to demonstrating the plan |
| `coaching_summary` | TEXT NULL | What was actually discussed. Free text. Only required when `status='completed'`. |
| `agent_attitude` | TEXT NULL CHECK | Enum: `receptive` / `engaged` / `neutral` / `defensive` / `dismissive` / `mixed`. NULL acceptable for pending coachings. |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | When the coaching row was created (frontend "Schedule Coaching" action) |
| `scheduled_at` | TIMESTAMPTZ NULL | When the session is set to happen — populated when known, often set at create time |
| `completed_at` | TIMESTAMPTZ NULL | When the manager filled `coaching_summary` and marked completed |
| `completed_by` | TEXT NULL | Email — populated when `status='completed'` |

CHECKs:

```sql
CHECK (conducted_by_role IN ('team_lead', 'manager', 'hr', 'external'))
CHECK (status IN ('pending', 'completed', 'cancelled'))
CHECK (agent_attitude IS NULL OR agent_attitude IN
       ('receptive', 'engaged', 'neutral', 'defensive', 'dismissive', 'mixed'))
CHECK (status <> 'completed'
       OR (coaching_summary IS NOT NULL AND completed_at IS NOT NULL
           AND completed_by IS NOT NULL))
```

The last CHECK is the completion-pair invariant — you can't mark a coaching completed without summary + timestamp + actor.

**No FK enforcement to `qa.evaluations.needs_coaching`.** The flag on the evaluation is intent only (§3.4 + Q4 of the Ops review). A manager who flags `needs_coaching='Y'` may never schedule the coaching; bandwidth realities apply. We track the flag and the eventual coachings separately rather than forcing creation on Stage 4 finalize — better to surface "agents with unhonored coaching flags" as a dashboard signal than to overflow the table with auto-created `pending` rows that get ignored.

**Future Google Calendar integration:** the frontend currently exposes a "Schedule Coaching" action that pre-fills a `pending` row. A later iteration (not in v1.2 scope) connects to Google Calendar's API to create the meeting event and auto-fill `scheduled_at` from the calendar invite.

**Indexes.** Two new partial indexes ship in `009`:

```sql
CREATE INDEX idx_coachings_pending
    ON qa.coachings (team_id, action_plan_deadline)
    WHERE status = 'pending';

CREATE INDEX idx_coachings_agent_status
    ON qa.coachings (agent_id, status, created_at DESC);
```

The first surfaces overdue-deadline reporting; the second supports the per-agent coaching history view.

### 3.18 `qa.coaching_evaluations` — M:N coachings ↔ evaluations (new in v1.2)

Each row links one coaching session to one evaluation. A session covering 3 evals creates 3 rows. An eval revisited across TL → Manager → HR appears in 3 rows across 3 different coaching sessions.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `coaching_id` | BIGINT NOT NULL FK → `qa.coachings(id)` ON DELETE CASCADE | |
| `evaluation_id` | BIGINT NOT NULL FK → `qa.evaluations(id)` | |
| `opportunities_snapshot` | TEXT NULL | Snapshot of `qa.evaluations.opportunities` at the moment this eval was linked to the coaching. Preserves what was discussed even if the eval is edited later. |
| `per_eval_note` | TEXT NULL | Anything specific to this eval within the coaching context — e.g. "second offense for this same SOP miss" |
| `linked_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | |

UNIQUE `(coaching_id, evaluation_id)` — duplicates would just be UX-confusing rather than data-corruption, but the constraint catches double-clicks on "Add to coaching".

**ON DELETE CASCADE on `coaching_id` but NOT on `evaluation_id`.** Deleting a coaching wipes its linked-eval rows (the coaching itself was the parent). Deleting an evaluation should NOT silently strip it from historical coaching records — if the workflow allows eval deletion (admin path only per §3.9), the coaching record keeps the orphaned FK and surfaces it as an `evaluation_orphaned` signal. We rely on FK violation (rather than CASCADE) to alert when this happens; the admin delete path explicitly handles it.

**Why snapshots not FKs to live opportunity text:** standard audit-log pattern (Slack message edits don't rewrite the channel history). If the evaluator edits `qa.evaluations.opportunities` after a coaching is recorded, the coaching's `opportunities_snapshot` preserves what was actually discussed in the session.

**Indexes.** Single index on `(evaluation_id)` to support the per-eval coaching history surface (an evaluation's "this call has been coached on N times" badge).

### 3.19 `qa.rubric_versions` — archived rubric JSONs (new in v1.3)

The rubric versioning peer to `qa.formula_versions` (§3.12). Closes the v1.2 reproducibility gap: until v1.3, `formula_version` could reproduce the scoring math but the **rubric** (section definitions + AI scoring prompt + per-section metadata) lived only in `config/teams/<team>.json` and could be reshaped at any time, breaking score reproducibility for historical evals that referenced section_ids no longer in the active config.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `rubric_version` | TEXT NOT NULL UNIQUE | Matches `qa.evaluations.rubric_version`. Convention: `<team_id>_v<n>` (e.g. `sales_v1`, `member_support_v3`). |
| `team_id` | TEXT NOT NULL FK → `public.teams(id)` | |
| `rubric_json` | JSONB NOT NULL | Full archived rubric — see §3.19.1 shape. Includes `sections` array AND `scoring_prompt` (one row versions the AI scoring contract end-to-end per Q1.a). |
| `effective_from` | TIMESTAMPTZ NOT NULL | First evaluation scored under this version. |
| `effective_until` | TIMESTAMPTZ NULL | Set when a successor version goes live. |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | |

INDEX on `(team_id, effective_from DESC)`.

#### 3.19.1 `rubric_json` shape

```json
{
  "rubric_version": "sales_v1",
  "sections": [
    {
      "id": "greeting",
      "history_id": "greeting",
      "name": "Greeting & Lead Name",
      "section_number": 1,
      "score_type": "yn",
      "audio_dependent": true,
      "rubric_question": "Did the agent ...",
      "na_applicable": true,
      "confidence_cap": null,
      "special_reasoning_instructions": null
    }
  ],
  "scoring_prompt": {
    "system_prompt_template": "...",
    "confidence_levels_note": "...",
    "long_call_focus_sections": ["matching_the_moment", "call_resolution"],
    "sop_sections": ["identity_validation", "process_adherence"]
  }
}
```

Pydantic-validate at load: every `section_id` referenced in `scoring_prompt.long_call_focus_sections` and `scoring_prompt.sop_sections` exists in `sections[].id`. `score_type` enum matches what `qa.evaluation_sections` allows. `audio_dependent` boolean per Plan B routing policy (§8.3a).

#### 3.19.2 Write path — DB-as-source

`qa.rubric_versions` is **the canonical store** for rubrics. The `config/teams/<team>.json` file is a **generated export artifact** — produced on demand by `python -m backend.config.export_team <team>`, never read by application code post-v1.3 cutover.

- **Editor write path** (rubric editor UI, future Wave 2): `POST /api/{team_id}/rubric` with the proposed new rubric_json. Backend validates (see §3.19.3) and on success inserts a new `qa.rubric_versions` row with `rubric_version = '<team_id>_v<n+1>'` and `effective_from = NOW()`, marking the prior version's `effective_until = NOW()` in the same transaction.
- **Programmatic write path** (CI / scripted edits): same endpoint, same validation. No backdoor that skips the FK + Pydantic checks.
- **Migration 010 seed path**: one-time seed at migration apply time, embedding the current JSON-file content as `sales_v1` and `member_support_v1`. From that point forward all edits go through the API.

The startup-hash pattern used by `qa.formula_versions` (§3.12) does **not** apply here — that pattern was file-source-of-truth-with-DB-archive. v1.3 inverts the relationship: DB is the source, file is the projection.

#### 3.19.3 Hard-fail validation — rubric edits that break the active formula are rejected (Q2.a)

The active formula (`qa.formula_versions` WHERE `effective_until IS NULL`) references `section_id`s in its `sections` array and in its `rules` predicates. A rubric edit that drops or renames a section the active formula references MUST be rejected at the API layer, with a 400 response listing the broken references.

The validation check, run before the INSERT into `qa.rubric_versions`:

```python
def validate_rubric_against_active_formula(team_id, new_rubric_json):
    active_formula = SELECT formula_json FROM qa.formula_versions
        WHERE team_id = ? AND effective_until IS NULL
    new_section_ids = {s["id"] for s in new_rubric_json["sections"]}
    formula_section_ids = (
        {s["section_id"] for s in active_formula["sections"]}
        | _section_ids_referenced_in_rules(active_formula["rules"])
    )
    missing = formula_section_ids - new_section_ids
    if missing:
        raise RubricBreaksFormulaError(missing)
```

The operator's choice when the validation fails: either keep the section in the rubric (revert the edit) or ship a new formula first that drops/replaces those references, then re-apply the rubric edit. The active-formula-always-valid invariant is what makes `compute_overall_score()` reliable.

For atomic combined edits (e.g. removing a section AND its weight in one go), the API exposes `POST /api/{team_id}/rubric_and_formula` that creates both rows in a single transaction with cross-validation. Out of v1.3 scope; mentioned here as the forward path.

#### 3.19.4 Reproducing a historical score

The compute function (§3.6) reads BOTH archives:

```sql
SELECT
    e.id,
    f.formula_json,
    r.rubric_json
FROM qa.evaluations e
JOIN qa.formula_versions f ON f.formula_version = e.formula_version
JOIN qa.rubric_versions  r ON r.rubric_version  = e.rubric_version
WHERE e.id = ?
```

The row + both archives + the eval's `evaluation_sections` rows uniquely determine the score — no global state, no current-config dependency. Old rows remain scoreable forever, even after the rubric has been reshaped a dozen times.

#### 3.19.5 Forward-compatibility with `qa.stats_versions` / `qa.gemini_versions`

Per §6 split, operational config (`stats_config`, `gemini_config`) overwrites in place on `public.teams`. If a future audit need promotes one to per-eval reproducibility — e.g. "what Gemini temperature scored this contested eval?" — the migration adds the corresponding `qa.stats_versions` or `qa.gemini_versions` table on the same `qa.rubric_versions` template (slug-style version, JSONB, `effective_from`/`until`), plus a column on `qa.evaluations`. Schema is ready for that pattern without disturbing v1.3.

---

## 4. Schema `command_center` — table-level shape

CC's persistence requirements come from `LandingOpsCommandCenter.md` §5.1, §6, §8. **`webhook_events` is the single mutable persistence layer for call + agent-status state.**

### 4.1 `command_center.webhook_events`

Discriminated by `event_kind` so call events and agent-status events share the same log without sharing a dedupe key.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `received_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | |
| `team_id` | TEXT NOT NULL FK → `public.teams.id` | |
| `event_kind` | TEXT NOT NULL | `call` / `agent_status` (CHECK) |
| `dialpad_call_id` | TEXT NULL | NOT NULL when `event_kind='call'` (CHECK) |
| `dialpad_master_call_id` | TEXT NULL | |
| `dialpad_agent_id` | TEXT NULL | Populated for both kinds when available |
| `state` | TEXT NOT NULL | See §4.1.1 vocabulary |
| `event_timestamp` | TIMESTAMPTZ NOT NULL | From Dialpad payload, NOT `received_at` |
| `raw_payload` | JSONB NOT NULL | Full webhook for replay + forward-compat |
| `processed_at` | TIMESTAMPTZ NULL | |

**Dedupe (two partial UNIQUE indexes):**

```sql
CREATE UNIQUE INDEX uq_webhook_call  ON command_center.webhook_events
    (dialpad_call_id, state, event_timestamp)        WHERE event_kind = 'call';
CREATE UNIQUE INDEX uq_webhook_agent ON command_center.webhook_events
    (dialpad_agent_id, state, event_timestamp)       WHERE event_kind = 'agent_status';
```

**Replay-ordering index:**

```sql
CREATE INDEX idx_webhook_replay ON command_center.webhook_events
    (team_id, event_timestamp, id);
```

#### 4.1.1 State vocabulary

Per CC §10 `monitored_states`:

> `ringing, connected, hold, hangup, recording, call_transcription, recap_summary`

No `unhold`, no `ended`. **A hold cycle ends on the next `connected` or `hangup`.** Matches `test_reconnect_flushes_current_keeps_total`.

**`recap_summary` subscription confirmation (resolves v0.3 §9 Q2):** Landing's Dialpad config already includes the `recap_summary` scope (along with the rest of `monitored_states`); no webhook/websocket has been created yet. Phase 1 deliverable: create the webhook subscription with these scopes before turning on the worker. Once subscribed, recap events land in `webhook_events` and feed Repeated/Frequent chiclet replay correctly.

#### 4.1.2 Replay strategy

On startup, `call_state.py` queries:

```sql
SELECT ... FROM command_center.webhook_events
 WHERE team_id = $1 AND event_timestamp >= $today_in_team_tz
 ORDER BY event_timestamp ASC, id ASC
```

…and runs each event through **the same handler used live**. For ~5k events/team/day, sub-second hydration.

**Perf tripwire (Phase 1 test):** factory-generate a 10k-event synthetic day, time `call_state.rebuild()`. Budget: p95 < 2s at 10k events. Log actual replay duration on every startup as a `qa.api_audit_log` row (§3.10).

**Phone-normalization invariant.** Replay derives `todays_calls` keys via `utils.phone.normalize()`. Replay and live handlers must be *the same function*.

#### 4.1.3 Dormant snapshot DDL (not created in v1)

```sql
-- DDL written, NOT applied. Reserved for tripwire firing.
CREATE TABLE command_center.call_state_snapshots (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id     TEXT NOT NULL REFERENCES public.teams(id),
    snapshot_for_date DATE NOT NULL,
    snapshot    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (team_id, snapshot_for_date)
);
```

#### 4.1.4 Retention

90 days rolling, pruned by daily cron.

### 4.2 `command_center.calls` (reinstated in v0.5)

The universe of every call CC has observed — scored or not. This table was rejected in v0.3/v0.4 as derivable from `webhook_events`. v0.5 reinstates it for three reasons no replay-derivation can satisfy:

1. **Calls-received-vs-calls-scored ratio is a single-query metric.** `SELECT COUNT(*) FILTER (WHERE scored), COUNT(*) FROM command_center.calls WHERE team_id = ? AND connected_at::date BETWEEN ? AND ?` answers a leadership question (what fraction of calls do we actually score?) in milliseconds. Replay-deriving this on demand makes the query expensive and the answer non-cacheable.
2. **`qa.evaluations` needs a fast FK target.** The cross-schema FK from §3.4 points here; replay-derived state cannot serve as an FK target.
3. **`get_call_details()` enrichment is per-call metadata, not per-event.** Webhook payloads are state transitions (ringing/connected/hold/…); `get_call_details()` returns aggregated call-level fields (recording URLs, MOS score, full duration) that don't fit the event log naturally. The right home is a per-call row.

**Webhook_events is still the truth for state derivation.** `calls` is a materialized aggregate written through by the same handler that processes webhook_events (live and replay — same function, per §4.1.2 invariant). On a Railway redeploy, `calls` rows for today are rebuildable from webhook_events; for older dates, the persisted row is already on disk. So `calls` survives redeploys naturally without `webhook_events` having to carry the load alone.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `team_id` | TEXT NOT NULL FK → `public.teams.id` | |
| `dialpad_call_id` | TEXT NOT NULL | UNIQUE per team |
| `dialpad_master_call_id` | TEXT NULL | From `master_call_id` |
| `dialpad_entry_point_call_id` | TEXT NULL | Drives `build_dialpad_link` |
| `started_at` | TIMESTAMPTZ NULL | `date_started` |
| `rang_at` | TIMESTAMPTZ NULL | `date_rang` |
| `connected_at` | TIMESTAMPTZ NULL | `date_connected` — call-time source of truth |
| `ended_at` | TIMESTAMPTZ NULL | `date_ended` |
| `total_duration_ms` | BIGINT NULL | `total_duration` |
| `direction` | TEXT NULL | `inbound` / `outbound` |
| `external_number` | TEXT NULL | |
| `internal_number` | TEXT NULL | |
| `group_id` | TEXT NULL | |
| `dialpad_agent_id` | TEXT NULL | Denormalized for fast queries; §4.6 `dialpad_agents` is the registry |
| `agent_name` | TEXT NULL | |
| `caller_name` | TEXT NULL | |
| `caller_phone_e164` | TEXT NULL | |
| `caller_email` | TEXT NULL | |
| `target_name` | TEXT NULL | |
| `target_type` | TEXT NULL | `call_center` / `user` / etc. |
| `target_phone` | TEXT NULL | |
| `mos_score` | NUMERIC(3,2) NULL | |
| `was_recorded` | BOOLEAN NULL | |
| `is_transferred` | BOOLEAN NULL | |
| `recording_urls` | JSONB NULL | Normalized `{audio: [...], screen: [...]}` shape (§3.4.2) |
| `last_state` | TEXT NULL | Last `monitored_state` observed |
| `last_state_at` | TIMESTAMPTZ NULL | |
| `total_hold_seconds` | INTEGER NOT NULL DEFAULT 0 | Accumulated across hold cycles (derived from webhook_events on each write) |
| `raw_call_details` | JSONB NULL | Full `get_call_details()` payload — forward-compat catch-all |
| `scored` | BOOLEAN NOT NULL DEFAULT FALSE | The load-bearing flag. Flipped TRUE when an evaluation FKs to this row. **Monotonic** — never flips back FALSE even if the evaluation is later deleted. |
| `scored_at` | TIMESTAMPTZ NULL | |
| `evaluation_orphaned` | BOOLEAN NOT NULL DEFAULT FALSE | New in v0.6. Flips TRUE when a previously-attached `qa.evaluations` row is deleted. The pair `(scored=TRUE, evaluation_orphaned=TRUE)` is the precise signal "we have a record this call was scored once, but the evaluation is no longer in the DB" — dashboards alert on it, ops actions it (resurface for re-scoring, or accept and document why). See also §3.9. |
| `evaluation_orphaned_at` | TIMESTAMPTZ NULL | |
| `seen_via` | TEXT NOT NULL | `webhook` (CC saw it live) / `qa_on_demand` (QA scored it without CC seeing it) / `qa_backfill` (Phase B-created stub) |
| `first_seen_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | |
| `last_updated_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | |

**Constraints + indexes:**

```sql
ALTER TABLE command_center.calls
    ADD CONSTRAINT uq_calls_team_call_id UNIQUE (team_id, dialpad_call_id);

-- The ratio query
CREATE INDEX idx_calls_team_scored_connected
    ON command_center.calls (team_id, scored, connected_at);

-- Date-range queries (per-team daily volume, calls in window, etc.)
CREATE INDEX idx_calls_team_connected
    ON command_center.calls (team_id, connected_at DESC)
    WHERE connected_at IS NOT NULL;

-- Agent-level call volume
CREATE INDEX idx_calls_agent
    ON command_center.calls (team_id, dialpad_agent_id)
    WHERE dialpad_agent_id IS NOT NULL;
```

**Write paths (UPSERT on `(team_id, dialpad_call_id)`):**

1. **CC webhook worker** — on every webhook for `event_kind='call'`, UPSERT into `calls` merging state, agent, caller fields. `seen_via='webhook'` on insert; never overwritten.
2. **QA scoring path** — when Stage 1 fires for a call CC hasn't seen (e.g. analyst scored an old/missed call), `write_draft_to_fr_ai` UPSERTs into `calls` with the `get_call_details()` payload and `seen_via='qa_on_demand'`, then writes the `qa.evaluations` row referencing it, then flips `scored=TRUE, scored_at=NOW()` in the same transaction.
3. **Phase B backfill** — inserts stub rows with `seen_via='qa_backfill'` from Analyst_History data so every backfilled `qa.evaluations` row has a valid FK. Stub rows have only what Analyst_History knew (agent_name, dialpad_link → entry_point_call_id, call_connected_at when PR-1 already populated it); the rest is NULL.

**Storage trajectory.** 600 calls/day/team × 2 teams = 1,200/day = ~440k/year. At ~2KB on-disk per row (most fields are nullable text, JSONB compresses well), that's ~900MB/year. Over a decade, ~9GB. Indexes roughly double that — still well under any practical Postgres deployment, including Railway's standard tier.

**Hold-interval derivation, when needed.** `total_hold_seconds` covers 95% of queries. If a future analytics need wants per-hold-cycle granularity ("longest hold by agent over month"), derive from `webhook_events` for `hold` → `connected`/`hangup` pairs on demand. No separate `hold_intervals` table.

**Planned retention policy (post-v1, not implemented at v1):** `raw_call_details` JSONB ages out past 2 years. First-class columns persist forever; only the catch-all JSONB nulls. Triggered by a periodic cron once year-2 storage observations land. Documented here so the eventual implementation doesn't require a schema doc revisit — `raw_call_details` is already declared NULL-able for exactly this reason.

### 4.3 `command_center.chiclets`

Stable identity per surfaced chiclet. Permanent retention.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `team_id` | TEXT FK | |
| `type` | TEXT | `hold` / `repeated` / `frequent` / `qa_outlier` / `sheets_update` / `mass_notif` / `profanity` |
| `tier` | TEXT | `T1` / `T2` / `T3` |
| `status` | TEXT | `active` / `resolved` |
| `border_state` | TEXT | |
| `source_event_id` | BIGINT FK NULL → `webhook_events.id` | NULL for non-webhook origins |
| `caller_phone_e164` | TEXT NULL | |
| `agent_name` | TEXT NULL | |
| `summary` | TEXT | Stable summary text |
| `data` | JSONB NOT NULL DEFAULT '{}' | Per-type live fields written-through on every `chiclet_updated` |
| `created_at` | TIMESTAMPTZ | |
| `resolved_at` | TIMESTAMPTZ NULL | |
| `resolved_by` | TEXT NULL | |

`data` carries hold timer seconds, "3rd call today" counts, concatenated recap text, QA outlier section list. Snapshot endpoint becomes a single indexed read.

### 4.4 `command_center.chiclet_events`

Append-only log of every SSE event emitted (`created` / `updated` / `escalated` / `resolved`). `(id, chiclet_id FK, event_type, payload JSONB, emitted_at)`. 30-day rolling.

### 4.5 `command_center.frequent_callers_cache`

Per-team snapshot of Looker registry (CC §5.6). Two-snapshot retention.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `team_id` | TEXT FK | |
| `caller_phone_e164` | TEXT | |
| `caller_name` | TEXT | |
| `unit` | TEXT NULL | |
| `category` | TEXT NULL | Looker-side category |
| `flag_reason` | TEXT NULL | VP-authored free-text rendered on Frequent chiclet |
| `last_call_at` | TIMESTAMPTZ NULL | |
| `total_calls_30d` | INTEGER NULL | |
| `snapshot_at` | TIMESTAMPTZ | |
| `snapshot_id` | BIGINT | |

INDEX on `(team_id, caller_phone_e164)`.

### 4.6 `command_center.dialpad_agents`

Per-team map of Dialpad's numeric agent IDs to display names.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `team_id` | TEXT FK | |
| `dialpad_agent_id` | TEXT | |
| `display_name` | TEXT | |
| `email` | TEXT NULL | |
| `first_seen_at` | TIMESTAMPTZ | |
| `last_seen_at` | TIMESTAMPTZ | |

UNIQUE `(team_id, dialpad_agent_id)`.

### 4.7 What v0.5 keeps dropped

- `command_center.hold_intervals` — derived from `webhook_events` `hold` → `connected`/`hangup` pairs when per-hold-cycle granularity is needed. The per-call rollup `total_hold_seconds` lives on §4.2's `calls` row.

**Why `calls` is back but `hold_intervals` is not.** `calls` earns its place because (a) it answers the calls-received-vs-scored ratio in one query, (b) it carries cross-schema FK semantics for `qa.evaluations`, and (c) it has per-call enrichment from `get_call_details()` that doesn't fit `webhook_events`'s per-event shape. `hold_intervals` has none of those properties — its only consumer is occasional analytics, and the rollup column on `calls` covers 95% of that. Bring it back the day a real query needs the per-cycle detail and not before.

---

## 5. Schema `embeddings` — model-agnostic, multi-language

A/B-testable across embedding models with different dimensions. Requires pgvector ≥ 0.7.

**Primary consumer:** LandGPT v2 RAG over Notion SOPs (`landing-ai/LandGPT.md` v2 scope). The current Gemini pipeline does not query this schema; LandGPT v1 doesn't either (v1 is the cascade, not RAG). Sizing and benchmark cadence (§5.8) accordingly run on the LandGPT v2 timeline.

### 5.1 Why model-agnostic from day one

Landing operates in Spanish + English; future expansion (France, Germany, …) implies a third or fourth language. The embedder must do **cross-lingual** retrieval well. The LandGPT roadmap adds an **open-source-preferred + CPU-deployable** constraint that closed-source SOTA leaders trade off against.

The chunk-vs-embedding split (§5.5 / §5.6) lets a SOP be ingested once and embedded with two or three models in parallel.

### 5.2 Hard constraint — pgvector dimensionality ceiling

**pgvector's ivfflat and HNSW indexes max out at 2,000 dimensions for the `vector` type.** A `VECTOR(3072)` column is storable but unindexable — every KNN against it is a sequential scan.

**v0.3+ decision:** ≤2,000 ceiling for any indexable embedding. Matryoshka truncation (e.g. Gemini's 3072→1536), not halfvec. Start lean: column set covers `1536` and `1024`, add more as benchmarks demand.

**Candidate set:**

- **Hosted:** Gemini-embed truncated to 1536.
- **Open-source (LandGPT-compatible):** `bge-m3` (1024-d, strong cross-lingual ES/EN, CPU-deployable); Qwen3-Embedding family (purpose-built embedders with MRL).

Qwen3-VL-2B is a vision-language chat model, not an embedder — not a default contender.

### 5.3 `embeddings.embedding_models`

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `name` | TEXT | |
| `version` | TEXT | |
| `dimensions` | INTEGER | ≤ 2000 (CHECK) |
| `modality` | TEXT | `text` / `text+image` / `text+audio` |
| `is_cross_lingual` | BOOLEAN | |
| `provider` | TEXT | `google` / `bge` / `qwen-team` / `local` |
| `is_open_source` | BOOLEAN | |
| `cpu_deployable` | BOOLEAN | Passes §5.8 CPU-only gate |
| `is_current` | BOOLEAN | At most one TRUE per `modality` (partial UNIQUE). Only `cpu_deployable=TRUE` may be `is_current`. |
| `created_at` | TIMESTAMPTZ | |

UNIQUE `(name, version)`.

### 5.4 `embeddings.sop_documents`

`(id, team_id, title, version_tag, language, source_url, published_at, is_current, created_at)`. UNIQUE `(team_id, version_tag)`. One `is_current=TRUE` per `(team_id, language)`.

### 5.5 `embeddings.sop_chunks`

Vector-agnostic chunk row. `(id, document_id, chunk_index, text, token_count, language, modality, heading_path TEXT[], page_number, image_asset_url)`. UNIQUE `(document_id, chunk_index)`.

### 5.6 `embeddings.sop_chunk_embeddings`

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT IDENTITY PK | |
| `chunk_id` | BIGINT FK | |
| `model_id` | BIGINT FK | |
| `embedding_1536` | VECTOR(1536) NULL | |
| `embedding_1024` | VECTOR(1024) NULL | |
| `created_at` | TIMESTAMPTZ | |

UNIQUE `(chunk_id, model_id)`. CHECK exactly one `embedding_*` non-NULL, matching `embedding_models.dimensions`. HNSW indexes per dim column.

### 5.7 `embeddings.embedding_runs`

`(id, document_id FK, model_id FK, chunk_count, total_tokens, estimated_cost_usd, started_at, completed_at, status, error_detail)`.

### 5.8 Benchmark methodology

1. **Labeled set:** 50–100 (query → correct SOP chunk) pairs per team, half ES against EN chunks and vice versa.
2. **Metric:** recall@5 and MRR@5 per language direction.
3. **Deployability gate:** embed a query on a 2-vCPU CPU-only container in <100ms p95. Failures cannot be marked `is_current`.
4. **Decoupling:** retrieval embedder and inference model are different roles with different deployment constraints. The embedder role runs CPU-only; the inference role (LandGPT cascade — Qwen2-Audio + Gemma 4) targets the LandGPT staged hardware path (`landing-ai/LandGPT.md` Hardware staging).

---

## 6. Cross-cutting: `public.teams`

| Column | Type | Added | Notes |
|---|---|---|---|
| `id` | TEXT PK | 004 | `member_support`, `sales` |
| `name` | TEXT | 004 | |
| `timezone` | TEXT | 004 | `America/Mexico_City` — drives CC day-boundary resets |
| `default_language` | TEXT | 004 | `en` / `es` — drives default embedding-model selection |
| `active` | BOOLEAN | 004 | |
| `created_at` | TIMESTAMPTZ | 004 | |
| `company` | TEXT NULL | 010 (v1.3) | "Landing Living LLC" — referenced by AI scoring prompts; was a top-level field in `<team>.json`. |
| `stats_config` | JSONB NULL | 010 (v1.3) | Statistical thresholds (EWMA span, SPC sigma multiplier, outlier z-threshold, etc.). Lifted from `<team>.json`'s `stats` block; lightly versioned via `updated_at` audit rather than full row history (operational tuning, not score-reproducibility). |
| `gemini_config` | JSONB NULL | 010 (v1.3) | Per-team Gemini params (scoring_model, scoring_temperature, max output tokens). Lifted from `<team>.json`'s `gemini` block. |
| `excluded_test_agents` | TEXT[] NOT NULL DEFAULT '{}' | 010 (v1.3) | Agent names excluded from stats + dashboards. Lifted from `<team>.json`'s `excluded_test_agents` list. |
| `sheets_config` | JSONB NULL | 010 (v1.3) | **Legacy.** Tab names, score-destination column mapping, ARRAYFORMULA buffer seconds — the Sheets-cutover-only configuration that lived in `<team>.json`'s `sheets` block. Stays nullable; dropped at Phase D (Sheets retirement). |
| `updated_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | 010 (v1.3) | Tracks operational-config edits. Lighter than `qa.rubric_versions` versioning — `stats_config`/`gemini_config` overwrite in place, with audit through `updated_at`. |

TEXT PK so config-driven references stay readable. Seed migration: `004_seed_teams.sql` ships in the same PR; `010_rubric_versioning.sql` (v1.3) backfills the v1.3 columns from the current JSON files of both teams.

**Versioning split — why `stats_config` / `gemini_config` are NOT in `qa.rubric_versions`.** Rubric edits change what gets evaluated and how a score interprets a section; they must be reproducible per evaluation (every eval stamps `rubric_version`, see §3.4). Operational config (EWMA λ, SPC sigma, model temperature) changes the statistics + AI behavior in ways that are *forward-looking* — historical evals don't need to be re-scored when you tune EWMA, and a model-param change applies to new scoring runs. So those live as overwrite-in-place columns with an `updated_at` clock. If we later decide they DO need per-eval reproducibility (e.g. proving the AI temperature at the time a contested score was generated), we promote those columns into their own versioned tables — `qa.stats_versions`, `qa.gemini_versions` — without disturbing rubric versioning.

---

## 7. Migration sequencing (QA cutover)

### 7.1 Phases

1. **Phase A — Schemas + dual-write.** Create `public.teams` + seed, `qa.*` tables (including §3.12 `formula_versions` and the v1.2 additions §3.14–3.18: `tags` + seed, `evaluation_tags`, `coachings`, `coaching_evaluations`, plus the new columns on `qa.evaluations`). Stage 1–4 writes to Postgres in addition to Sheets; persists full `dialpad_call_metadata` and (if LandGPT pilot is live for the team) `annotated_transcript` + `models_used`. **v1.2:** the writer also evaluates the §3.14 trigger at Stage 1 and sets `scoring_status='flagged_human_review'` when conditions are met. Sheets remains truth. Postgres-write failures swallowed.
2. **Phase A.5 — New formula ship + historic-compliance sweep (§3.6, reframed in v1.1).** Author the revised formula JSON with QA leadership, drop into `config/scoring/<team>/overall_formula.json`, restart FastAPI (§3.12 write path picks it up automatically). From this point all new Stage 2 approvals run `compute_overall_score()` under the new `formula_version`. Then run the historic-compliance sweep script (§3.13) per team — recomputes every backfilled historic row under the new formula, persists results in `qa.formula_compliance_sweeps`, flags rows where `|recomputed - sheet_original| > ε = 0.05`. Surface flag-rate distributions per team to drive formula iteration (§7.7). Loop until QA leadership accepts the flagged set; each iteration is a fresh `formula_version` row + a fresh sweep.
3. **Phase B — Backfill (QA-only — scope clarified in v1).** Read Analyst_History per team, insert into `qa.evaluations` + `qa.evaluation_sections` with `state='finalized'`. **For every backfilled evaluation, also insert (or merge) a stub row into `command_center.calls` with `seen_via='qa_backfill'`** so the `command_center_call_id` FK is populated end-to-end (§4.2 write path 3). Then a **second-stage enrichment script** refetches `get_call_details()` per historical `dialpad_call_id` and merges the full payload into the corresponding `command_center.calls` row. **`agent_stat_points` seeding orders by the sheet's `eval_approved_at` column, NOT `finalized_at`** — `finalized_at` on backfilled rows is the backfill clock, not the historical approval clock, and per-agent series need the true approval order.

   **Scope clarification (v1):** Phase B touches `qa` tables and the `command_center.calls` rows derived from QA-backfilled evaluations only. **There is no historical migration for the rest of `command_center` or for `embeddings`.** CC tables (`webhook_events`, `chiclets`, `chiclet_events`, `frequent_callers_cache`, `dialpad_agents`) start empty at deploy time and fill from live webhooks. The `embeddings` schema starts empty and fills from the LandGPT v2 SOP-ingest job when that ships. Neither schema has historical data sitting somewhere waiting to be moved, so Phase B has nothing to do for either.

   **Dialpad rate-limit constraint:** the enrichment script targets **5 requests per minute** — observed-stable rate against the live Dialpad API, regardless of the 20 req/sec documented limit. At ~50K–200K historical evaluations per team, that's 7–28 days of overnight unsupervised runs per team. Designed for that mode: token-bucket throttle, checkpoint per-call into a `command_center.calls_backfill_state` table (transient — dropped after backfill completes), resume from last checkpoint on script restart, transient failures (5xx, 429) logged for human review without in-loop retry. Hard-fail terminations of the script never lose progress because every successful enrichment is committed individually.
4. **Phase C — Truth flip per team.** Read paths switch to Postgres for everything (agent history, team stats, lookup, Score_Audit search, dashboards). `compute_overall_score()` already runs in Stage 2 (since Phase A.5 ship); Stage 3 already collapsed (since Phase A.5 ship). What Phase C adds is consumer-side: AI-Scoring frontend and GAS scorecard pipelines start reading from Postgres rather than Sheets.
5. **Phase D — Sheets retirement (deferred).**

**Phase ordering note (v1.1):** Phase A.5 requires Phase B's row-insert stage to be complete (sweep needs `qa.evaluations` + `qa.evaluation_sections` rows to recompute against). Phase B's *enrichment* stage (the slow 5 req/min Dialpad calls) can still be running in parallel — the sweep doesn't need `dialpad_call_metadata` or `command_center.calls` enrichment, only sections. So the practical sequence per team is: Phase A (deploy) → Phase B Stage 1 (row backfill, hours) → Phase A.5 (formula ship + sweep, iterates over days) → Phase B Stage 2 (enrichment, runs in background) + Phase C (consumer flip, gated on Phase A.5 acceptance) → Phase D.

### 7.2 Cutover sequencing constraint — LandGPT vs Phase A.5 (reframed in v1.1)

LandGPT's QA-side cutover (`landing-ai/LandGPT.md` weeks 8–10: "side-by-side parity check vs Gemini on 50+ calls" → "Member Support cutover, behind a feature flag") is *also* a comparison exercise. **Do not run LandGPT-vs-Gemini parity and Phase A.5 historic-compliance sweeps in the same window for the same team.** Both produce signals; running them simultaneously makes it impossible to tell whether a flagged row is a formula-change concern or a provider-quality concern. Sequencing per team:

1. Complete Phase A.5 — formula iteration loop terminates, QA leadership signs off on the locked `formula_version`.
2. *Then* start LandGPT-vs-Gemini parity. New evaluations during this window score under the locked formula on whichever provider the parity comparison routes them to; the parity question is "do Gemini and LandGPT produce the same per-section scores under the same formula" — a clean two-variable comparison.
3. Plan B routing (per-section fallback to Gemini) is fine to ship before LandGPT cutover completes; that's a runtime fallback, not a comparison.

**Why this ordering matters more in v1.1:** because the new formula is live as soon as Phase A.5 starts, every new Gemini scoring during the formula-iteration loop is scored under whatever formula version is current at write time. That's a natural consequence of the shipped-day-one approach. Mixing in a provider parity comparison during this window adds a second moving variable — don't.

### 7.3 Dual-write failure semantics

- Sheets write fails: existing behavior (error surfaces).
- Postgres write fails Phase A/B: logged, swallowed.
- Postgres write fails Phase C: hard error.

### 7.4 Backfill correctness checks

Per (team, day):
- Row count parity: Analyst_History vs `qa.evaluations WHERE state='finalized'`.
- Overall-score sum parity (ε = 0.05 for legacy).
- Per-section provenance reconstruction.
- `agent_stat_points` series matches a synthetic full-replay against the same Phase B inputs.
- After enrichment script completes: 0 stub `command_center.calls` rows where `dialpad_call_id` is non-NULL and the corresponding Dialpad call returns 200.

### 7.5 Multi-agent parallelization seams (v1 implementation gate)

The v1 implementation PR set must be designed so independent agents can work in parallel without merge conflicts or blocking dependencies. The schema seams in this doc make that natural; the v1 PR-design pass identifies the actual wave structure. Sketch (not the plan — the plan lands in v1):

**Wave 1 — parallel, no cross-dependencies:**

- `database/migrations/004_create_schemas.sql` — creates `public.teams` + seed, `qa`, `command_center`, `embeddings` namespaces (single PR).
- Three schema-specific migration PRs (`005_qa_tables.sql`, `006_command_center_tables.sql`, `007_embeddings_tables.sql`) authored independently — they touch different schemas and don't conflict.
- Pydantic models PR — JSONB shape validators for `models_used`, `annotated_transcript`, `recording_urls`, `overall_formula`, `webhook_events.raw_payload`. Lives in `qa-automation/AI-Scoring/backend/models/` (consumer side per v0.4 Q3 resolution).
- Test scaffolding PR — pytest fixtures, testcontainers wiring, golden-fixture loader (§11.0).

**Wave 2 — gated on Wave 1, independent within:**

- `sheets_service.py` dual-write integration (writes to Sheets AND Postgres at each Stage).
- `command_center/services/webhook_handler.py` skeleton + `webhook_events` writer.
- `embeddings/indexer.py` skeleton (no live model calls yet — just the chunk + embedding writer interface).
- Backfill scripts (`scripts/backfill_phase_b.py`, `scripts/backfill_calls_enrichment.py`).

**Wave 3 — gated on Wave 2, sequential within team:**

- Phase A.5 parity gate (per team, sequential).
- Phase B backfill + reconciliation (per team, can overlap teams).
- Phase C truth-flip (per team, sequential).

**Cross-cutting (one owner across waves):** migration ordering + DDL review + parity-report monitoring (§7.7). Single human + agent pair, runs alongside every wave.

### 7.6 Migration file numbering + reversibility (locked in v1)

Existing migrations in `database/migrations/`:

```
001_mass_notifications_schema.sql        (in production)
002_add_property_event_columns.sql       (in production)
003_qa_scoring_schema.sql                (stub — never applied — replaced by 005)
```

v1 ships the following ordered migration set. Each has a companion `_down.sql` file for reversibility unless flagged irreversible:

| # | File | Wave | Notes |
|---|---|---|---|
| 004 | `004_create_schemas_and_teams.sql` | 1 | `CREATE SCHEMA qa, command_center, embeddings`; `CREATE EXTENSION IF NOT EXISTS vector`; `public.teams` table + seed. **Reversible** (`DROP SCHEMA … CASCADE`, `DROP EXTENSION`). |
| 005 | `005_command_center_tables.sql` | 1 | All CC tables: `webhook_events`, `calls`, `chiclets`, `chiclet_events`, `frequent_callers_cache`, `dialpad_agents`. Includes partial UNIQUE indexes. Sequenced before 006 so `qa.evaluations.command_center_call_id` FK can reference `command_center.calls.id` in 006. **Reversible.** |
| 006 | `006_qa_tables.sql` | 1 | All `qa` tables: `agents`, `evaluations` (without the v1 parity column — v1.1 reframe), `evaluation_sections`, `formula_versions`, **`formula_compliance_sweeps` (new in v1.1, §3.13)**, `score_audit`, `score_audit_archive`, `api_audit_log`, `agent_stat_points`. Includes the relaxed-pre-cutover CHECK constraints (§3.4.3). **Reversible.** |
| 007 | `007_embeddings_tables.sql` | 1 | `embedding_models`, `sop_documents`, `sop_chunks`, `sop_chunk_embeddings`, `embedding_runs`. **Reversible.** |
| 008 | `008_indexes.sql` | 1 | All analytics indexes (§9.1), KNN HNSW indexes (§5.6), `qa.formula_compliance_sweeps` indexes (§3.13), partial UNIQUE indexes not declared in 005/006. Splitting indexes into a separate file lets them be created `CONCURRENTLY` post-deploy without holding locks during initial table creation. **Reversible.** |
| 009 | `009_vp_review_additions.sql` (new in v1.2) | 1 | Ops VP review (2026-06-24) additions: ALTER `qa.evaluations` (add `needs_coaching`, `action_plan`, `human_review_required_at`, `human_review_completed_at`; extend `scoring_status` CHECK with `'flagged_human_review'`); CREATE `qa.tags` + seed 4 rows; `qa.evaluation_tags`; `qa.coachings`; `qa.coaching_evaluations`; new partial indexes (`idx_eval_human_review_queue`, `idx_coachings_pending`, `idx_coachings_agent_status`, `idx_eval_tags_*`, `idx_tags_category_active`, `idx_coaching_evals_eval`). **Reversible.** |
| 010 | `010_rubric_versioning.sql` (new in v1.3) | 1 | CREATE `qa.rubric_versions`; ALTER `qa.evaluations` to add `rubric_version` FK; ALTER `public.teams` to add `company`, `stats_config`, `gemini_config`, `excluded_test_agents`, `sheets_config`, `updated_at` — the operational config the JSON files currently carry. Seeds `qa.rubric_versions` with `sales_v1` + `member_support_v1` (full embedded `rubric_json` for each) and backfills the new `public.teams` columns from the same JSON content. From this point forward the file becomes a generated export, not source. **Reversible.** |
| 011 | `011_calls_backfill_state.sql` (shifted from 010 in v1.3) | 2 | Transient `command_center.calls_backfill_state` table used by §7.1 enrichment script. **Reversible.** Dropped explicitly by 013. |
| 012 | (no migration — Phase A.5 is application-code: formula JSON drop + sweep script run, both reversible via re-running) | — | |
| 013 | `013_drop_calls_backfill_state.sql` (shifted from 012 in v1.3) | 3 (post-backfill) | Drops the transient backfill state table. **Reversible** by re-applying 011. |
| 014 | `014_qa_evaluations_strict_state_check.sql` (shifted from 013 in v1.3) | 3 (Phase C, per team) | `ADD CONSTRAINT … NOT VALID` + `VALIDATE CONSTRAINT` to tighten the pre-cutover relaxed CHECK to its strict post-cutover form (§3.4.3). **Reversible** via `DROP CONSTRAINT`. |

**v1.2 → v1.3 migration plan changes:**

- **Added:** migration 010 (`010_rubric_versioning.sql`). DB-as-source for rubric configuration. Lives in Wave 1 so the rubric archive is in place before any Wave-2 evaluation writes (which now stamp `rubric_version` at Stage 1).
- **Renumbered (no semantic change):** old 010 → 011, old 011 placeholder → 012, old 012 → 013, old 013 → 014. Each migration's role is unchanged; only its file number shifts.
- **Rationale for separate migration vs. amending 006/009:** same as v1.2's reasoning — 006/009 are already on main and represent past design conversations. Keeping them stable and adding 010 maps the v1.3 design (2026-06-29 rubric versioning discussion) to its own PR for clean audit history.

**v1.1 → v1.2 migration plan changes:**

- **Added:** migration 009 (`009_vp_review_additions.sql`). Single coherent "VP review additions" Wave-1 migration. Lives in Wave 1 (alongside 004–008) so the schema is consistent before any Wave-2 dual-write or backfill scripts can write to it.
- **Renumbered (no semantic change):** old 009 → 010, old 010 placeholder → 011, old 011 → 012, old 012 → 013. The shift is purely so 009 maps to "VP review additions" and Wave 2/3 numbering stays contiguous.
- **Rationale for separate migration vs. amending 006:** 006 is already merged to main and represents the "Sheets→Postgres baseline" pre-VP-review. Keeping 006 stable and adding 009 maps the 2026-06-24 design conversation to a single PR — better audit trail for the recruit (per `[[project_recruit_onboarding]]`) and future schema archaeologists.

**v1 → v1.1 migration plan changes:**

- **Dropped:** migration 013 (`013_drop_parity_check_value.sql`). The `overall_score_parity_check_value` column never exists in v1.1 — replaced by the persistent `qa.formula_compliance_sweeps` table (folded into 006). Nothing to drop later.
- **Folded into 006:** `qa.formula_compliance_sweeps` table DDL. No separate migration; it's part of the qa-tables wave-1 PR.
- **Reduced irreversibility surface:** v1's migration set had one irreversible migration (013). v1.1+ has zero. Every migration in the v1.2 set is reversible. The compliance sweep history is preserved through the entire migration sequence — there is no "drop the audit artifact" moment.

**Ordering invariants** the migration runner must enforce:

- 004 → {005, 006, 007} → 008 → 009 → 010. Within Wave 1, 005 before 006 (cross-schema FK), 006 and 007 independent. 009 must follow 006 (ALTERs the existing `qa.evaluations` table and FKs to `qa.agents`). 010 must follow 006 + 009 — ALTERs `qa.evaluations` to add `rubric_version` and depends on the v1.2 columns from 009 being present (for the seed's INSERT shape verification).
- 011 before backfill script execution; 013 only after backfill confirmed complete (all `qa.evaluations` backfilled, all `command_center.calls` enriched).
- 014 per team, only after that team's Phase A.5 outcome is decided (§7.7) and Phase C truth-flip is complete.

**Each migration file is its own PR** so multi-agent ownership maps to file ownership cleanly. Cross-file dependencies are explicit in the migration metadata header.

### 7.7 Phase A.5 historic-compliance-sweep runbook (reframed in v1.1)

The runbook for the formula-iteration loop per team. There is no fixed two-week window — the loop runs until QA leadership accepts the flagged-row set under the current formula version, which may be one sweep or several.

**Sweep report (per formula version, generated when sweep completes):**

```
HISTORIC COMPLIANCE SWEEP — team=member_support — formula_version=member_support_v2

  Historic rows swept:        12,408
  Within ε=0.05:              11,287  (90.97%)
  Flagged (|delta| > 0.05):    1,121  (9.03%)

  Flag breakdown by signed delta:
    new_formula > sheet by  >0.05:   742  (66.2% of flags)
    new_formula < sheet by  >0.05:   379  (33.8% of flags)

  Flag concentration by section (top 3):
    section 4 (matching_the_moment):   428 flags  — new formula awards
                                                   partial credit on NA
                                                   where sheet awarded 0
                                                   (DESIGN INTENT)
    section 9 (identity_validation):    61 flags  — new formula hard-zero
                                                   triggers on edge case
                                                   sheet missed
                                                   (REVIEW — possible bug?)
    section 7 (call_resolution):        38 flags  — rounding only
                                                   (ACCEPT)

  Comparison vs prior version (member_support_v1):
    v1 flag count:  2,103  (16.9%)
    v2 flag count:  1,121  ( 9.0%)
    Net delta:    -982 flagged rows — iteration converged

  Recommended next action:
    - 'section 4' flag pattern is design intent → ACCEPT and document
    - 'section 9' flags need investigation → pull 5 sample evals for QA
      leadership review before locking v2
    - All other flags are acceptable noise
```

**Decision criteria per sweep:**

1. **Flag patterns are all design-intent (formula change is doing what it's supposed to) or rounding noise:** lock this `formula_version`. Phase C unblocked.
2. **Flag patterns reveal an unintended formula behavior on a specific section or rule:** revise the JSON, drop into config, restart FastAPI (new `qa.formula_versions` row writes via §3.12), re-run the sweep, compare v_n+1 flag rate to v_n. Iterate.
3. **Flag rate is acceptable to QA leadership without further iteration:** lock. Iteration count is not a metric; QA-leadership acceptance is.

**Sample-row pull:** the runbook calls for pulling 5 sample historic rows per flag pattern for human review. SQL pattern:

```sql
SELECT e.id, e.agent_name_raw, e.approved_at,
       s.original_score, s.recomputed_score, s.delta,
       json_agg(json_build_object(
         'section', sec.section_id,
         'numeric', sec.numeric_score,
         'binary', sec.binary_value,
         'source', sec.score_source
       )) AS sections
  FROM qa.formula_compliance_sweeps s
  JOIN qa.evaluations e          ON e.id = s.evaluation_id
  JOIN qa.evaluation_sections sec ON sec.evaluation_id = e.id
 WHERE s.swept_formula_version = 'member_support_v2'
   AND s.flagged = TRUE
   AND EXISTS (SELECT 1 FROM qa.evaluation_sections sx
                WHERE sx.evaluation_id = e.id AND sx.section_id = 'section_9')
 GROUP BY e.id, s.original_score, s.recomputed_score, s.delta
 ORDER BY RANDOM()
 LIMIT 5;
```

**Owner:** the §7.5 cross-cutting human-plus-agent pair. Sweep report lands in the AI-Scoring team's Slack channel after each formula iteration. Iteration cadence is whatever the team needs — same-day if the patterns are clear, week-long if QA leadership wants spread-out review meetings.

**Stopping condition:** QA leadership signs off on the locked `formula_version` for the team. From that point, Phase C is unblocked for that team and no further sweeps run unless a future revision needs to be justified against this baseline.

---

## 8. LandGPT integration shape (new in v0.4)

`landing-ai/LandGPT.md` introduces a two-model cascade (Qwen2-Audio → annotated transcript → Gemma 4 → scorecard) that replaces Gemini for the QA scoring pipeline. The schema needs to: (a) record which model(s) scored each evaluation, (b) persist the annotated transcript as the integration artifact between the two models, and (c) keep Plan B (per-section fallback to Gemini with redaction) representable. The schema does *not* model LandGPT's internal state — LandGPT's own server (`landing-ai/server/`) owns that.

### 8.1 `models_used` JSONB on `qa.evaluations`

Cascade provenance per evaluation. Shape:

```json
{
  "audio":     { "provider": "landgpt", "model": "qwen2-audio-v1",     "version": "..." },
  "text":      { "provider": "landgpt", "model": "gemma-4-27b-q4",     "version": "..." },
  "fallback":  { "provider": "gemini",  "sections": ["section_4"],     "reason": "qwen_audio_low_confidence" }
}
```

- `audio` / `text` describe the primary cascade. Pre-LandGPT runs have `audio` NULL or omitted and `text` set to `gemini-2.5-flash` (matches today's pipeline).
- `fallback`, when present, names Plan B activations: which sections were rerouted to Gemini and why. Empty/missing → no Plan B activation.

**Indexed summary column:** `ai_provider_primary TEXT` (§3.4) takes one of `gemini`, `landgpt`, `landgpt_with_gemini_fallback`. Convenience for dashboards and cost queries — the JSONB carries the full detail.

**Cost attribution:** Gemini calls populate `qa.evaluations.estimated_cost_usd`. LandGPT calls leave it NULL — local compute cost is amortized hardware, tracked off-row in the LandGPT ops layer per `landing-ai/LandGPT.md` Hardware staging.

### 8.2 `annotated_transcript` JSONB on `qa.evaluations`

The Qwen2-Audio output is the **single artifact that determines scoring quality** (`landing-ai/LandGPT.md` Architecture). Persisting it is non-optional:

```json
{
  "schema_version": "qwen2_audio_v1",
  "language_detected": "es",
  "turns": [
    {
      "speaker": "agent",
      "text": "Buenos días, mi nombre es...",
      "emotion": "neutral_friendly",
      "paraphrase_intent": "greeting + introduction",
      "pace_marker": "normal",
      "interruption": false,
      "start_ms": 1200,
      "end_ms": 4800
    },
    { "speaker": "caller", "text": "...", "emotion": "frustrated", "...": "..." }
  ]
}
```

- Schema lives at `landing-ai/server/providers/qwen_audio_client.py` (referenced from the LandGPT side) and is validated by Pydantic at the writer boundary.
- `schema_version` lets future Qwen variants ship breaking field changes without losing reproducibility of old rows — same pattern as `formula_version` for overall_score.
- **NULL for Gemini-scored evaluations** (pre-LandGPT or Plan-B-only routes). Always populated when `models_used.audio.provider = 'landgpt'`.
- The transcript is **inspectable**: managers disputing a Section 4 score can be shown the annotated transcript, which is the LandGPT design's key auditability claim over direct audio-LLM scoring.

### 8.3 Per-section provider provenance — `evaluation_sections.ai_provider`

§3.5's `ai_provider TEXT` column closes the Plan B loop. When Section 4 routes to Gemini (with redaction) while Sections 1–3 + 5+ stay on LandGPT, each section's row carries its own `ai_provider`. The §8.1 `fallback.sections` array on `models_used` and the per-row `evaluation_sections.ai_provider` must agree — Pydantic-validated at the Stage 1 writer.

### 8.3a Plan B trigger — audio-dependent sections + uniform LOW confidence (new in v0.5)

The LandGPT cascade returns structured per-section JSON: `{score, reasoning, confidence}` where `confidence ∈ {LOW, MED, HIGH}`. The Plan B routing policy (resolves v0.4 §10 Q2):

1. Each team's config declares an **`audio_dependent_sections: [section_id, ...]`** list — sections whose scoring quality depends materially on audio fidelity (e.g. Member Support's `matching_the_moment`, Sales' equivalent).
2. After the Qwen2-Audio + Gemma 4 cascade returns its per-section scores, the writer checks: **if ALL items in `audio_dependent_sections` came back with `confidence='LOW'`, reroute those sections to Gemini (with redaction) for the current call.** Sections not in the list stay on LandGPT regardless.
3. `models_used.fallback.sections` records exactly the rerouted set; `models_used.fallback.reason` carries `'audio_dependent_sections_uniform_low_confidence'`.
4. Per-section `evaluation_sections.ai_provider` is `landgpt` for the kept sections and `gemini` for the rerouted ones.
5. If `audio_dependent_sections` returns *mixed* confidences (one LOW, one HIGH), no fallback — the cascade's partial signal is usable.

**Why all-of, not any-of.** A single low-confidence audio-dependent section is a noisy datapoint, not a model failure. Uniform LOW across the audio-dependent set is the signal that the audio itself isn't yielding useful structure to Qwen — which is the failure mode Plan B exists to handle.

**Where the policy lives.** Decision logic is in `landing-ai/server/` (the consumer of `audio_dependent_sections` config); the SQL schema just records the outcome. Schema is ready for the policy to evolve (e.g. ≥80% LOW threshold instead of strict all-of) — `fallback.reason` is free-text.

### 8.4 PII surface and retention

LandGPT exists to keep PII inside Landing's network. The schema's PII surface grows as the cascade ships:

- `qa.evaluations.annotated_transcript` carries verbatim caller speech (cards, addresses, names, DOB).
- `qa.evaluations.dialpad_call_metadata` carries `caller_phone`, `caller_name`, `recording_urls`.
- `command_center.webhook_events.raw_payload` mirrors the same.

**Schema does not store audio bytes.** Today's Gemini pipeline uploads the audio file itself to the cloud alongside the transcript; LandGPT will do that with Qwen2-Audio inside Landing's network. **Neither pipeline stores audio in Postgres.** The schema stores only (a) Dialpad URLs as pointers (`recording_urls` JSONB on `qa.evaluations` and `command_center.calls`), and (b) audio-derived structured artifacts (the `annotated_transcript` JSONB after Qwen2-Audio has interpreted the audio). The PII surface in Postgres is therefore text + metadata + pointers — audio biometrics never land here. This is by design and worth not eroding in v1+ as new fields are added.

Implications and access policy (v0.5 — resolves v0.4 §10 Q6):

- Railway Postgres encryption-at-rest covers these by default; no column-level encryption at this stage.
- **`KEY_ROLE_PRIVILEGED` (existing role) is the gate to reading verbatim `annotated_transcript` and unredacted `dialpad_call_metadata` JSONB.** Auditors, HR-grade workflows, and the Landing-side QA leadership use this role.
- **Per-team keys (`KEY_ROLE_<team_id>`) get a redacted view.** Endpoints surfacing `annotated_transcript` content to per-team users return a `redacted_annotated_transcript` projection — same shape, sensitive turns replaced with `[REDACTED:card]`/`[REDACTED:address]`/etc. via the same redaction layer used in front of Gemini in Plan B (`landing-ai/LandGPT.md`). The redaction is applied at the API boundary, not stored separately — single source of truth, two presentations.
- **`KEY_ROLE_AUDIT_READER` (§3.9) is intermediate:** it can search audit metadata (timestamps, actors, actions) but cannot read transcript bodies. Three-tier separation aligns the existing role hierarchy with the new PII surface.
- For Plan B activations, the per-section Gemini path must only receive redacted text. The schema doesn't model redaction; the writer does. Confirmed at writer-implementation time.

The TOAST-compress decision for `annotated_transcript` (v0.4 §10 Q5) is deferred to v0.6 as a future-design-requirements line item, not in scope for v0.5 DDL. Postgres TOAST is on by default for JSONB; this is about whether to push very-large transcripts to object storage with a pointer — a question that needs production size distributions first.

### 8.5 Scoring_status telemetry for LandGPT availability

`qa.evaluations.scoring_status` gets a new value: `landgpt_unavailable_routed_to_gemini`. Surfaces "the cascade was down, full-call Gemini fallback fired" in dashboards distinctly from "Plan B section-level routing" (which is `models_used.fallback`, not a status). Two failure modes, two ways to query them.

**v1.2 addition:** `'flagged_human_review'` joins the enum. Surfaces "the §3.14 trigger fired, this eval is awaiting a human reviewer" — distinct from the LandGPT-availability path and routed to a different frontend queue. Three failure / pause modes, three ways to query.

### 8.6 Annotated transcript as future SOT for tags (new in v1.2)

The Ops VP review noted that LandGPT's annotated transcript should eventually become the source of truth for `qa.evaluation_tags`. The v1.2 schema already supports this — the `source` column on `qa.evaluation_tags` admits `'ai'` from day one, so no future migration is needed. The path:

1. **Today (v1.2):** managers manually tag from the 4 human-review-focus tags via the frontend pre-filled scorecard. Every row in `qa.evaluation_tags` has `source='manager'`.
2. **LandGPT v2 ships:** the Qwen2-Audio + Gemma 4 cascade includes a tag-suggestion pass that reads the annotated transcript and emits suggested tags (likely from broader categories like `compliance.profanity_agent`, `soft_skills.empathy_miss`). These land as `qa.evaluation_tags` rows with `source='ai'`, written by the same Stage 1 writer.
3. **Future SOT shift:** if AI tagging proves reliable, managers' UI moves to a "review AI tags" surface rather than "pick from scratch." Schema doesn't change — manager confirmations could be modeled as `source='manager'` rows that mirror the AI's selections (preserves provenance), or the frontend just hides AI-tagged rows from manager edit unless they explicitly override.

**What the schema commits to in v1.2:**
- `source` is permanent provenance — even if a manager later removes an AI-suggested tag, the AI row is kept (or soft-deleted via an `active` flag on a future migration if needed). Provenance for "what did LandGPT think at the time" is auditable forever.
- The `(evaluation_id, tag_id, source)` UNIQUE means a tag from `manager` and from `ai` coexist as distinct rows — agreement between the two is information ("the AI saw this and the manager confirmed").
- No CHECK constraint on `category` cross-source — the AI may emit broader-category tags (`compliance.*`) on the same eval where the manager picked human-review-focus tags. Both are legitimate.

The schema is forward-compatible with the auto-tagging path without committing to it in v1.2. LandGPT v2's tag-suggestion logic and its prompt design are LandGPT-side concerns (`landing-ai/LandGPT.md` v2 scope).

---

## 9. Analytics layer on Railway

### 9.1 Read-path indexes

```sql
CREATE INDEX idx_eval_team_time   ON qa.evaluations (team_id, finalized_at)
    WHERE state = 'finalized';
CREATE INDEX idx_eval_agent_time  ON qa.evaluations (agent_id, finalized_at)
    WHERE state = 'finalized' AND agent_id IS NOT NULL;
CREATE INDEX idx_sections_trend   ON qa.evaluation_sections (section_id, evaluation_id);
```

### 9.2 `qa.agent_stat_points` — incremental EWMA + SPC

```sql
CREATE TABLE qa.agent_stat_points (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id         TEXT NOT NULL REFERENCES public.teams(id),
    agent_id        BIGINT NOT NULL REFERENCES qa.agents(id),
    evaluation_id   BIGINT NOT NULL UNIQUE REFERENCES qa.evaluations(id),
    score           NUMERIC(5,1) NOT NULL,
    ewma            NUMERIC(6,2) NOT NULL,
    ewma_lambda     NUMERIC(4,3) NOT NULL,
    spc_mean        NUMERIC(6,2),
    spc_sigma       NUMERIC(6,3),
    spc_flags       TEXT[],
    coverage_regime TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_stat_points_agent ON qa.agent_stat_points (agent_id, id);
```

`team_stats.py` keeps owning the math; what changes is it computes **one point** on each finalize. Three consumers:

- CC Zone D sparklines — one indexed read.
- QA outlier chiclets — `spc_flags` non-empty = Tier 2 trigger.
- PDF assessment pipeline — reproducible series with λ pinned per point.

**Backfill:** Phase B replays finalized evaluations in **`eval_approved_at` order** (the sheet column, not the backfill clock — §7.1).

### 9.3 SQL vs Python division

- **SQL owns:** filtering, joining, windowed aggregates, storage of computed series.
- **Python owns:** sequential/recursive statistics (EWMA, modified Z-scores, SPC rules).

Sheets analytics pulls disappear at Phase C; leadership-facing Sheets surfaces become projections of these tables.

### 9.4 LISTEN/NOTIFY — non-decision

CC and AI-Scoring share a process. In-process bus. LISTEN/NOTIFY reserved for a future CC-as-separate-service split.

---

## 10. Question history — closed through v1.3

**v1.2 → v1.3 reframe (this revision — rubric versioning 2026-06-29):**

- **Rubric becomes versioned + DB-as-source.** `qa.rubric_versions` (§3.19) is now the canonical store for section definitions + AI scoring prompt. `config/teams/<team>.json` becomes a generated export — never read by application code post-cutover. The Wave-2 PR set will refactor `team_config.py` to query from DB instead of file.
- **Every evaluation stamps both `formula_version` AND `rubric_version`** (§3.4). `compute_overall_score()` signature simplifies to `(evaluation_id)` — both archives are joined from the row alone (§3.6 pseudocode). Old rows remain scoreable forever, regardless of how many times the rubric is later reshaped.
- **Rubric ↔ formula hard-fail validation** (§3.19.3). A rubric edit that drops or renames a section the active formula references is rejected at the API layer with a 400 listing the broken references. The operator must update the formula first OR use the atomic combined-edit API (`POST /rubric_and_formula`, out of v1.3 scope, mentioned as forward path).
- **`scoring_prompt` lives inside `rubric_json`** (Q1.a). One row versions the AI scoring contract end-to-end. The prompt's `long_call_focus_sections` / `sop_sections` arrays reference section_ids and must be inseparable from their sections.
- **Operational config (stats, gemini, sheets) moves to `public.teams` columns** (§6). Lightly versioned (overwrite-in-place with `updated_at`) rather than full row history — historical scores don't depend on EWMA λ or Gemini temperature at scoring time. Forward-compatible with `qa.stats_versions` / `qa.gemini_versions` promotion if a future audit need demands per-eval reproducibility.
- **Migration plan renumbered:** new 010 = "rubric versioning" (Wave 1, seeds both teams' current rubrics). Old 010/012/013 → 011/013/014. All reversible.
- **Question history closes v1.3 Q1–Q2:** Q1 → `scoring_prompt` inside `rubric_json` (one row, one archive); Q2 → hard-fail on rubric-breaks-formula at the API layer.

**v1.1 → v1.2 reframe (Ops VP review 2026-06-24):**

- **Coaching workflow becomes first-class.** Two new tables (`qa.coachings`, `qa.coaching_evaluations`) capture the manager-driven escalation flow (TL → Manager → HR can revisit the same eval; one session can cover multiple evals). The `coaching_summary` + `agent_attitude` + `action_plan_deadline` triple makes the post-coaching follow-up auditable.
- **Tags taxonomy normalized.** `qa.tags` registry + `qa.evaluation_tags` M:N join with `source` provenance. Seeds 4 human-review-focus tags (`sop`, `soft_skills`, `hard_skills`, `efficiency`); future analytical categories ship as rows, not migrations. Forward-compatible with LandGPT v2 auto-tagging (§8.6).
- **Pipeline trigger for human review.** New `scoring_status='flagged_human_review'` + `human_review_required_at`/`human_review_completed_at` timestamps + per-team `human_review_triggers` config in `overall_formula.json`. The §3.14 trigger evaluates at Stage 1 and Stage 1.5 (re-evaluates on analyst edits). Documentation section is deprecated cleanly (no rewrite of historical rows); `human_review_required` numeric section replaces it.
- **`needs_coaching` + `action_plan` columns on `qa.evaluations`.** Denormalized intent flag + initial proposed plan, both nullable, no FK enforcement to `qa.coachings`. A `pending` coaching row is manager-triggered later (Q7 — magic numbers matter; don't overflow with un-actioned auto-creates).
- **Migration plan renumbered.** New 009 = "VP review additions" (Wave 1). Old 009/011/012 shift to 010/012/013. Each migration in the v1.2 set is reversible.
- **Question history closes v1.2 Q1–Q12:** Q1 → M:N coachings ↔ evaluations (§3.17 / §3.18); Q2 → opportunities stay TEXT, snapshot at coaching link time; Q3 → human_review_required is a normal numeric section, 1=0pts / 5=full weight; Q4 → `needs_coaching` is a flag with no FK enforcement, tags independent; Q5b → 4th tag is `efficiency`; Q6 → 6-value attitude enum incl. `mixed`; Q7 → manager-triggered, never auto-created; Q8 → `conducted_by_role` enum incl. `external`; Q9 → action_plan + coaching_summary on `qa.coachings`; Q10 → new migration 009; Q11 → clean break on documentation; Q12 → full v1.2 covering §3.6, §3.8, §8.6.

**v1 → v1.1 reframe:**

- **Phase A.5 changed from "two-week shadow + decide" to "ship new formula day-one + iterate via historic-compliance sweep."** Drivers in §3.6 spell out why: the cutover is the right moment to fix the formula, not preserve it; shadowing delays the decision the team is going to make anyway. Schema changes: drop `overall_score_parity_check_value` column; add `qa.formula_compliance_sweeps` table (§3.13). Migration set changes: drop migration 013 (no parity column to drop); fold the sweeps table into migration 006.
- **Sweep is iteration-aware.** Each formula version produces its own immutable sweep row per evaluation, so v2 vs v1 flag-count comparison is a single GROUP BY. v1's transient column would have been overwritten.
- **Phase ordering clarified:** Phase A.5 sweep gates on Phase B row-insert completion (it needs `evaluation_sections` to recompute against), but does NOT gate on Phase B enrichment (which can run in background — it only enriches `command_center.calls`, not anything the sweep reads).

**v1 (locked all v0.6 items):**

All v0.6 open-for-v1 items resolved:

- **Migration file numbering + reversibility plan** → §7.6 — ordered 004 → 012 (in v1.1; v1's 013 dropped). Every migration in the v1.1 set is reversible.
- **Per-PR test coverage targets** → §11.5 — three required test classes per new table (CHECK constraint, UPSERT idempotency, ALTER TABLE NOT VALID where applicable) + JSONB shape validators + cross-schema FK referential-integrity tests + state-machine transition tests + index query-plan tests.
- **Phase A.5 parity-report runbook** → §7.7 — reframed twice: v1 made it "daily diff report with three outcome paths"; v1.1 made it "historic-compliance sweep with iteration loop." The schema artifacts back the new approach (§3.13).
- **`evaluation_orphaned` dashboard surface** → resolved: **AI-Scoring main team dashboard renders a red permanent banner (dismissible per row) when the count is > 0; Command Center Zone A renders a counter chip per CC §3 Zone A.** Two surfaces because the two consumer roles (analysts/QA leadership vs CC operators) need different presentation. v1 UI PRs implement; schema is ready.

All prior-version questions also closed:

- v0.5 Q1 → §8.3a (`audio_dependent_sections` lives in existing per-team scoring config).
- v0.5 Q2 → §7.1 (5 req/min overnight enrichment).
- v0.5 Q3 → §3.9 + §4.2 (monotonic `scored`, `evaluation_orphaned` flag, audit row).
- v0.5 Q4 → §4.2 (2-year `raw_call_details` aging post-v1, NULL-able by design).
- v0.5 Q5 → §11 (test suite shipped in v0.6).
- v0.4 Q1–Q6 → resolved in v0.5.
- v0.3 Q1–Q7 → resolved in v0.4 + earlier.

**Nothing open at v1.3.** Future revisions ship as new doc minor versions (v1.4, etc.) with their own migration file numbers.

---

## 11. First test suite (v0.6 deliverable for review)

The headline of v0.6. Without tests, every diff post-cutover is a risk we can't measure. This section is a **first set for review and iteration**, not exhaustive coverage — the goal is to lock the patterns (fixtures, DB isolation, parametrization shape) so v1's per-PR test additions follow the same shape.

### 11.0 Conventions and infrastructure

- **Test framework:** `pytest` + `pytest-asyncio` + `testcontainers[postgres]` for ephemeral Postgres-per-test-session.
- **DB isolation:** one fresh Postgres container per `tests/` session, with `004_*.sql` through `00N_*.sql` migrations applied at session-start. Each test runs in a SAVEPOINT-rolled-back transaction so tests don't see each other's writes.
- **Fixtures location:** `qa-automation/AI-Scoring/tests/fixtures/`. Golden fixtures for `compute_overall_score` live at `tests/fixtures/overall_formula/<team>.json`; webhook payload fixtures at `tests/fixtures/dialpad/`; cascade-output fixtures at `tests/fixtures/landgpt/`.
- **Naming:** `tests/qa/test_<topic>.py`, `tests/command_center/test_<topic>.py`, `tests/embeddings/test_<topic>.py`, `tests/integration/test_<topic>.py`. The fourth namespace is for cross-schema/cross-service tests.
- **Async:** the QA + CC layers are async-first (`asyncpg`); tests use `@pytest.mark.asyncio` throughout.

### 11.1 Unit tests — DB constraints and pure functions

| Path | Tests | Surface validated |
|---|---|---|
| `tests/qa/test_evaluations_state.py` | `test_relaxed_check_accepts_approved_with_null_score`, `test_strict_check_rejects_finalized_with_null_score`, `test_check_rejects_approved_without_evaluator_email`, `test_012_alter_table_not_valid_then_validate_succeeds` (migration test) | §3.4.3 CHECK constraints — relaxed-pre-cutover, strict-post-cutover, and the migration 012 mechanism itself. |
| `tests/qa/test_evaluations_dedupe.py` | `test_partial_unique_dialpad_call_id_allows_nulls`, `test_partial_unique_dialpad_link_allows_nulls`, `test_concurrent_inserts_with_same_call_id_one_wins` | §3.4.1 dual partial UNIQUEs. |
| `tests/qa/test_compute_overall_score.py` | `test_member_support_golden_fixtures` (parametrized over 30+ fixtures), `test_sales_golden_fixtures`, `test_hard_zero_short_circuits`, `test_na_redistribution_proportional`, `test_weight_transfer_chain`, `test_unknown_section_id_fails_at_load_not_compute` | §3.6 + §3.8 — pure-function correctness against historical truth. ε = 0.05 acceptance per row. |
| `tests/qa/test_formula_versions_archive.py` | `test_loader_inserts_unseen_version`, `test_loader_marks_predecessor_effective_until`, `test_loader_idempotent_on_same_hash` | §3.12 — startup-load archival of formula JSON. |
| `tests/qa/test_formula_compliance_sweeps.py` (new in v1.1) | `test_sweep_inserts_one_row_per_eval_per_version`, `test_sweep_idempotent_under_rerun` (ON CONFLICT DO NOTHING), `test_delta_generated_column_signed_correctly`, `test_flagged_matches_abs_delta_gt_epsilon`, `test_multiple_formula_versions_preserve_per_version_history` | §3.13 — historic-compliance sweep writer correctness + iteration preservation. |
| `tests/qa/test_evaluation_sections_provider.py` | `test_ai_provider_required_when_score_source_ai`, `test_ai_provider_null_for_manual` | §3.5 — `ai_provider` CHECK aligned with `score_source`. |
| `tests/qa/test_score_audit_orphan.py` | `test_orphaned_action_writes_score_audit_and_flips_flag` | §3.9 + §4.2 — `evaluation_orphaned` consistency. |
| `tests/qa/test_evaluations_v1_2_columns.py` (new in v1.2) | `test_needs_coaching_check_accepts_y_n_null`, `test_needs_coaching_check_rejects_other`, `test_human_review_pair_invariant_requires_required_at_first`, `test_scoring_status_extended_check_accepts_flagged_human_review` | §3.4 + §3.4.3 — v1.2 column additions on `qa.evaluations`. |
| `tests/qa/test_tags_and_evaluation_tags.py` (new in v1.2) | `test_tags_slug_unique`, `test_tags_category_index_present`, `test_evaluation_tags_unique_per_eval_tag_source`, `test_evaluation_tags_source_check`, `test_seed_includes_four_human_review_focus_tags` | §3.15 + §3.16 — tag taxonomy invariants + seed. |
| `tests/qa/test_coachings_lifecycle.py` (new in v1.2) | `test_conducted_by_role_check`, `test_status_check`, `test_agent_attitude_check`, `test_completion_pair_invariant`, `test_completed_requires_summary_and_completed_by`, `test_pending_coaching_allows_null_summary` | §3.17 — coachings table CHECKs + completion pair invariant. |
| `tests/qa/test_coaching_evaluations_mn.py` (new in v1.2) | `test_unique_coaching_eval_pair`, `test_cascade_on_coaching_delete_wipes_links`, `test_fk_violation_on_evaluation_delete` (no CASCADE on evaluation_id), `test_one_coaching_covers_multiple_evals`, `test_one_eval_appears_in_multiple_coachings` | §3.18 — M:N semantics + cascade boundaries. |
| `tests/qa/test_human_review_trigger.py` (new in v1.2) | `test_trigger_fires_when_process_adherence_le_3`, `test_trigger_fires_when_call_resolution_le_3`, `test_trigger_does_not_fire_when_above_threshold`, `test_per_team_config_can_add_trigger_sections`, `test_re_evaluation_at_stage_1_5_clears_or_re_fires_flag` | §3.14 + §3.8 — pipeline trigger policy + per-team config + re-evaluation on edits. |
| `tests/qa/test_rubric_versions.py` (new in v1.3) | `test_rubric_version_unique`, `test_seed_includes_sales_v1_and_member_support_v1`, `test_seed_rubric_json_contains_sections_and_scoring_prompt`, `test_effective_until_pair_invariant`, `test_team_id_fk_enforced`, `test_qa_evaluations_rubric_version_fk_enforced`, `test_qa_evaluations_rubric_version_nullable_for_backfill` | §3.19 — qa.rubric_versions write path + FK + seed verification. |
| `tests/qa/test_teams_operational_columns.py` (new in v1.3) | `test_stats_config_jsonb_accepts_canonical_shape`, `test_gemini_config_jsonb_accepts_canonical_shape`, `test_excluded_test_agents_array_seeded`, `test_sheets_config_legacy_column_present_and_nullable`, `test_updated_at_default_now` | §6 — operational config columns on public.teams + seed values from migration 010. |
| `tests/command_center/test_webhook_events_dedupe.py` | `test_duplicate_call_event_is_idempotent`, `test_duplicate_agent_status_is_idempotent`, `test_different_event_kinds_same_timestamp_both_succeed` | §4.1 partial UNIQUE indexes by `event_kind`. |
| `tests/command_center/test_calls_upsert.py` | `test_webhook_then_qa_upsert_merges_fields`, `test_qa_on_demand_creates_row_when_cc_never_saw_it`, `test_seen_via_immutable_on_subsequent_writes` | §4.2 write paths 1+2. |
| `tests/command_center/test_chiclets_data_writethrough.py` | `test_chiclet_update_writes_through_to_data_jsonb`, `test_snapshot_renders_from_data_alone` | §4.3 + CC P2.2 resolution. |
| `tests/embeddings/test_chunk_embedding_dim_check.py` | `test_exactly_one_embedding_column_populated`, `test_dim_must_match_model_registry` | §5.6 dim-class invariants. |
| `tests/landgpt/test_plan_b_trigger.py` | `test_all_audio_dependent_low_triggers_fallback`, `test_partial_low_does_not_trigger`, `test_no_audio_dependent_in_config_means_no_fallback`, `test_fallback_sections_match_evaluation_sections_provider` | §8.3a routing policy. |
| `tests/landgpt/test_models_used_validation.py` | `test_pydantic_accepts_gemini_only_shape`, `test_pydantic_accepts_landgpt_cascade_shape`, `test_pydantic_accepts_fallback_shape`, `test_pydantic_rejects_unknown_provider` | §8.1 JSONB shape. |

### 11.2 Integration tests — files, services, and parity

| Path | Tests | Surface validated |
|---|---|---|
| `tests/integration/test_stage_1_dual_write.py` | `test_writes_both_sheets_and_postgres_with_metadata`, `test_postgres_failure_does_not_block_sheets_write` (Phase A semantics), `test_sheets_failure_surfaces_as_error` | §3.2 Stage 1 + §7.2 dual-write failure semantics. |
| `tests/integration/test_stage_2_4_lifecycle.py` | `test_evaluation_progresses_draft_to_finalized`, `test_command_center_calls_scored_flips_at_evaluation_insert`, `test_agent_stat_point_appears_at_finalize` | §3.2 + §4.2 + §9.2 end-to-end on a single evaluation. |
| `tests/integration/test_phase_a5_sweep.py` (renamed in v1.1) | `test_new_formula_version_goes_live_on_fastapi_restart`, `test_post_formula_ship_new_evals_score_under_new_version`, `test_sweep_runs_over_backfilled_rows_only`, `test_iteration_v2_reduces_flag_count_vs_v1`, `test_flagged_row_query_returns_5_random_samples` | §3.6 + §3.13 + §7.1 + §7.7 — full Phase A.5 reframe behavior end-to-end. |
| `tests/integration/test_webhook_replay_invariant.py` | `test_replay_and_live_handler_produce_identical_state`, `test_replay_is_deterministic_under_ties` (event_timestamp ties broken by id), `test_replay_under_2s_at_10k_events` (tripwire) | §4.1.2 same-handler invariant + replay perf budget. |
| `tests/integration/test_backfill_phase_b.py` | `test_analyst_history_rows_become_evaluations`, `test_each_backfilled_eval_gets_calls_stub`, `test_agent_stat_points_seeded_in_eval_approved_at_order` | §7.1 Phase B + §7.3 correctness checks. |
| `tests/integration/test_backfill_calls_enrichment.py` | `test_rate_limit_respects_5_per_min` (token-bucket), `test_resume_from_checkpoint`, `test_partial_failure_logged_without_in_loop_retry` | §7.1 enrichment script behavior + §7.5 backfill design. |
| `tests/integration/test_landgpt_cascade_to_postgres.py` | `test_models_used_records_cascade_provenance`, `test_annotated_transcript_persisted`, `test_plan_b_activation_records_per_section_provider` | §8.1 + §8.2 + §8.3a end-to-end through the writer. |
| `tests/integration/test_pii_access_roles.py` | `test_privileged_role_reads_verbatim_transcript`, `test_team_role_gets_redacted_projection`, `test_audit_reader_blocked_from_transcript_bodies` | §8.4 three-tier role gate. |

### 11.3 Canonical samples — pattern lock for v1

Four sample test bodies that establish the patterns the rest follow. The fourth (§11.3.4) is new in v1 and targets migration mechanisms themselves — not the resulting schema, but the act of applying the migration.

#### 11.3.1 DB CHECK constraint test (pattern: testcontainers + raw SQL + ConstraintViolation)

```python
# tests/qa/test_evaluations_state.py
import pytest
from asyncpg.exceptions import CheckViolationError

@pytest.mark.asyncio
async def test_relaxed_check_accepts_approved_with_null_score(pg_conn):
    """Pre-cutover, Stage 2 writes evaluator_email + approved_at but
    overall_score is NULL until Stage 3 runs. The relaxed CHECK must accept
    this — the strict v0.4 CHECK rejected it."""
    eval_id = await pg_conn.fetchval(
        """
        INSERT INTO qa.evaluations
            (team_id, agent_name_raw, state, source,
             evaluator_email, approved_at,
             models_used, created_at)
        VALUES
            ('member_support', 'Test Agent', 'approved', 'ai',
             'mgr@landing.com', NOW(),
             '{"text":{"provider":"gemini","model":"gemini-2.5-flash"}}'::jsonb,
             NOW())
        RETURNING id
        """
    )
    assert eval_id is not None

@pytest.mark.asyncio
async def test_strict_check_rejects_finalized_with_null_score(pg_conn):
    """state='finalized' MUST have overall_score populated, both pre- and
    post-cutover."""
    with pytest.raises(CheckViolationError):
        await pg_conn.execute(
            """
            INSERT INTO qa.evaluations
                (team_id, agent_name_raw, state, source,
                 evaluator_email, approved_at, finalized_at,
                 models_used, created_at)
            VALUES
                ('member_support', 'Test Agent', 'finalized', 'ai',
                 'mgr@landing.com', NOW(), NOW(),
                 '{"text":{"provider":"gemini","model":"gemini-2.5-flash"}}'::jsonb,
                 NOW())
            """
        )
```

#### 11.3.2 Pure-function golden-fixture test (pattern: parametrize + load + assert ε)

```python
# tests/qa/test_compute_overall_score.py
import json
import pathlib
import pytest
from qa_automation.ai_scoring.backend.scoring import compute_overall_score

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures" / "overall_formula"
EPSILON = 0.05  # §3.6 acceptance threshold

def _load(team):
    with open(FIXTURES / f"{team}.json") as f:
        data = json.load(f)
    return [(row["evaluation_id"], row["sections"], row["expected_score"])
            for row in data["rows"]]

@pytest.mark.parametrize(
    "eval_id, sections, expected_score",
    _load("member_support"),
    ids=lambda x: f"eval_{x}" if isinstance(x, int) else None,
)
def test_member_support_golden_fixtures(eval_id, sections, expected_score, ms_formula):
    """Each fixture row is a real Analyst_History entry with its
    sheet-computed score. Python compute must land within ε of the sheet."""
    actual = compute_overall_score(sections, ms_formula)
    assert abs(actual - expected_score) <= EPSILON, (
        f"eval {eval_id}: Python={actual}, Sheet={expected_score}, "
        f"diff={abs(actual - expected_score):.4f} > {EPSILON}"
    )
```

#### 11.3.3 Integration test (pattern: mocked externals, multi-table assertion)

```python
# tests/integration/test_stage_1_dual_write.py
import pytest
from unittest.mock import AsyncMock
from qa_automation.ai_scoring.backend.services import sheets_service, postgres_service

@pytest.mark.asyncio
async def test_writes_both_sheets_and_postgres_with_metadata(
    pg_conn, mock_sheets_client, mock_dialpad_client, sample_scorecard
):
    """Stage 1 must write to both Sheets and Postgres, persist the full
    dialpad_call_metadata JSONB, and UPSERT a command_center.calls row
    that the new qa.evaluations FK points at."""
    mock_dialpad_client.get_call_details.return_value = {
        "call_id": "test-call-123",
        "caller_phone": "+15551234567",
        # ... full payload per §3.7
        "raw": {"call_id": "test-call-123", "...": "..."},
    }

    eval_id = await sheets_service.write_draft_to_fr_ai(
        sample_scorecard, team_config="member_support"
    )

    # Postgres: evaluation row exists with full metadata
    row = await pg_conn.fetchrow(
        "SELECT command_center_call_id, dialpad_call_metadata, models_used "
        "FROM qa.evaluations WHERE id = $1", eval_id
    )
    assert row["command_center_call_id"] is not None
    assert row["dialpad_call_metadata"]["call_id"] == "test-call-123"
    assert row["models_used"] is not None

    # Postgres: command_center.calls row UPSERTed
    cc_call = await pg_conn.fetchrow(
        "SELECT scored, seen_via, raw_call_details "
        "FROM command_center.calls WHERE id = $1",
        row["command_center_call_id"]
    )
    assert cc_call["scored"] is True
    assert cc_call["seen_via"] == "qa_on_demand"
    assert cc_call["raw_call_details"] is not None

    # Sheets: write fired
    mock_sheets_client.append_row.assert_called_once()
```

#### 11.3.4 Migration mechanism test (pattern: apply migration mid-test, then assert)

```python
# tests/qa/test_evaluations_state.py (continued)
import pathlib

MIGRATIONS = pathlib.Path(__file__).parent.parent.parent.parent / "database" / "migrations"

@pytest.mark.asyncio
async def test_012_alter_table_not_valid_then_validate_succeeds(pg_conn):
    """Migration 012 tightens the relaxed-pre-cutover CHECK via
    ADD CONSTRAINT ... NOT VALID + VALIDATE CONSTRAINT. This must succeed
    when every existing row already satisfies the strict shape (post-Phase
    C state). The test seeds rows in the strict shape, applies 012, and
    asserts no exception is raised and the constraint is present + valid."""

    # Seed two evaluations in the strict-compliant state (state='finalized'
    # with overall_score populated).
    for _ in range(2):
        await pg_conn.execute(
            """
            INSERT INTO qa.evaluations
                (team_id, agent_name_raw, state, source,
                 evaluator_email, approved_at, finalized_at,
                 overall_score, models_used, created_at)
            VALUES
                ('member_support', 'A', 'finalized', 'ai',
                 'm@l.com', NOW(), NOW(), 92.5,
                 '{"text":{"provider":"gemini","model":"gemini-2.5-flash"}}'::jsonb,
                 NOW())
            """
        )

    # Apply migration 012.
    migration_sql = (MIGRATIONS / "012_qa_evaluations_strict_state_check.sql").read_text()
    await pg_conn.execute(migration_sql)

    # Constraint is present and validated.
    constraint = await pg_conn.fetchrow(
        """
        SELECT conname, convalidated
          FROM pg_constraint
         WHERE conname = 'qa_evaluations_strict_state_check'
        """
    )
    assert constraint is not None
    assert constraint["convalidated"] is True

    # Strict shape now enforced: a row with state='approved' AND NULL score
    # (which the pre-cutover relaxed CHECK allowed) is rejected.
    with pytest.raises(CheckViolationError):
        await pg_conn.execute(
            """
            INSERT INTO qa.evaluations
                (team_id, agent_name_raw, state, source,
                 evaluator_email, approved_at,
                 models_used, created_at)
            VALUES
                ('member_support', 'A', 'approved', 'ai',
                 'm@l.com', NOW(),
                 '{"text":{"provider":"gemini","model":"gemini-2.5-flash"}}'::jsonb,
                 NOW())
            """
        )
```

This pattern generalizes: every migration that's not a pure CREATE TABLE gets a test that applies it mid-session and asserts the new invariant.

### 11.4 Deliberately NOT in v1's first set

These exist as post-v1 additions, not the v1 review pass:

- Full coverage of every JSONB shape (samples in §11.1 establish the pattern; per-PR expansion follows §11.5).
- Performance tests beyond the §4.1.2 replay tripwire.
- Embedding-model benchmark harness (§5.8) — separate workflow, not a regression test.
- LandGPT-side tests in `landing-ai/server/` — live with that service's own test suite.
- Phase D Sheets-retirement smoke tests — phase is deferred.

### 11.5 Per-PR test coverage targets (locked in v1)

Every implementation PR meets the bar below before merge. v1's job, not v0.6's:

**For every new table:**

- ≥1 CHECK constraint test (per CHECK declared on the table).
- ≥1 UPSERT-idempotency test (whichever combination of columns is the natural conflict target — typically the UNIQUE index).
- ≥1 ALTER TABLE … NOT VALID + VALIDATE test (only required where the migration sequence includes post-cutover tightening — i.e. tables 006 + 012). For pure CREATE TABLE migrations, this row is N/A and the PR description explicitly says so.

**For every new JSONB column carrying a structured shape (`models_used`, `annotated_transcript`, `recording_urls`, `dialpad_call_metadata`, `raw_payload`, `formula_json`, `chiclets.data`):**

- ≥1 Pydantic validator test that accepts the canonical shape.
- ≥1 Pydantic validator test that rejects an obvious malformation.

**For every cross-schema FK** (`qa.evaluations.command_center_call_id`, `qa.evaluations.sop_used_document_id`):

- ≥1 referential-integrity test (FK enforces).
- ≥1 graceful-degradation test (NULLABLE FK behavior under CC outage where the QA write must still succeed).

**For every state-machine column** (`qa.evaluations.state`; `qa.evaluations.scoring_status` since v1.2; `qa.coachings.status` since v1.2):

- ≥1 transition test per allowed transition (`draft → approved`, `approved → finalized`; for v1.2: `complete ↔ flagged_human_review` re-evaluation at Stage 1.5; `pending → completed` and `pending → cancelled` on coachings).
- ≥1 invalid-transition rejection test.

**For every CHECK pair invariant** (v1.2 — `human_review_completed_at` requires `human_review_required_at`; `qa.coachings` completion pair):

- ≥1 test that the second-half column cannot be set without the first.
- ≥1 test that the pair can be cleared (NULL'd) coherently when the eval transitions back to auto-flow.

**For every analytics index** (§9.1):

- ≥1 EXPLAIN-plan test asserting the index is used by the documented query shape.

**Integration coverage:**

- The §11.2 integration tests are the minimum cross-schema acceptance gate. Every Wave-2 PR (dual-write integration, webhook handler, backfill scripts) adds at least one new integration test.

This is the floor, not the ceiling. Reviewers can require more; reviewers cannot accept less without explicit waiver documented in the PR.

### 11.6 What this suite buys before v1 implementation starts

- **Confidence the schema is implementable as designed.** Writing the tests against the v1 spec surfaced several edge cases that became §10 resolutions and §7.7 runbook clarifications. The act of designing the test cases is the act of stress-testing the schema.
- **A regression net for the per-team Phase C truth-flips.** Phase C is when "Sheets is truth" becomes "Postgres is truth"; that flip is the highest-risk moment in the migration. The integration tests run identically pre-flip and post-flip.
- **A multi-agent contract.** Per §7.5, parallel agents own different schemas. The tests in §11.1 are the per-schema acceptance gate; the §11.2 integration tests are the cross-schema acceptance gate; §11.5 is the per-PR floor.

---

## 12. Reference links

- `landing-ai/LandGPT.md` — LandGPT v1 cascade architecture, hardware staging, Plan B. Drives §8.
- `qa-automation/AI-Scoring/references/LandingOpsCommandCenter.md` — gates Phase 1 on this doc; §5.1, §6, §10 are the parity surfaces.
- `qa-automation/AI-Scoring/backend/config/history_layout.py` — Analyst_History wide layout this schema replaces.
- `qa-automation/AI-Scoring/backend/services/sheets_service.py` — Stage 1–4 writes mapped in §3.2.
- `qa-automation/AI-Scoring/backend/services/dialpad_client.py:362` — expanded in v0.2 to return all payload fields + `raw`.
- `database/migrations/003_qa_scoring_schema.sql` — superseded stub; gap analysis in §3.1.
- `database/SQLMigration-v0.3-inputs.md` — review companion that drove v0.3 → v0.4. Delete after sign-off.
- `qa-automation/AI-Scoring/references/CallTimeOnAnalystHistory.md` — drives `call_connected_at` / `approved_at` split.
- `qa-automation/AI-Scoring/references/PRD-MultiTeam.md` — multi-team requirements feeding `public.teams`.
- External: <https://milvus.io/blog/choose-embedding-model-rag-2026.md> — embedding-model selection context for §5.
