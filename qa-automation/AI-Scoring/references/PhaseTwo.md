# Phase 2 — Schema Convergence + Sales Onboarding (Step 5)

> **Purpose:** Design doc for the work that lands Sales as the second team and retroactively
> straightens the Member Support layout into the same generalized shape. Companion to
> `PhaseOne.md` (which closed Step 4).
> **Author session:** 2026-05-06.
> **Status:** Design — not yet implemented. Review before writing JSON.

---

## Why Step 5 grew beyond "just add Sales"

The original Step 5 in the unified roadmap (`PhaseOne.md` §"Sales Onboarding") read as a
config drop: write `sales.json`, set up a Sales sheet, generate an API key, end-to-end
test. Walking through the real Sales rubric on 2026-05-06 revealed that the abstraction
work from Step 3 (PR #14) was incomplete in places we hadn't noticed because Member
Support is the only team that exercised it. Specifically:

1. **`sheets_service.py` hardcodes MS rubric shape in three places** (failure points #4, #5,
   #6 in the abstraction-testing branch — `documentation` literal id, `Q:AI` reasoning
   range, `[:8]` slicing).
2. **`Analyst_History` column layout is hand-tuned per team** via `section_columns`,
   `yn_columns`, and `extended_columns` dicts. Sales' inverted shape (4 numeric + 15 Y/N
   vs MS's 8 numeric + 1 Y/N) makes this hand-tuning painful and error-prone.
3. **The Apps Script `Config.js` is a parallel team-config layer** (`qa-automation/teams/
   member_support/Config.js`) that drifts independently from the Python `team_config.json`.
4. **Three frontend HTML files hardcode MS section IDs** (`index.html`, `dashboard.html`,
   `datapoint.html`) — would block Sales on its own, independent of any layout refactor.

Step 5 therefore expands from "config drop" to "schema convergence". The end-state: **one
team config drives every layer, layout is derived by formula from the section list,
adding team #3 is genuinely a JSON change**.

---

## What Sales' rubric looks like (locked 2026-05-06)

19 sections. Inverse proportion from MS:

| Q | id | type | notes |
|---|---|---|---|
| 1 | `greeting` | yn (AI) | transcript-derivable |
| 2 | `pb_creation` | manual | analyst checks CRM |
| 3 | `mc_call_notes` | manual | analyst checks CRM |
| 4 | `situation_match` | numeric (AI) | 1–5, has rubric text |
| 5 | `reason_for_move_pitch` | yn (AI) | |
| 6 | `value_uplift` | numeric (AI) | 1–5, has rubric text |
| 7 | `membership_explanation` | yn (AI) | |
| 8 | `flex_long_stay_pitch` | yn (AI) | |
| 9 | `landing_guarantee` | numeric (AI) | 1–5, has rubric text |
| 10 | `pricing_explanation` | yn (AI) | |
| 11 | `book_attempt` | yn (AI) | |
| 12 | `objection_handling` | numeric (AI) | 1–5, has rubric text |
| 13 | `urgency_disclosure` | yn (AI) | |
| 14 | `followup_setup` | yn (AI) | |
| 15 | `tonality_pace` | yn (AI) | |
| 16 | `hold_usage` | yn (AI) | |
| 17 | `audio_quality` | yn (AI) | |
| 18 | `screen_recording` | yn (auto-Yes) | fixed `"Yes"` for outbound — never AI-scored |
| 19 | `pre_send_intro` | yn (AI) | |

Totals: 4 numeric + 12 AI Y/N = **16 AI-scored**; 2 manual; 1 fixed-Yes; 19 total.

Existing pipeline source-of-truth lives at:
- `Form Responses 3` (form-submission tab; 243 historical entries to migrate)
- `Scores` (calculation tab; column Y holds the ARRAYFORMULA, weights from `Weighing!B2:B20`)
- `Mails` (agent roster — same convention as MS)
- New tabs to bootstrap: `Form Responses AI` (drafts) and `Analyst_History` (final).

---

## Locked design decisions from this session

| # | Decision |
|---|---|
| 1 | Manual approval block for Sales = Q2 (`pb_creation`) + Q3 (`mc_call_notes`). |
| 2 | New `SectionDef.auto_value` field for fixed-value sections (Q18). Prompt skips them; writer hardcodes the value. |
| 3 | Confidence caps applied per judgement (mixed caps allowed); Sales: `tone_alignment` cap medium, `closing` cap medium, etc. — finalized at JSON-review pass. |
| 4 | Score formula stays sheet-side. Python reads `score_readback_col` from the destination tab. |
| 5 | `score_readback_col`: MS = `Q`, Sales = `Y`. Both ARRAYFORMULA-driven. |
| 6 | Stage 2 (write to score destination) gated on **approval**, not on every dashboard save. Avoids re-triggering the formula on each evaluator edit. |
| 7 | Idempotency on Stage 1: re-scoring overwrites the FR-AI row keyed on `dialpad_link` (col E). Frontend pops a confirmation toast when collision detected. |
| 8 | `source` column values: `"ai"` \| `"manual"` \| `"migrated"`. Migrated rows isolatable in analytics. |
| 9 | FR-AI uses the **same programmatic layout** as Analyst_History. Stage 4 = row-copy (with timestamp swap from "scoring time" → "approval time"). |
| 10 | Apps Script `Config.js` generated from `team_config.json` at clasp pre-push time. Single source of truth. |
| 11 | Frontend Sales rollout folded into this commit (multi-team data-driven section lists in 3 HTML files). |
| 12 | Migration safety: rename current `Analyst_History` → `Analyst_History_legacy` for MS, write to a fresh tab. Cheap rollback. |
| 13 | Sales feedback migration: combined "What went well + Opportunities" cell from FR3 col X → new `opportunities` col, `key_strengths` left blank. Future evals split as designed. |
| 14 | Sales `Call Date` (FR3 col B) backfilled from Dialpad API where empty, using the call link in col A. |

---

## New TeamConfig schema

### `SectionDef` additions

```python
class SectionDef(BaseModel):
    # existing fields …
    auto_value: Optional[str] = None
    # When set, this section is never AI-scored:
    #   - prompt builder skips it
    #   - writer hardcodes auto_value at the score column
    #   - reasoning/confidence cells stay blank
    # Used for Sales Q18 (screen_recording = "Yes" for outbound).

    deprecated_at: Optional[str] = None
    # ISO date. Section stays in the layout for historical row stability,
    # but excluded from new evaluations. Not used at launch but reserved
    # so the schema doesn't have to break later.
```

### `analyst_history` becomes minimal — layout is derived

The existing `section_columns` / `yn_columns` / `extended_columns` / per-col-index fields
disappear. They are replaced by a derived layout module that takes `len(sections)` and
returns the canonical positions.

```json
"analyst_history": {
  "tab_name": "Analyst_History",
  "tab_name_legacy": "Analyst_History_legacy"
}
```

### New `score_destination` block

```json
"score_destination": {
  "tab_name": "Form Responses 1",
  "section_score_columns": {
    "greeting": "D",
    "caller_identity_validation": "E",
    "purpose_of_call": "F",
    "matching_the_moment": "G",
    "process_adherence": "H",
    "call_resolution": "I",
    "communication": "J",
    "efficiency_call_handling": "K",
    "documentation": "L",
    "customer_resolution_indicator": "M"
  },
  "score_readback_col": "Q",
  "arrayformula_buffer_seconds": 4
}
```

For Sales, `tab_name = "Scores"`, `score_readback_col = "Y"`, mapping is `D=greeting,
E=pb_creation, F=mc_call_notes, …, V=pre_send_intro` (section_number → letter offset).

`section_score_columns` is **the only place in the system with hardcoded letters**. This
mirrors the legacy form layouts that Apps Script + ARRAYFORMULAs already depend on.

### `form_responses_ai` simplified

The old `column_map` / `scored_section_columns` / `reasoning_start_col` /
`caller_metadata_cols` / `doc_reasoning_col` collapse to:

```json
"form_responses_ai": {
  "tab_name": "QA Scores"
}
```

Layout is derived (same formula as Analyst_History — see next section).

---

## The derived layout formula

Both `Analyst_History` and `Form Responses AI` use the same shape, parameterized only by
`N = len(sections)`.

| 0-indexed cols | content |
|---|---|
| 0 | `agent_name` |
| 1 | `agent_email` |
| 2 | `timestamp` |
| 3 | `evaluator_email` |
| 4 | `dialpad_link` |
| 5 | `overall_score` |
| 6 → 6+N−1 | section scores, one per section in `section_number` order |
| 6+N → 6+2N−1 | reasoning, one cell per section |
| 6+2N → 6+3N−1 | confidence, one cell per section |
| 6+3N | `key_strengths` |
| 6+3N+1 | `opportunities` |
| 6+3N+2 | `call_summary` |
| 6+3N+3 | `caller_name` |
| 6+3N+4 | `caller_phone` |
| 6+3N+5 | `source` |

**Stable section_number invariant**: once a team's rubric publishes, `section_number`
must never shift, even if a section is later deprecated (`deprecated_at` flag). Old rows
keep their position.

**Manual sections** sit in their normal slot in the score range; reasoning is whatever
the analyst typed; confidence is blank.

**`auto_value` sections**: score cell holds the literal value; reasoning + confidence
blank. Two cells per such section are intentionally empty — acceptable trade for keeping
the formula universal.

For Sales (N=19): row width = 6 + 3·19 + 6 = 69 columns (A–BQ).
For MS (N=10): row width = 6 + 3·10 + 6 = 42 columns (A–AP).

The shared layout module exposes:

```python
class HistoryLayout:
    def __init__(self, n_sections: int): ...
    def col_score(self, section_idx: int) -> int: ...
    def col_reasoning(self, section_idx: int) -> int: ...
    def col_confidence(self, section_idx: int) -> int: ...
    @property
    def col_overall_score(self) -> int: return 5
    # ... etc.
```

Both Python and Apps Script consume this same shape (see "Apps Script" below).

---

## Pipeline — 4 stages on the Step 1.5 [approve] backbone

The existing `[approve] Step 1..4` pattern (PhaseOne decision log, 2026-04-06) generalizes
cleanly. The names below replace the per-team-specific glue with a uniform contract:

### Stage 1 — Score draft → FR-AI

- AI scoring service writes a row to `Form Responses AI` in the new layout.
- Cells filled at this stage: `agent_name` (col A), `timestamp`, `dialpad_link`, all
  section scores for AI-scored sections, all reasoning + confidence cells, `auto_value`
  scores (Q18 → "Yes").
- Cells **left blank**: `agent_email` (filled at Stage 4 from Mails lookup),
  `evaluator_email` (Stage 4), `overall_score` (Stage 3), manual section scores
  (analyst), key_strengths/opportunities (analyst), caller metadata (analyst or
  enrichment).
- **Idempotency**: `dialpad_link` is the unique key. If a row exists, overwrite it.
  Frontend warns on collision.

### Stage 1.5 — Evaluator edits (asynchronous)

- Independent of the linear flow. Dashboard PUTs to FR-AI as analyst edits scores,
  reasoning, manual sections, and feedback.
- Approval is a separate explicit action, not implicit on save.

### Stage 2 — On Approve: section scores → score_destination

- Writer reads FR-AI row (using `dialpad_link` to locate it).
- Writes `section_score_columns` mapping to the destination tab. For MS that's
  `Form Responses 1` cols D–M; for Sales that's `Scores` cols D–V.
- Also writes the metadata columns the destination tab expects (timestamp, agent name,
  manager email, dialpad link). The mapping for those few columns is a small extension
  of `score_destination`.
- For `auto_value` sections, writes the literal value (e.g. `"Yes"`).
- For empty manual sections after analyst review (shouldn't normally happen, but
  defensible) — writes blank for Sales (formula treats as N/A), writes section default
  (1) for MS to match legacy semantics.

### Stage 3 — Poll readback → write Overall Score back to FR-AI

- After Stage 2's append/upsert returns, poll `score_destination.score_readback_col` on
  the new row with bounded retries (5 × 800 ms = 4 s ceiling, matches the existing 3–4 s
  buffer).
- Once a non-blank value appears, write it to FR-AI col F (`overall_score`).
- This is the only place a calculated score crosses from sheet → Python → sheet. After
  this, FR-AI col F is the canonical Overall Score for the row.

### Stage 4 — Copy FR-AI → Analyst_History; trigger email

- Read the now-complete FR-AI row.
- Resolve `agent_email` from the `Mails` lookup. Resolve `evaluator_email` from the
  authenticated session. Update the timestamp to "approval time".
- Append the row to `Analyst_History` (same layout — literally a row copy with the three
  late-bound cells filled).
- POST to the Apps Script web app's `doPost`. Apps Script reads the new history row,
  builds the email cards, sends.
- Apps Script no longer reads from `Form Responses 1` — `Analyst_History` is the source
  of truth for the email composer post-refactor (see next section).

---

## Apps Script — generated Config.js, multi-team overlay, push.sh

### Deployment surface: `./push.sh`

All Apps Script deployments go through the repo-root `push.sh` wrapper, registered in
`push.projects` (tab-separated manifest). The wrapper:

- Detects multi-team layout when the clasp dir matches `*/teams/*`.
- Stages `qa-automation/src/` (shared) + `qa-automation/teams/<team>/` (overlay) into
  `qa-automation/.build/<team>/` (hermetic — `rm -rf` first), rewrites the build dir's
  `.clasp.json` to `rootDir: "."`, then runs `clasp push` from the build dir.
- Surfaces target Script ID, branch, SHA, dirty-tree state, and the `live` flag from the
  manifest before requiring a literal `yes` confirmation.
- Logs every successful push to `.push-log` (gitignored, local audit).
- Honors `--dry-run` for a stage-only preview.

The team-source `.clasp.json` (in `teams/<team>/`) deliberately keeps a `rootDir` that
points to a non-existent path as a foot-gun guard: a stray bare `clasp push` from inside
`teams/<team>/` no-ops safely instead of partially deploying just `Config.js`. The build
dir is the only place `clasp push` is allowed to run, and `push.sh` is what gets it
there.

`push.projects` already contains the Sales entry (added pre-this-doc):
```
qa-sales    qa-automation/teams/sales    no    QA Automation — Sales (staging until cutover)
```
Cutover (Phase F) flips `live: no` → `live: yes` in `push.projects`. MS is `qa-member-support`,
already `live: yes`.

### Config.js generation

`Config.js` becomes a build artifact, not a hand-edited file. The generator
(`qa-automation/scripts/build_config.py`) reads `backend/config/teams/{team_id}.json`
and writes `qa-automation/teams/{team_id}/Config.js` with:

- `HISTORY_LAYOUT` constants derived from the `HistoryLayout(N)` formula
- `NUMERIC_CATEGORIES`, `BINARY_CATEGORIES`, `MANUAL_CATEGORIES` from `sections[]`
  partitioned by `score_type`
- `SECTION_LABELS` from `sections[].name`
- `RUBRIC_QUESTIONS` from `sections[].rubric_question`
- Brand colors and email-template constants (kept hand-edited in a small `Branding.js`
  that the generator does not touch)

The generator must be run before `./push.sh qa-<team>`. Two-step deployment workflow:

```bash
python qa-automation/scripts/build_config.py member_support
./push.sh qa-member-support              # confirm 'yes' at the prompt
```

CI verifies that the generated `Config.js` matches what's checked in (drift fails the
build) so the file stays in lockstep with `team_config.json` even if a developer forgets
to regenerate before commit.

