# Backfill Plan — Analyst_History → `qa.evaluations` (Phase B seed)

> Wave 2 Phase 5 groundwork ([Wave2Plan.md](../qa-automation/AI-Scoring/references/Wave2Plan.md)).
> Source of truth for the historic backfill: what the seed contains, what maps where, what gets
> enriched from Dialpad in nightly stages, and what is permanently unrecoverable.
>
> **Seed:** `database/analyst_history_member_support.csv` — gitignored (agent/caller PII); the
> derived, anonymized fixtures under `tests/fixtures/` are the only committed artifacts. Sales will
> arrive later **in this same column shape** (its own sections); every loader in this plan is
> team-parameterized for that reason.

| | |
|---|---|
| **Status** | Decisions locked 2026-07-01 (§8) — ready to implement |
| **Seed rows** | 1,678 evaluations (2,794 CSV lines — multiline reasoning cells) |
| **Coverage** | Member Support, 2025-02 → 2026-06-29 (16 rows with broken timestamps) |
| **Last updated** | 2026-07-01 |

---

## 1. Seed inventory

43 columns: 6 identity/meta, 10 section scores, 10 reasonings, 10 confidences, 5 text/caller
fields, `Source`, plus one **header-less trailing column** of timestamps (§8 D1).

Two populations, split cleanly by `Source`:

| | pre-AI ("manual era") | AI era |
|---|---|---|
| rows | 1,152 | 526 (`Source='ai'`) |
| reasonings / confidence | **absent** (never existed) | present (confidence: always `high`, lowercase) |
| Call Summary | mostly absent | mostly present |
| Caller Name / Phone | mostly absent | mostly present |
| first / last eval | 2025-02 → 2026-05-01 | 2026-03-21 → 2026-06-29 |

The eras **interleave** for ~6 weeks (manual evals continued after the AI rollout) — era membership
comes from `Source`, never from the timestamp.

Universal facts: `Overall Score` populated on **every** row (integer 0–100); section scores
populated on every row; `Evaluator Email` ~always present; `Agent Email` missing on **45%** of rows
(59 distinct agents, only 37 ever appear with an email); `Dialpad Link` present on 98.7%.

Data-quality inventory:

| Issue | Count | Disposition |
|---|---|---|
| Mixed binary vocab (`Y`/`Yes`/`N`/`No`/`Not Applicable`) | ~650 rows touched | normalize (§4) |
| Duplicate `Dialpad Link` groups | 34 groups (23 with differing overall = re-evaluations) | all import; latest keeps the link (§8 D2) |
| Broken timestamps (epoch-zero / unparseable) | 16 | Dialpad repair in B2 |
| All-zero section rows, no agent name | 2 | test artifacts — excluded, logged |
| Manual hard-zero overrides (overall=0, non-zero sections) | 5 | **excluded**, logged (§8 D5) |
| Hand-edited overall (inconsistent with §2 formula) | 3 | import verbatim + `backfill_anomaly` note (§4) |
| Confidence vocabulary (`high` vs DB `HIGH`) | all 526 AI rows | normalize (§4) |

---

## 2. Finding: the legacy sheet formula, reverse-engineered

Least-squares fitting + hypothesis testing against all 1,678 sheet-computed scores recovered the
ARRAYFORMULA exactly:

```
overall = Σ  weight_i × (rating_i / 5)        binary: Y=1.0, N=0.0
weights (twelfths):
    call_resolution 4/12 · comms 2/12 · efficiency 2/12 · documentation 2/12
    purpose 1/12 · cri 1/12
    greeting 0 · caller_id 0 · matching 0 · process_adherence 0
NA  → drop the section, rescale remaining weights to 100 (proportional redistribute)
```

