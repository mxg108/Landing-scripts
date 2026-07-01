# Wave 2 Plan — QA Pipeline Automation for Agent Bonus Metrics

> **Owning document for the 2026-07 push.** A fresh session should be able to open only this file plus the two team scoring specs and execute against the plan.
>
> **Ordered by silent-failure impact per [[feedback_silent_failure_ordering]]:** Phase 3 (formula sign-off) sits ahead of Phase 5 (backfill) because a locked-but-wrong formula silently produces wrong bonus numbers for every agent every month, while a delayed backfill loudly excludes rows that never got dual-written.

| | |
|---|---|
| **Status** | v1 (2026-06-30, authoritative post-v1.4 schema) |
| **Objective** | Postgres-backed monthly agent bonus metrics live by **2026-07-31** |
| **Deadline** | 31 calendar days from today |
| **Owner** | AI-Scoring backend + QA leadership (sign-off gate) |
| **Superseded** | prior "Wave 2 inventory" laid out in chat — this doc is the reference going forward |

---

## 1. Objective — what "bonus-ready" means

By month-end we can produce, for each active agent, a **defensible monthly QA score** that Landing HR can use as a bonus input. Concretely:

- `GET /api/{team_id}/bonus_metrics?month=2026-07` returns per-agent:
  - `overall_score` — from `qa.compute_overall_score()` running against a **locked** `formula_version` + `rubric_version`
  - `evaluations_included` — count of finalized evals in the month
  - `sections_summary` — per-section rollup for defensibility during appeals
- The number is **reproducible** — feed the same eval row + the two archived versions, get the same score forever
- QA leadership has **signed off** on both:
  1. The formula/rubric that's live (per team, see §3)
  2. The historic-compliance sweep result (per team, §7.7 of `SQLMigration.md`)

That's the finish line. Nothing else needs to ship for that outcome.

---

## 2. Sources of truth for the formula and rubric

**These files are AUTHORITATIVE:**

| File | Team | Status |
|---|---|---|
| `database/member_support_scoring_migration.md` | Member Support | ✅ **Finalized** — Ops VP sign-off. 4 minor items in §10 still to confirm (see §5 below). |
| `database/sales_scoring_migration.md` | Sales | ⚠️ **Populated from source PDF, PENDING sign-off** — 7 open items in its §10. Blocks Sales bonus math. |

**These files DEFINE THE SCHEMA that the specs load into:**

- `database/SQLMigration.md` — the QA-schema design (versions v1.0 → v1.4 shipped). Read §3.6 (compute_overall_score), §3.8 (formula rules pipeline), §3.12 (formula_versions), §3.14 (human-review trigger), §3.19 (rubric_versions).

**These files are the LEGACY (partial, being superseded):**

- `qa-automation/AI-Scoring/backend/config/teams/{sales,member_support}.json` — current JSON files that the app reads. Section IDs and weights **differ** from the Ops-signed specs. Section-key remapping in §9 is REQUIRED to reconcile.

**Precedence when they disagree:** team scoring migration `.md` > SQLMigration.md > team JSON files. Any mismatch is resolved by treating the `.md` as the target and generating both the DB seed AND the (soon-to-be-generated) JSON file from it.

---

## 3. Delta from what's already shipped

**What's on Railway prod as of 2026-06-30** (through migration 011, PR #68 applied):

- `public.teams` seeded with `member_support` + `sales` (v1.3 operational columns filled)
- `qa.evaluations` + `qa.evaluation_sections` (schema, empty)
- `qa.agents` (schema, empty)
- `qa.rubric_versions` seeded with `sales_v1` + `member_support_v1` **BUT sourced from the legacy JSON files, NOT the Ops-signed specs**
- `qa.formula_versions` (schema, empty)
- `qa.formula_compliance_sweeps` (schema, empty)
- `qa.assessments` + `qa.assessment_sections` (schema, empty)
- `qa.tags` seeded with 4 human-review-focus tags
- `qa.coachings` + `qa.coaching_evaluations` (schema, empty)
- `qa.agent_stat_points` (schema, empty)
- `qa.score_audit` / `qa.api_audit_log` / `qa.evaluation_tags` (schema, empty)