### AnalystHistory.js refactor

Three functions move from "interleaved walk over `HISTORY_EXTENDED_LAYOUT`" to "three
parallel passes":

- `getHistory()` — reads scores in `[6, 6+N)`, reasoning in `[6+N, 6+2N)`, confidence in
  `[6+2N, 6+3N)`. Joins by section index.
- `_writeEnrichment()` — writes the same three contiguous ranges instead of interleaved
  pairs. The enrichment write happens during Stage 4's row-append, not as a separate
  pass.
- `QAEntry.toHistoryRow()` — emits the full row in one shot now that scoring/reasoning/
  confidence/feedback all flow through FR-AI before reaching Apps Script.

### Sales container — what's already there, what's missing

Already in place:
- `push.projects` entry `qa-sales` (live=no).
- `qa-automation/teams/sales/` directory exists (empty).

To stand up:
1. Create the Apps Script project from inside the Sales spreadsheet (Extensions → Apps
   Script → "Untitled project"). Capture the Script ID from the project's URL.
2. Write `qa-automation/teams/sales/.clasp.json` with that Script ID and the foot-gun
   `rootDir` pattern (mirror what `member_support/.clasp.json` does).
3. Run `python qa-automation/scripts/build_config.py sales` → generates
   `teams/sales/Config.js`.