Evidence: **1,670 / 1,678 rows (99.5%) match exactly** (±0.5 on the sheet's integer rounding), both
eras, including 44/44 NA rows under proportional redistribution. The 8 non-matching rows are 5
manual hard-zero overrides (overall=0 with non-zero sections) and 3 apparent hand-edits.

Two consequences:

1. **The legacy formula is fully expressible in the Phase 1a `Formula` shape** — rating curve
   `output: [0.2, 1.0]` (= r/5), zero-weight sections, `na: redistribute_per_rules` +
   `na_redistribution` (proportional, `targets: remaining`) rules. Archiving it as a
   `qa.formula_versions` row (proposed id: **`member_support_v0_sheet`**) makes every backfilled
   row version-stamped, reproducible via `compute_overall_score()`, and testable — golden fixtures
   can assert *exact* engine-vs-sheet parity instead of an ε band (§6).
2. **The ε-sweep narrative writes itself.** The sheet has been silently ignoring Greeting,
   Caller ID, Matching the Moment, and **Process Adherence** — the section the Ops-signed v2
   formula weights at 25%. Historic-compliance deltas will be large and *by design* (SQLMigration
   §3.6: acceptance, not iteration). QA leadership should see §2 before seeing the sweep output.

---

## 3. Column → schema mapping

Target rows: one `qa.evaluations` + 10 `qa.evaluation_sections` per CSV row.

| CSV | Target | Notes |
|---|---|---|
| Agent Name | `evaluations.agent_name_raw` | `agent_id` resolved in B3, stays NULL when unresolvable |
| Agent Email | `evaluations.agent_email` | NULL on 45% — partially repairable via Mails tab (B3) |
| Timestamp | `evaluations.call_connected_at` | col C = Dialpad call clock (PR-1 semantics, drives team_stats); B2's Dialpad lookup is authoritative and overwrites |
| *(header-less col 43)* | `evaluations.approved_at` + `.finalized_at` | approval clock (§8 D1); the ~50 rows missing it fall back to Timestamp, annotated |
| Evaluator Email | `evaluations.evaluator_email` | |
| Dialpad Link | `evaluations.dialpad_link` | dedupe key (§3.4.1); dup policy §8 D2 |
| Overall Score | `evaluations.overall_score` | **sheet value verbatim** — never recomputed at import |
| — | `evaluations.formula_version = 'member_support_v0_sheet'` | §8 D3 — archived legacy formula row |
| — | `evaluations.rubric_version = 'member_support_v1'` | the 010-seeded rubric (still has `documentation`) |
| — | `evaluations.state = 'finalized'` | seed rows are settled history; CHECKs need `approved_at`/`finalized_at` + `overall_score` — all satisfied |
| — | `evaluations.source` | `Source='ai'` → `ai_reviewed` (AI-drafted, analyst-approved — the CHECK's vocabulary); blank → `manual`. Confirm against what live Stage 2 stamps at dual-write time. |
| — | `evaluations.models_used` | AI era: `{"text":{"provider":"gemini","model":"gemini-2.5-flash"}}`; manual era: `{"text":{"provider":"human","model":"human_brain"}}` (§8 D4) |
| 10 section scores | `evaluation_sections` rows | legacy `history_id`s as `section_id` (incl. `documentation`) — §3.6 v1.2 |
| 10 reasonings | `evaluation_sections.reasoning` | AI era only |
| 10 confidences | `evaluation_sections.confidence` | normalized to `HIGH` etc. |
| Key Strengths / Opportunities | `evaluations.key_strengths` / `.opportunities` | |
| Call Summary | `evaluations.call_summary` | AI era mostly; NULL elsewhere |
| Caller Name / Phone | `evaluations.caller_name` / `.caller_phone` | AI era mostly; B2 repairs |

Per-section fields: `score_type` numeric/binary per rubric (`manual_numeric`/`manual_binary` for
the manual era); `score_source` = `ai` (with `ai_provider='gemini'`, `model='gemini-2.5-flash'`)
when the row is AI-era **and** the section carries a reasoning, else `manual`; NA lands as
`binary_value='NA'` (binary sections; the numeric-NA shape from migration 012 doesn't occur in
this seed — Documentation was always 1–5).

---

## 4. Normalization rules (B0)

- Binary: `Yes→Y`, `No→N`, `Not Applicable→NA`; already-clean `Y`/`N` pass through.
- Confidence: lowercase → `HIGH`/`MED`/`LOW`.
- Timestamps: parse sheet-local, store TIMESTAMPTZ; the 16 broken ones → NULL + repair queue (B2).
- Excluded (written to `backfill_exclusions.csv` with reason): the 2 all-zero test rows, and the
  5 manual hard-zero overrides (§8 D5).
- The 3 hand-edited rows → imported verbatim, annotated in `dialpad_call_metadata`
  (`{"backfill_anomaly": "overall_score inconsistent with v0_sheet formula"}`) so the ε sweep can
  segregate them.
- Every transform is logged; the loader is idempotent (natural key = Dialpad Link + Timestamp) so
  re-runs upsert instead of duplicating.

## 5. Staged nightly plan

- **B0 — sanitize + stage (offline, no DB):** CSV → normalized parquet/JSON staging file + exclusion
  and anomaly logs. Pure, re-runnable, reviewable before anything touches Railway.
- **B1 — row import (one batch, off-peak):** staging → `qa.evaluations` + `qa.evaluation_sections`.
  No external calls; ~1,676 evals / ~16,760 section rows, minutes of work. §7.4 checks after.
- **B2 — Dialpad enrichment (nightly schedule, rate-limited):** for each row with a `dialpad_link`,
  resolve `dialpad_call_id`, call clocks (authoritative `call_connected_at`/`started`/`ended`/
  `duration`), `recording_urls`, caller name/phone, `call_type`; repair the 16 broken timestamps.
  Cursor-based (`dialpad_call_metadata ->> 'backfill_enriched_at'`), small nightly batches outside
  peak hours, resumable, failures logged and retried next night.
- **B3 — identity resolution (after B2 settles):** `agent_id` via `qa.agents` (name + Mails-tab
  email lookup); missing historic agent emails repaired where Mails covers them, otherwise NULL
  forever (accepted).
- **B4 — verification + sweep handoff:** §7.4 correctness checks (counts, per-agent/per-month
  score distributions vs sheet), then the Phase 6 ε sweep runs `compute_overall_score()` under v2
  with the §2 narrative attached.

Constraint honored throughout: **no writes** to `qa.assessments` / `qa.coachings` /
`qa.evaluation_tags` / `command_center.*` (Wave 2 rule); B1/B2/B3 touch only `qa.evaluations`,
`qa.evaluation_sections`, `qa.agents`.

## 6. Golden fixtures (Phase 2c, unblocked by this analysis)

Sample ~40 rows stratified by era × score band × NA presence, plus the 3 annotated hand-edit rows
→ `tests/fixtures/overall_formula/member_support.json`. Each fixture: anonymized label, section
answers, sheet overall, era. Tests assert `evaluate_formula(member_support_v0_sheet, answers) ==
sheet_overall` **exactly** (the hand-edit rows marked expected-mismatch) and record the v2 delta
as documentation output. Sales §11's 75.00 vector stays the Sales fixture until its export lands.

## 7. Permanently unrecoverable (do not fabricate)

- **Manual-era reasonings/confidence** — no AI ran; re-scoring recordings now would fabricate
  provenance for an analyst's historic judgment. They stay NULL. (Re-scoring as *new* data is a
  separate, opt-in exercise, never attributed to the original eval.)
- Manual-era **call summaries** — same reasoning; NULL unless a future decision generates them
  clearly marked as post-hoc.
- **Agent emails** beyond Mails-tab coverage; **caller identity** where Dialpad no longer retains
  the call; the 22 agents who never appear with an email.

## 8. Decisions (locked 2026-07-01)

- **D1 — header-less column 43 = the approval clock** (confirmed by ops). Column C ("Timestamp")
  is Dialpad's call-connected clock per PR-1 — it drives team_stats analytics and frontend
  ordering. Mapping: col C → `call_connected_at`, col 43 → `approved_at` + `finalized_at`
  (fallback to col C for the ~50 rows missing it, annotated).
- **D2 — duplicate Dialpad links: import all** (re-evals are real history). Latest row (by
  approval clock, falling back to Timestamp) keeps `dialpad_link`; earlier rows stash it in
  `dialpad_call_metadata.superseded_dialpad_link`, preserving the §3.4.1 UNIQUE.
- **D3 — archive `member_support_v0_sheet` in `qa.formula_versions`.** Backfilled rows are
  version-stamped, reproducible, and exactly testable (§2, §6).
- **D4 — `models_used` for fully-human evaluations:
  `{"text": {"provider": "human", "model": "human_brain"}}`.** Fits the standard §8.1 shape
  (provider/model are free-text; `ModelsUsed` parses it; section-level `ai_provider` CHECK is
  untouched since manual sections carry `score_source='manual'`), and satisfies §3.4's "every
  scored evaluation has at least the text model recorded". This same value is the canonical
  marker for the future full-manual-override pipeline (analyst scores a call from scratch —
  reasonings, scores, confidence, feedback — with zero AI involvement).
- **D5 — the 5 manual hard-zero rows are excluded** from the backfill, logged in
  `backfill_exclusions.csv`.