**What is NOT yet on Railway** (deferred to Wave 3, do not touch during Wave 2):

- `command_center.*` — the whole schema is deployed but no writer wired up
- `embeddings.*` — schema deployed, no consumer

**The critical delta between what's seeded and what's authoritative:**

The `member_support_v1` and `sales_v1` rows already in `qa.rubric_versions` (from migration 010's seed) reference the **legacy JSON section keys** — they DO NOT match the Ops-signed specs. When Wave 2 kicks off:

1. New `member_support_v2` row lands in `qa.rubric_versions` with the Ops-signed content; the current `_v1` gets `effective_until = NOW()`
2. First-ever `member_support_v2` row lands in `qa.formula_versions` with the Ops-signed formula
3. Same pair for Sales — but only AFTER Sales management signs off on the 7 open items

**Do NOT edit the migration 010 seed to fix this.** Ship the new versions as new rows. Ranks with `[[project_predictive_groundwork]]` — versioning is exactly for this case.

---

## 4. Formula engine — what needs to be built

The Ops-signed specs use a **richer formula shape** than `SQLMigration.md §3.8` currently describes. `qa.compute_overall_score()` and the Pydantic `Formula` model must expand to support the full spec shape. Deltas from §3.8:

| Field / concept | §3.8 today | Ops-signed spec (§8 of each team file) |
|---|---|---|
| Formula shape | `sections + rules + scale` | `formula_id + rubric_version + scale + normalization + sections + rules + evaluation_order + triggers + external_signals` |
| Rule schema | `type + if + fields per type` | `id + type + enabled + when + effect + note` — every rule now has a stable ID and enabled flag |
| NA policy | implicit in rule pipeline | **explicit per-formula** — MS `redistribute_per_rules`, Sales `full_credit` |
| Evaluation order | implicit array order | **explicit** array of rule IDs + `weighted_sum` sentinel |
| Rule types | `hard_zero`, `na_redistribution`, `weight_transfer` | + `weight_redistribution` (equal-additive), + `score_scale` (post-sum multiplier), + `flag` (emits event, doesn't affect score) |
| Weight transfer amount | "all" or fraction | `amount: "all"` OR `fraction: N.M` — MS uses fraction for `frequent_caller_shift` |
| Triggers | `human_review_triggers` (§3.8) | `triggers.active` + `triggers.candidates` + `triggers.threshold` + `triggers.routes_to` — same idea, richer shape |
| External signals | not modeled | `external_signals.frequent_caller` — MS's `frequent_caller_shift` rule reads a signal delivered via Command Center Looker snapshot |

**Two schema updates queue behind this:**

- **Spec `SQLMigration.md` §3.8 needs to grow to match the Ops-signed shape.** File a v1.5 spec update (not urgent, doc only).
- **Pydantic `Formula` model** (Wave 2a #1 below) implements the richer shape from day 1.

**External signals — how to handle without Command Center.** MS's `frequent_caller_shift` rule reads `signals.frequent_caller` from a Looker snapshot delivered via CC. CC is postponed. Ship rule the with `enabled: true` in the formula JSON; wire `frequent_caller` as **`false` by default at evaluation time** in `compute_overall_score()`. When CC ships later, the signal starts firing without a formula change. Zero impact on bonus math meanwhile — the rule stays a no-op.

---

## 5. Open items blocking the plan

**Member Support (§10 of the MS scoring spec):**

- [ ] Confirm `matching` / `efficiency` become **active** triggers or stay candidates (currently only `process_adherence` + `call_resolution` fire escalation)
- [ ] Confirm `frequent_caller_shift` default fraction (0.5) and default target (`process_adherence`)
- [ ] Confirm hard-zero stays globally available (enabled per team) vs. removed outright for MS
- [ ] Confirm NA-spread behavior when **multiple** sections are NA (current: recompute equal-additive over remaining active sections)

**Sales (§10 of the Sales scoring spec):**

- [ ] Confirm rating→points curve (`(rating − 1) / 4` vs `rating / 5`) — **materially changes every scaled section's score**
- [ ] Confirm NA = full credit on-slot **for all 15 sections** with no redistribution
- [ ] Confirm `formula_id = sales_v1` and version lineage
- [ ] Confirm section keys (the spec's proposed keys)
- [ ] Confirm whether to persist the PDF's `category` grouping on `qa.formula_section`
- [ ] Confirm `client_experience` placement under *Post-Call & Documentation*
- [ ] Confirm no rules / no triggers planned for `sales_v1`

**Nothing else can start on Sales bonus math until §10 is closed.** MS can start now.

**Q — should we get all sign-offs before writing any code?** No — Phase 1 and 2 (Foundation + Compute Engine) are entirely internal, agnostic to which sections/weights end up locked. Formula sign-off gates Phase 3 (ship the versions) onward, not the engine itself.

---

## 6. Phased plan (31 days, 2026-06-30 → 2026-07-31)

Phases 1–2 are internal engineering. Phases 3–6 depend on QA leadership signoffs.

### Phase 1 — Foundation (Days 1–5)

**Goal:** Every downstream write validates against the same Pydantic contract; `team_config` reads from DB with a file fallback.

| # | PR | Notes |
|---|---|---|
| 1a | Pydantic models — `Formula`, `Rule` (all 6 types), `Rubric`, `RubricSection`, `ScoringPrompt`, `EvaluationSection`, `AssessmentSection`, `ModelsUsed`, `AnnotatedTranscript`, `RecordingUrls` | Uses the **Ops-signed formula shape** — richer than §3.8. Read the two team `.md` files first. |
| 1b | `team_config.py` DB-refactor + `backend.config.export_team` CLI | Q1.b dual-source (DB primary, JSON fallback), Q2.c cache-bust-on-write. Export script writes JSON from DB state — the "Save" button's future backend. |

**Deliverable:** every `.json` shape is now Pydantic-validated at load; runtime reads from DB when available. Local dev still works without DB.

### Phase 2 — Compute engine (Days 4–12)

**Goal:** `qa.compute_overall_score(evaluation_id)` produces a deterministic, versioned score from row + archived formula + archived rubric.

| # | PR | Notes |
|---|---|---|
| 2a | Rule engine — 6 rule types (hard_zero, weight_transfer, weight_redistribution, score_scale, flag, score_override), `evaluation_order` sequencing, `normalization` block with `na_policy` switch (redistribute vs full_credit) | Pure function, no DB access. Table-driven per `Formula` — one engine, both teams' rules. |
| 2b | `qa.compute_overall_score(evaluation_id)` — the DB-touching wrapper | Loads formula + rubric from `qa.formula_versions` / `qa.rubric_versions`; calls the rule engine; returns `NUMERIC(5,1)`. |
| 2c | Golden fixtures — per team, ~30-50 real Analyst_History rows with their sheet-computed scores → JSON fixtures at `tests/fixtures/overall_formula/{team}.json` | Sales' spec §11 gives the **first test vector** (75.00 with the sample answers) — use it. MS parity is the harder gate. |

**Deliverable:** given a filled-in `qa.evaluations` row + `qa.evaluation_sections` rows, `compute_overall_score()` returns the correct score under any formula version.

### Phase 3 — Formula sign-off + version ship (Days 8–15)

**Goal:** Both teams have their new formula_version + rubric_version live in the DB, effective from a specific timestamp forward.

| # | PR | Notes |
|---|---|---|
| 3a | Ship `member_support_v2` — new row in `qa.rubric_versions` + first row in `qa.formula_versions` | Mark existing MS `_v1` row `effective_until = NOW()`. Content from the finalized MS scoring spec §8. |
| 3b | Close MS §10 open items with Ops VP | Fixes the 4 lingering ambiguities before ship. |
| 3c | Coordinate with Sales management to close §10 open items | 7 items. Independent of MS ship. |
| 3d | Ship `sales_v1_ops` — new rubric_version + first formula_version | Post Sales sign-off. Existing `sales_v1` gets `effective_until = NOW()`. |

**Deliverable:** for both teams, `qa.rubric_versions WHERE effective_until IS NULL` and `qa.formula_versions WHERE effective_until IS NULL` return the Ops-signed content.

### Phase 4 — Dual-write from sheets_service.py (Days 12–20)

**Goal:** Every new eval from the current pre-filled scorecard workflow lands in Postgres alongside Sheets.

| # | PR | Notes |
|---|---|---|
| 4a | `sheets_service.py` Stage 1 dual-write | Writes `qa.evaluations` (`state='draft'`) + `qa.evaluation_sections` at write_draft_to_fr_ai time. Postgres failures **swallowed** (§7.3 Phase A semantics). |
| 4b | `sheets_service.py` Stage 2 dual-write | State transitions `draft → approved`, sets `evaluator_email` + `approved_at`. Post-Phase-C this is when `compute_overall_score` fires; pre-cutover we still take the Sheet's ARRAYFORMULA result via Stage 3 readback. |
| 4c | `sheets_service.py` Stage 4 dual-write + `qa.agent_stat_points` seed on finalize | `state → finalized`, resolves `agent_id` via `qa.agents`. One new stat point per finalize (§9.2). |
| 4d | Section-key remapping layer (§9 below) | Legacy JSON section keys → Ops-signed spec keys, per team. Applied at write path so `evaluation_sections.section_id` uses the Ops-signed key from day one. |
| 4e | §3.14 human-review trigger fires at Stage 1 / Stage 1.5 | Reads formula's `triggers` block (MS has active triggers; Sales has none). Sets `scoring_status='flagged_human_review'` + `human_review_required_at`. |

**Deliverable:** any eval flowing through the current scorecard workflow after this cutover is fully replicated in Postgres.

### Phase 5 — Backfill Phase B (Days 15–25)

**Goal:** All historical evals from Analyst_History are in `qa.evaluations` so "current month's metric" isn't a 2-week slice.

| # | Script | Notes |
|---|---|---|
| 5a | `scripts/backfill_phase_b.py` — read Analyst_History per team → INSERT into `qa.evaluations` (`state='finalized'`) + `qa.evaluation_sections` | Uses section-key remapping (§9). Preserves `eval_approved_at` on the eval. Rate-limited to gspread-safe pace. |
| 5b | `qa.agent_stat_points` seeding — replay per team ordered by **`eval_approved_at`** (not `finalized_at`) | §7.1 clarification — `finalized_at` is the backfill clock, not history. |
| 5c | Stub `command_center.calls` rows with `seen_via='qa_backfill'` per backfilled eval | Needed so `qa.evaluations.command_center_call_id` FK is populated. Enrichment via `get_call_details()` runs in background (5 req/min); NOT required for bonus. |

**Deliverable:** every finalized Analyst_History row appears as a `qa.evaluations` row with `state='finalized'`. `agent_stat_points` series is complete in historical order.

### Phase 6 — Phase A.5 historic compliance sweep (Days 20–28)

**Goal:** Recompute every backfilled row's `overall_score` under the new formula, surface flagged patterns, iterate until QA leadership accepts.

| # | Item | Notes |
|---|---|---|
| 6a | Sweep script — for each backfilled row, `compute_overall_score()` under the new formula_version, persist to `qa.formula_compliance_sweeps` | §3.13 spec, ε = 0.05. Per-team run. |
| 6b | Flag distribution report per team | §7.7 runbook. Feeds QA leadership review. |
| 6c | Iterate formula versions if patterns warrant | Each iteration = new formula_version row + fresh sweep. Loop terminates when QA leadership signs the flagged set. |

**Deliverable:** locked `formula_version` per team, backed by explicit QA leadership sign-off on the flagged-row set.

### Phase 7 — Cutover + bonus surface (Days 25–31)

**Goal:** Postgres is truth; monthly bonus metric surface answers HR's question.

| # | PR | Notes |
|---|---|---|
| 7a | Phase C truth-flip per team | Reads move from Sheets to Postgres for agent history, team stats, dashboards, `Score_Audit` search. Sheets ARRAYFORMULA continues but its output is no longer authoritative. |
| 7b | `GET /api/{team_id}/bonus_metrics?month=YYYY-MM` — per-agent monthly rollup | SQL view or on-the-fly aggregate. Returns `{agent, overall_score, evaluations_included, sections_summary}`. |
| 7c | Frontend: bonus dashboard reads from the new endpoint | Small — replace the current stats source. |

**Deliverable:** HR can pull the July 2026 bonus number for every active agent, defensibly, from a versioned formula they can review the sign-off history of.

---

## 7. Explicit deferrals (do NOT touch during Wave 2)

The scope is disciplined. These items are OUT until Wave 3 (post-2026-07-31):

- **Command Center** — the entire `command_center.*` schema and its writer. `qa.evaluations.command_center_call_id` stays NULL for backfilled rows (§4.2 allows this). Live CC development kicks off in August.
- **Rubric editor UI** — managers keep editing team `.md` specs + the export script writes new versions. No web editor in July.
- **Formula editor UI** — same.
- **Assessment writer/reader** — `qa.assessments` sits empty. The current 1h in-memory TTL persists for July.
- **Coaching workflow UI + tag apply/remove APIs** — DB tables exist; no writer or UI in July.
- **LandGPT v2 integration** — Gemini continues producing scores. No cascade in July.
- **Embeddings live ingestion** — schema is ready; consumer waits for LandGPT v2.
- **Sheets Phase D retirement** — Sheets stays as a redundant sink through Wave 3.

If it's not in §6, it's not this cycle.

---

## 8. Delivery risks and how they surface silently

Ranked by silent-failure impact per [[feedback_silent_failure_ordering]]:

**Critical — silent wrong numbers:**

1. **Formula lock happens before sign-off closes.** If Sales §10 items don't get answered but we ship anyway, every Sales bonus is computed under a guessed rating curve. Discovery is post-payroll — very expensive.
   → **Mitigation:** Sales version ships only after §10 is closed in writing (spec update marking each item resolved).

2. **Section-key remapping is wrong.** A silent misalignment between Analyst_History column and spec section key means one section is scored as "0" for every backfilled eval, invisibly deflating everyone's score.
   → **Mitigation:** Migration script asserts `evaluations_included × sections_per_eval == inserted_section_rows`. Any per-team sanity fails loud.

3. **NA policy mismatch.** If Sales runs under MS's redistribute-NA instead of full-credit, or vice versa, every eval with an NA on any section produces a different score.
   → **Mitigation:** `Formula.na_policy` is a required field with a CHECK; loading a formula without one raises. Test: run the §11 Sales worked example (expected 75.00) — assert exact.

**High — wrong numbers with clear symptoms:**

4. Weight sum ≠ 100 in a shipped formula → assertion at Pydantic load fails loud.
5. Formula references section_id absent from active rubric → hard-fail validator (§3.19.3) blocks ship.

**Medium — reversible:**

6. Backfill misses rows (parse errors on Analyst_History edge cases) → row-count mismatch surfaces during Phase A.5 sweep.
7. `qa.agent_stat_points` order wrong (seeded by `finalized_at` not `eval_approved_at`) → EWMA lookbacks compute under wrong history; PR-review-catchable.

**Low:**

8. Cost attribution missing on assessment writes → not blocking; can backfill from `api_audit_log`.

---

## 9. Section-key remapping table (mandatory reference for Phase B + dual-write)

The legacy JSON section keys don't match the Ops-signed spec keys. Wave 2 code must translate at write time (§4d) AND at backfill time (§5a).

**Member Support:**

| Legacy JSON key | Ops-signed spec key | Notes |
|---|---|---|
| `greeting` | `greeting` | unchanged |
| `caller_identity_validation` | `caller_id` | shortened |
| `purpose_of_call` | `purpose` | shortened |
| `matching_the_moment` | `matching` | shortened |
| `process_adherence` | `process_adherence` | unchanged |
| `call_resolution` | `call_resolution` | unchanged |
| `communication` | `comms` | shortened |
| `efficiency_call_handling` | `efficiency` | shortened |
| `documentation` | `human_review_required` | **renamed + repurposed** — see SQLMigration.md §3.5 v1.2 deprecation note; legacy rows in `qa.evaluation_sections` stay with `section_id='documentation'`, new rows use `human_review_required` |
| `customer_resolution_indicator` | `cri` | shortened |

**Sales:** the Ops-signed matrix is 15 sections, but the legacy JSON has 19 with substantially different keys. The remapping table cannot be finalized until **Sales §10 is closed** — several current sections are being dropped and new ones introduced. Placeholder mapping to be authored during Phase 3c and reviewed with Sales management.

| Legacy JSON key | Ops-signed spec key | Status |
|---|---|---|
| `greeting` | `greeting` | mapped |
| `pb_creation` | `potential_booking` | mapped (renamed) |
| `mc_call_notes` | `notes_mc` | mapped (renamed) |
| … remaining 15 sections | | **PENDING §10 close** |

---

## 10. Reference commands + status checks

**Verify Railway state matches this doc's Phase-3 preconditions:**

```sql
-- Should show: sales_v1 + member_support_v1 (from migration 010's seed).
-- After Phase 3 you'll see additional rows.
SELECT rubric_version, team_id, effective_from, effective_until
FROM qa.rubric_versions ORDER BY team_id, effective_from;

-- Should be EMPTY until Phase 3 lands the Ops-signed formulas.
SELECT formula_version, team_id, effective_from FROM qa.formula_versions;

-- Should return NULL for now; becomes non-NULL after Phase 4 dual-write starts.
SELECT rubric_version, formula_version FROM qa.evaluations WHERE id = (SELECT MAX(id) FROM qa.evaluations);
```

**Golden-fixture parity check (Phase 2c):**

Load Sales's §11 worked example into a test → `compute_overall_score(evaluation_id)` MUST return `75.00`. That's the only single-test spec-guaranteed value we have as of 2026-06-30.

---

## 11. Handoff notes for a fresh session

If you're picking this up in a new conversation:

1. **Read this doc top to bottom.**
2. **Read `database/member_support_scoring_migration.md` and `database/sales_scoring_migration.md`** — these are the formula/rubric authorities.
3. **Confirm the current state of Railway matches §3 "already shipped".** If not, find out what changed.
4. **Confirm the current state of Sales §10 sign-offs.** If any of the 7 items are still open, do NOT ship `sales_v1_ops` yet.
5. **Start with Phase 1a** (Pydantic models) unless someone tells you otherwise. It blocks nothing else and unblocks everything.
6. **Never edit the two `_scoring_migration.md` specs unilaterally.** They're Ops VP / Sales Management sign-off surfaces.
7. **Never write to `qa.assessments` / `qa.coachings` / `qa.evaluation_tags` / `command_center.*` — those are out of scope for Wave 2.**

Questions this doc doesn't answer belong in the chat that produced it. Cross-reference `[[SQLMigration.md]]` for schema-level detail (§3.6 compute_overall_score, §3.19 rubric write path, §7.6 migration ordering).