4. `./push.sh qa-sales --dry-run` to verify staging, then drop `--dry-run` for the real
   push. Confirm with `yes` at the prompt.
5. In the Apps Script editor: deploy → Web App → execute as me, access anyone with link.
   Capture the deploy URL into the Sales API key's secret bundle (alongside the
   existing Member Support deploy URL).

### What does NOT change

- `Main.js` `doPost` flow (parse → `_processRow` → email send)
- The 6-min execution timeout headroom
- `onFormSubmit` 3 s buffer for ARRAYFORMULA hydration (independent path)
- The deliberate `A:V` read-width on `Form Responses 1` (commit 284a965 — don't widen)

---

## Frontend — multi-team via team config

Three files have MS-only assumptions (audit findings, Step 5 prep). All three become
data-driven:

| File | Current | New |
|---|---|---|
| `dashboard.html:334-350` | Hardcoded `SECTION_KEYS` + `SECTION_LABELS` (MS only) | Fetch from `/api/{team_id}/sections` (new endpoint, returns id/name/score_type/section_number) |
| `datapoint.html:372-383, 390` | Hardcoded `sectionOrder` + `sectionLabels` + `'documentation' ? 'MANUAL'` literal | Same `/sections` endpoint; `'MANUAL'` derived from `score_type === 'manual'` |
| `index.html:803-824, 933-943` | `sectionRows.splice(8, 0, …)` for Documentation; literal `id: 'documentation'` in approve payload | Iterate over team config's manual sections; insert each at its `section_number - 1` index |

`team_dashboard.html` is already data-driven (consumes `binary_stats` and
`section_analysis` from `/team/stats`). It serves as the template the other three should
mirror.

**API additions:**
- `GET /api/{team_id}/sections` → array of `{id, name, score_type, section_number,
  audio_dependent, na_applicable}` (no scoring criteria — those stay backend-only).

**Latent bug to fix as part of this:** scorecard send-payload uses `opportunities`,
history responses use `improvements`. Standardize on `opportunities` everywhere. One-line
backend-side rename.

---

## Migration

### MS Analyst_History (one-shot, in-place reorder)

`scripts/migrate_ms_history.py`:

1. Read every row of `Analyst_History` (current MS layout).
2. For each row, transpose into the new layout:
   - Old col 0/1/2/3 → new col 0/1/2/5 (overall_score moves from D to F)
   - Old col 14 → new col 3 (manager_email → evaluator_email)
   - Old col 15 → new col 4 (dialpad_link)
   - Old `section_columns` + `yn_columns` → new score range `[6, 6+N)` in
     `section_number` order
   - Old `extended_columns` (interleaved reasoning + confidence pairs) → new reasoning
     range `[6+N, 6+2N)` and confidence range `[6+2N, 6+3N)`
   - Old col 16/17/18 → new col 6+3N / 6+3N+1 / 6+3N+5 (key_strengths, improvements
     [renamed to opportunities], source)
   - Old call_summary/caller_name/caller_phone (37/38/39) → new col 6+3N+2/3/4
   - Documentation extended (40/41) → reasoning + confidence cell for documentation in
     the new layout
3. Write to fresh tab `Analyst_History` (after renaming current → `Analyst_History_legacy`).
4. Verify row counts match. Verify a small sample of overall_scores match. Bail on
   mismatch.

### Sales — 243-row import from FR3 → new Analyst_History

`scripts/import_sales_history.py`:

1. Read FR3 rows (A–AA, 244 rows incl header).
2. For each row:
   - Map A → dialpad_link, B → timestamp, C → agent_name (resolve email via Mails)
   - D–V → 19 section scores (placed in score range `[6, 25)`)
   - W → evaluator_email
   - X (combined feedback) → `opportunities`; `key_strengths` blank
   - Y → timestamp (use as primary if FR3 col B was blank)
   - Z (agent email lookup formula) → already covered via Mails, ignore
   - AA (Month) → derived; ignore
3. For rows where col B (Call Date) is blank, call Dialpad `get_call_details(call_id)`
   to backfill. Rate-limit: 5 calls/sec, ~1 minute total for 243 rows worst case. Cache
   results.
4. Reasoning + confidence: blank for migrated rows (no AI ran).
5. `source` = `"migrated"` so analytics can filter these out until trust is established.
6. Append to fresh `Analyst_History`.

---

## Cutover plan

1. Branch off `main` → `feat/sales-onboarding-phase2`.
2. Phase A (schema + JSON): land schema changes + new `member_support.json` + new
   `sales.json`. Tests still red but compile-clean.
3. Phase B (sheets_service refactor): replace monolithic writers with the 4 stage
   functions. Existing 10 xfails become regular tests; expect 4 to fix here (#4, #5, #6,
   plus #1 long-call note).
4. Phase C (Apps Script): Config.js generator + AnalystHistory.js refactor + Sales
   container creation. Run end-to-end against MS sheet (legacy layout) to confirm parity.
5. Phase D (frontend multi-team): three HTML files become data-driven.
6. Phase E (migrations): MS in-place reorder, Sales 243-row import. Both gated behind a
   `--dry-run` flag; require a live confirmation prompt before touching production
   sheets.
7. Phase F (cutover):
   - Rename current MS `Analyst_History` → `Analyst_History_legacy`.
   - Run MS migration → fresh tab.
   - Regenerate + push MS Apps Script: `python scripts/build_config.py member_support &&
     ./push.sh qa-member-support` (LIVE — extra confirmation prompt fires).
   - Smoke test: score one MS call end-to-end through Stages 1–4. Verify email lands.
   - Run Sales 243-row import.
   - Generate + push Sales Apps Script: `python scripts/build_config.py sales &&
     ./push.sh qa-sales` (still `live: no` in `push.projects` until smoke passes).
   - Smoke test: score one Sales call end-to-end. Verify `Scores!Y` populates, FR-AI col F
     fills, Analyst_History row appears, email lands.
   - Flip `qa-sales` from `live: no` → `live: yes` in `push.projects`.
8. Update PhaseOne decision log + close Step 5 in the unified roadmap.

Rollback: if anything fails post-cutover, dashboard repoints to `Analyst_History_legacy`,
sheets_service writers revert to the old branch. Sales rolls back by deleting the new
tabs (Sales has no production traffic until cutover).

---

## Phasing & sequencing

| Phase | Days | Parallelizable? |
|---|---|---|
| A — Schema + JSON | 0.5 | — |
| B — sheets_service + history_service + team_stats | 1.0 | — |
| C — Apps Script + Config.js gen + Sales container | 0.75 | with B |
| D — Frontend multi-team | 0.5 | with B |
| E — Migration scripts (MS reorder + Sales FR3 import) | 0.5 | after B |
| F — Cutover + smoke tests | 0.5 | after all |

Critical path A → B → E → F ≈ 3 days. C and D run alongside B. Total wall-clock target:
**~3 days** with focus.

---

## Open questions to resolve at JSON-review pass

1. **Per-section `confidence_cap` values for Sales.** Defaults proposed: `tone_alignment`
   = "medium", `closing` = "medium", everything else null. Need user sign-off on each
   numeric section.
2. **Score-destination metadata columns for both teams.** Beyond
   `section_score_columns`, the writer needs to know where timestamp/agent_name/
   manager_email/dialpad_link go on the destination tab. Likely a small `metadata_cols`
   sub-block under `score_destination`.
3. **`weights_tab` reference for Sales.** The Sales formula reads `Weighing!B2:B20`. We
   said sheet-only, no Python mirror — but worth recording the tab name + range as a
   reference value in `score_destination` so the doc-of-truth lives in one file.
4. **Rubric prompt template for Sales.** MS's `system_prompt_template` and SOP-section
   list go into `scoring_prompt`; needs Sales-equivalent text. The existing Sales
   placeholder copy in `sales.json.example` is a reasonable starting point.
5. **Should the existing `sales.json.example` be deleted** once the real `sales.json`
   lands, or kept as a documented schema illustration? Lean delete.

---

## Decisions to add to PhaseOne decision log

| Decision | Date | Rationale |
|---|---|---|
| Sales onboarding scope expanded to schema convergence | 2026-05-06 | Hardcoded MS shape in sheets_service + parallel Apps Script Config.js + frontend section-id literals would re-block any team #3. Better to fix once with team #2 fresh than migrate twice. |
| Analyst_History layout becomes formula-derived from N=len(sections) | 2026-05-06 | Generalized layout per Step 5 design — see PhaseTwo §"Derived layout formula". Replaces hand-tuned section_columns / yn_columns / extended_columns dicts. |
| Apps Script Config.js generated from team_config.json at clasp pre-push | 2026-05-06 | Eliminates the parallel team-config layer that drifts from Python config. CI verifies no drift. |
| Stage 2 (write to score destination) gated on approval, not on every dashboard save | 2026-05-06 | Avoids re-triggering ARRAYFORMULA per evaluator keystroke. Lets manual sections settle to their final value before the formula reads them. |
| `auto_value` field added to SectionDef for fixed-value sections (Sales Q18) | 2026-05-06 | Cleaner than hardcoding `if section.id == "screen_recording"`. Prompt-token-lean (skipped from prompt). |
| `source` column gains "migrated" value | 2026-05-06 | Lets analytics filter out the 243 legacy Sales rows until trust is established. |

---

## Out of scope

- **PostgreSQL migration (Step 9).** The new layout is DB-shaped, but actually moving the
  store stays Step 9.
- **SSE / live updates for Sales.** Step 2.5 already covers MS; Sales gets it for free
  once team-scoped events stream through `/api/{team_id}/events`. No new design work.
- **Cost tracking for Sales (Step 7).** Per-team budget caps wait until 3+ teams.
- **Notion/RAG integration (Step 6).** Sales SOP injection is a separate epic.
- **Splitting Sales' combined Feedback into key_strengths + opportunities for migrated
  rows.** Heuristic split too error-prone; migrated rows get the combined cell in
  `opportunities` and `key_strengths` blank. Future evals split as designed.
- **Frontend visual redesign.** Three files become data-driven (`dashboard.html`,
  `datapoint.html`, `index.html`); no new components, no new pages, no styling work.
- **Apps Script deploy URL rotation / OAuth.** Existing pattern (manual deploy URL +
  bundled secret) is fine for two teams.

---

## Phase 2+ follow-ups — ordered by silent-failure impact

These items surfaced during Phase 2 implementation but aren't in this phase's scope.
Ordered by **what breaks if a future session doesn't tackle them** — top of the list is
data loss or launch block; bottom is doc hygiene. A session picking up cold should start
at the top and only skip an item if a higher-priority one absolutely depends on it.

### Tier 1 — Critical (production data loss or launch-blocking)

#### 1.1 Migration script mapping tables (Phase E prerequisite)
**Status:** Phase E §"Migration" in this doc is light on column-mapping detail.
**Silent failure:** A Phase E session re-derives mappings from memory and writes a
one-shot migration that puts data into wrong columns. Worst case: 243 Sales FR3 rows
imported with reasoning/feedback in the wrong cells; MS legacy reasoning+confidence
pairs un-pair across the new contiguous ranges. Recovery requires
`Analyst_History_legacy` restore + re-run.
**Where to start:** Add a column-mapping table per migration:
- MS in-place: old `analyst_history.section_columns` index → new
  `L.col_score(section_idx_by_section_number)`; old `extended_columns` pair → new
  `L.col_reasoning(i)` + `L.col_confidence(i)`.
- Sales FR3 import: FR3 col D-V → new `L.col_score(0..18)`; FR3 col X (combined
  feedback) → new `L.col_opportunities`; FR3 col Y → timestamp.

#### 1.2 Manual-section dashboard input for Sales
**Status:** Stage 1.5 already accepts analyst writes to manual section cells via
`apply_analyst_edits_to_fr_ai`, but the frontend may not yet expose distinct inputs for
manual sections (Q2 `pb_creation`, Q3 `mc_call_notes`).
**Silent failure:** Analysts can't fill Q2/Q3 from the dashboard. Sales calls cannot be
approved end-to-end. Sales rollout blocked entirely.
**Where to start:** Audit the approve-payload UI in `frontend/index.html` for inputs
keyed on `score_type === "manual"`. Add explicit manual-section affordances if missing.

#### 1.3 Apps Script trigger flip must be atomic (Phase C)
**Status:** `trigger_apps_script` currently passes `dest_row` (FR1/Scores tab); Apps
Script reads from FR1.
**Silent failure:** If a future session updates Python to send `history_row` without
simultaneously updating Apps Script to read from Analyst_History (or vice versa), MS
production emails break for hours/days until the other side catches up. Confusing
failure mode — emails just stop, no error.
**Where to start:** Phase C must be a single PR that touches both
`backend/services/sheets_service.py` (`trigger_apps_script` payload) AND
`qa-automation/src/Main.js` (doPost source-of-truth). Document this constraint at the
top of the Phase C PR description.

### Tier 2 — High (operational pain or user-visible bug)

#### 2.1 `improvements` ↔ `opportunities` field name standardization (Phase D)
**Status:** Latent bug found in audit. `EvaluationRecord` still uses `improvements`;
scorecard send-payload uses `opportunities`. Phase B (b) bridged the schema-side rename
but didn't standardize the API surface.
**Silent failure:** Frontend code may read `r.improvements` from a response that
returns `opportunities` (or vice versa) → blank fields in the dashboard, "no feedback"
appearance. Easy to miss in casual testing.
**Where to start:** Standardize on `opportunities` end-to-end (model field, API key,
frontend reader). One renaming pass across `EvaluationRecord`,
`history_service._parse_row`, the three hardcoded-section frontend files.

#### 2.2 In-memory `_jobs` store (PhaseOne tech debt, partial mitigation in Phase 2)
**Status:** `routes/scoring.py` still uses `_jobs: dict[str, dict]`. Phase 2's Stage 1
`dialpad_link` idempotency partially mitigates by making re-scoring safe.
**Silent failure:** Railway restart mid-job → manager polls `/score/{job}` and gets
404. Has to re-upload audio, but Stage 1 won't duplicate the FR-AI row (idempotency
saves us). Still an annoying, low-trust UX.
**Where to start:** Persist job state to a small SQLite file or a Sheets tab (cheap
before Step 9 Postgres lands). `_jobs[key]` keys are already team-scoped — store the
same dict per `(team_id, job_id)`.

### Tier 3 — Medium (rework cost or quota concerns)

#### 3.1 Step 8 (webhook automation) architectural sketch
**Silent failure:** A future webhook session re-invents the stage flow, possibly
skipping Stage 1.5 (no human edits in webhook path) without realizing Stage 2 needs the
analyst-edited values for manual sections. Wastes time, can produce wrong scores.
**Where to start:** Add a §"Stage flow for non-interactive scoring" to PhaseTwo:
webhook calls Stage 1 → auto-approves if score > X → Stage 2 → Stage 3 → Stage 4. The
stage abstraction makes this trivial. Manual sections need a sensible default (likely
the existing `manual_default_value`).

#### 3.2 Step 9 (Postgres) ETL sketch
**Silent failure:** ETL writer re-derives schema instead of using `history_layout`
module. Drift between sheet and DB schemas.
**Where to start:** Add a §"From sheet to SQL" to PhaseTwo: each FR-AI/Analyst_History
row → 1 `qa_entries` row + N `qa_section_scores` rows. The derived layout *is* the row
schema; ETL walks `history_layout` positions and emits SQL.

#### 3.3 Cost / API call multiplication for future scaling
**Status:** Phase 2 added ~5x sheet API calls per approve (Stages 1.5 + 2 + 3 poll + 4
+ trigger). At 50 calls/day/team this is ~250 extra; well within Sheets quota.
**Silent failure:** Step 8 webhook automation could 10x this volume. Sheets API write
quota is 60/min/user (soft limit) — could throttle during burst load.
**Where to start:** Once Step 8 is on the roadmap, add per-team rate limiting in
`sheets_service` or batch Stage 2/4 writes.

#### 3.4 Mails-missing-entries fallback for migration
**Status:** Sales FR3 has 243 historical entries; some evaluators may no longer be in
the Mails tab (people who left).
**Silent failure:** Migrated rows get blank `evaluator_email`. Audit trail incomplete;
analytics filtering by evaluator misses these.
**Where to start:** Phase E migration script should warn (and log) on unresolved
evaluator names rather than silently writing blank.

#### 3.5 Per-team Apps Script container rationale
**Status:** Decision baked into `push.sh` + `push.projects`, but not explicitly
documented.
**Silent failure:** A future "let's consolidate to one shared container" instinct
breaks the per-spreadsheet binding model and the deliberate `A:V`-only read on FR1
(commit 284a965 — ARRAYFORMULA performance fix).
**Where to start:** One-paragraph note in PhaseTwo §"Apps Script": each team has its
own container-bound Script because container-binding + ARRAYFORMULA performance reads
are spreadsheet-local; cross-spreadsheet `openById` would re-introduce the column
explosion.

### Tier 4 — Low (doc hygiene, cosmetic)

#### 4.1 Tech debt resolved by Phase 2 — cross-reference
**Silent failure:** Future session reads PhaseOne tech debt table, attempts to re-fix
items already closed.
**Where to start:** Update PhaseOne §"Known Technical Debt" to mark as resolved: FRAI
append duplication, rubric hardcoded in 8+ files (last holdouts), 2s ARRAYFORMULA
buffer. Phase 2's decision-log additions in this doc cover the *why*; PhaseOne needs the
*it's done* marker.

#### 4.2 Step 6 (SOP RAG) forward-pointer
**Silent failure:** RAG implementer assumes layout dependency on SOP content. Wastes
time exploring.
**Where to start:** One line in PhaseTwo: SOP injection happens in `scoring_service`
before Stage 1; layout doesn't change. `sop_sections` config is already wired per team
(MS: `[5, 6]`; Sales: `[7, 8, 9, 10]`).

#### 4.3 Audio-as-source-of-truth principle echo
**Silent failure:** Future contributor doesn't recognize `audio_dependent: bool` as the
abstraction backing Step 1's long-call note. May add parallel mechanisms.
**Where to start:** Mention in PhaseTwo §"Schema additions" alongside `auto_value`:
`audio_dependent` flag is the canonical "this section needs the audio file, not just
the transcript" marker — used by long-call notes and by future webhook auto-approval
gating.

#### 4.4 `feedback_combined` separator format verification
**Status:** Stage 2 currently joins as `"What went well:\n…\n\nOpportunities for
improvement:\n…"` — guess at convention.
**Silent failure:** Analysts see oddly-formatted feedback in the Sales Scores tab.
Cosmetic only.
**Where to start:** Inspect a few existing FR3 col X entries for the actual analyst
convention; adjust `_combine_feedback` in `sheets_service` to match.

#### 4.5 `print(...)` → proper logging
**Silent failure:** Production debugging stays harder than it should be. Phase 2 added
several Stage `print()` calls.
**Where to start:** Replace `print(...)` with `logger.info(...)` across `sheets_service`,
`history_service`, `scoring_service`. Wire to the existing audit middleware logger if
applicable.

---

## Repository

- **Branch:** `feat/sales-onboarding-phase2` (off `main`)
- **Working dir:** `qa-automation/AI-Scoring/`
- **Apps Script source:** `qa-automation/src/` (shared logic),
  `qa-automation/teams/{team_id}/` (per-team overlay incl. generated `Config.js` + the
  team's `.clasp.json` foot-gun guard).
- **Apps Script build dir (transient, regenerated each push):**
  `qa-automation/.build/{team_id}/` — staged by `push.sh`, push origin for `clasp push`.
- **Apps Script deployment:** `./push.sh qa-<team>` (registry: `push.projects` at repo
  root; audit log: `.push-log`, gitignored). Run
  `python qa-automation/scripts/build_config.py <team>` first to regenerate `Config.js`.
- **Tests:** `qa-automation/AI-Scoring/tests/` — 60 tests + 10 xfails carried over from
  the abstraction-testing branch. Expect 4 xfails to become passing tests during Phase B
  (#1, #4, #5, #6); the other 6 either no longer apply (#2 Sales is also 1-5; #7 Sales is
  also 1-5) or get parked with a written reason.
