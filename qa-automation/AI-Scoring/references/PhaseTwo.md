# Phase 2 — Schema Convergence + Sales Onboarding (Step 5)

> **Purpose:** Design doc for the work that lands Sales as the second team and retroactively
> straightens the Member Support layout into the same generalized shape. Companion to
> `PhaseOne.md` (which closed Step 4).
> **Author session:** 2026-05-06.
> **Status (2026-05-11):** Phases A, B, C, E ✅ landed on branch
> `feat/sales-onboarding-phase2`. Tier 1.1/1.2/1.3 follow-ups ✅. Both
> teams' `Analyst_History` tabs are now in the new derived layout with
> matching column-header conventions. **Up next:** Phase D (frontend
> multi-team — `dashboard.html` + `datapoint.html`), then Phase F
> (cutover). See §"Phase 2 status" below for a precise picture.

---

## Phase 2 status (2026-05-11)

Branch: `feat/sales-onboarding-phase2` (off `main`, not yet pushed).

| Phase | Status | Landed in |
|---|---|---|
| A — TeamConfig schema + JSON | ✅ | `11cc270` |
| B (a) — sheets_service 4-stage pipeline | ✅ | `11cc270` |
| B (b) — history_service + team_stats | ✅ | `11cc270` |
| B (c) — Test refactor (59/3/2) | ✅ | `11cc270` |
| Tier 1.1 — Migration mapping tables | ✅ | `8391052` |
| Tier 1.2 — Manual-section dashboard inputs + `/api/{team_id}/sections` | ✅ | `8391052` |
| Tier 1.3 — Atomic-flip constraint docs | ✅ | `8391052` |
| C — Apps Script atomic flip + Config.js generator + Branding split | ✅ | `8391052` |
| Bridge legacy compat constants in generator | ✅ | `8391052` |
| E — Migration scripts (MS reorder + Sales FR3 import) | ✅ | `1c51e9c` |
| MS Analyst_History header normalization | ✅ | `661630a` |
| **D — Frontend multi-team: `dashboard.html` + `datapoint.html`** | ⏭️ **Next** — see §"Phase D audit" below |
| F — Cutover + smoke tests | ⏭️ |

**Live state of production sheets:**
- MS `Analyst_History` is already in the new 42-col layout (migrated on
  2026-05-11). Old data renamed to `Analyst_History_legacy`.
- Sales `Analyst_History` is populated with 242 migrated rows in the new
  69-col layout, all tagged `source='migrated'`. One unresolved agent
  ("Raul") needs adding to the Sales `Mails` tab if still active.
- Both tabs share matching prefix/trailing canonical headers and
  display-name section headers (e.g. "Greeting", "Caller Identity
  Validation", "Pricing Breakdown") — verified by schema audit on
  2026-05-11.

**Apps Script deployment state:**
- Sales: pushed via `./push.sh qa-sales` on 2026-05-11; web app
  deployed; URL captured in `APPS_SCRIPT_WEBAPP_URL_SALES`.
- MS: **not yet pushed**. Per the §"Apps Script" atomicity callout, MS
  push lands during Phase F coordinated with Railway redeploy.

**Railway:**
- Still on a pre-Phase-2 commit (`feedback_railway_isolation` pattern).
  No traffic has hit the new code yet.
- Phase 2 code IS on the branch; redeploy happens during Phase F.

**Outstanding before Phase F cutover:**

1. Phase D (this branch): data-drive `dashboard.html` and
   `datapoint.html`. Audit below.
2. Manual sheet ops in the MS Google Sheet:
   - Rename FR-AI tab from `"QA Scores"` to `"Form Responses AI"`
     (the JSON config change in `8391052` assumes this rename).
   - Delete the `onFormSubmit` installable trigger via Apps Script
     editor → Triggers UI (the function was deleted in `8391052`).
3. Phase F cutover ordering (per §"Cutover plan" below):
   migration scripts already ran for both teams; remaining steps are
   "redeploy Railway → push MS Apps Script → smoke test → flip Sales
   to `live: yes`".

---

## Phase D audit (file:line breakdown for the fresh-session pickup)

Two HTML files hardcode MS section IDs. Both become data-driven via
`/api/{team_id}/sections` (already implemented in Tier 1.2 — returns
`{id, name, section_number, score_type, audio_dependent, na_applicable,
auto_value}` for each section in canonical order).

`team_dashboard.html` is already data-driven (consumes
`binary_stats` + `section_analysis` from `/team/stats`) — use it as the
template/reference for naming conventions.

### `frontend/dashboard.html` — 5 hit sites

| Line(s) | Current hardcode | Replace with |
|---|---|---|
| 334–338 | `const SECTION_KEYS = ['greeting', 'purpose_of_call', …, 'documentation']` — 8 numeric+manual keys in MS rubric order | derive on init: `_numericSectionKeys = sections.filter(s => s.score_type !== 'yn' && !s.auto_value).map(s => s.history_id || s.id)` |
| 339–350 | `const SECTION_LABELS = {greeting: 'Greeting', identity_validation: 'Identity Validation', …}` — 10 keys covering ALL sections (used by assessment cards which iterate `progression.section_assessments` keyed by history_id) | derive: `_sectionLabels = Object.fromEntries(sections.map(s => [s.history_id || s.id, s.name]))` |
| 613 | `SECTION_KEYS.map(key => { vals = history.map(r => parseSectionScore(r.sections[key])).filter(...) })` — bar-chart average per numeric section | unchanged once `SECTION_KEYS` → `_numericSectionKeys` |
| 634 | `labels: SECTION_KEYS.map(k => SECTION_LABELS[k])` — chart x-axis labels | unchanged once both derive from team config |
| 677 | `SECTION_LABELS[key] || key` — title for each assessment card | unchanged |

**Bootstrap pattern** (match what Tier 1.2 added to `index.html`):

```js
let _teamSections = [];
let _sectionLabels = {};
let _numericSectionKeys = [];

async function loadTeamSections() {
  const res = await fetch(`${API_BASE}/sections`, { headers: authHeaders() });
  if (!res.ok) return;
  _teamSections = await res.json();
  _sectionLabels = Object.fromEntries(
    _teamSections.map(s => [s.history_id || s.id, s.name])
  );
  _numericSectionKeys = _teamSections
    .filter(s => s.score_type !== 'yn' && !s.auto_value)
    .map(s => s.history_id || s.id);
}
// Call before any rendering. Page already awaits agent_name from URL; this
// fits naturally at top of DOMContentLoaded.
```

**Note on `history_id` vs `id`:** Sales sections have `history_id === id`
(both snake_case section ids). MS sections differ for some (e.g. sec 8
`efficiency_call_handling` has `history_id: "efficiency"`). The backend
analytics + assessment payloads key by `history_id`. Use
`s.history_id || s.id` consistently in the frontend so MS keeps working.

### `frontend/datapoint.html` — 4 hit sites

| Line(s) | Current hardcode | Replace with |
|---|---|---|
| 372–376 | `const sectionOrder = ['greeting', 'identity_validation', …, 'customer_resolution_indicator']` — 10 history_ids in MS section_number order | derive: `_sectionOrder = _teamSections.map(s => s.history_id || s.id)` (already in canonical section_number order from the endpoint) |
| 377–383 | `const sectionLabels = {…}` — same 10 history_id → display label | same as dashboard.html: `_sectionLabels = Object.fromEntries(...)` |
| 386 | `for (const key of sectionOrder)` loop body unchanged | iterates `_sectionOrder` |
| 390 | `confLabel = sec.confidence \|\| (key === 'documentation' ? 'MANUAL' : '')` — hardcoded MS-only check | derive: `const sd = _teamSectionsByKey[key]; confLabel = sec.confidence \|\| (sd && sd.score_type === 'manual' ? 'MANUAL' : (sd && sd.auto_value ? 'AUTO' : ''))` |

Build the by-key lookup once at init:

```js
let _teamSectionsByKey = {};
// after loadTeamSections:
_teamSectionsByKey = Object.fromEntries(
  _teamSections.map(s => [s.history_id || s.id, s])
);
```

The `'AUTO'` badge for `auto_value` sections (Sales screen_recording)
is a small UX polish over today's "blank confidence" rendering; reasonable
to include in Phase D, easy to drop if it clutters.

### What does NOT change

- `team_dashboard.html` — already data-driven; no edits.
- The backend `/api/{team_id}/sections` endpoint — already done in Tier
  1.2; this Phase D work just consumes it.
- The progression assessment / section-analysis backend responses — they
  already key by `history_id` consistently per-team.
- URL routing — both files already team-aware via
  `TEAM_ID = (location.pathname.match(...) || [, 'member_support'])[1]`.

### Phase D testing

- MS path: visit `/dashboard/member_support/agent/<name>` — should
  render exactly as today (no MS regression). Bar chart shows 8 numeric
  sections, assessment cards iterate all 10.
- Sales path: visit `/dashboard/sales/agent/<name>` — bar chart should
  show 6 sections (4 AI numeric + 2 manual — pb_creation +
  mc_call_notes); assessment cards iterate however many
  `section_assessments` the backend returned (depends on whether
  progression service has data for migrated rows).
- Datapoint MS: `/datapoint/member_support/<call_id>` — 10-row table,
  Documentation row shows "MANUAL" badge.
- Datapoint Sales: `/datapoint/sales/<call_id>` — 19-row table,
  `pb_creation` and `mc_call_notes` rows show "MANUAL", `screen_recording`
  shows "AUTO" (or blank).

Caveat for Sales smoke: progression service may not produce useful
assessment data until enough non-migrated rows accumulate. Acceptable
empty state for cutover — Phase D's job is rendering the shell, not
fixing analytics with sparse data.

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

> **⚠ Atomicity constraint — read before opening the Phase C PR.**
>
> Phase C must touch the Python writer **and** the Apps Script `doPost` source-of-truth
> tab in the same PR. Splitting them stops MS production emails for hours/days
> with no exception — the failure mode is silent (emails just don't go out).
>
> **Today's wiring** (verified 2026-05-10):
>
> - `backend/services/sheets_service.py` `trigger_apps_script(dest_row_num, team_id)`
>   posts `{"rowNumber": <score-destination row>}` to the team's web app.
>   The route handler in `backend/routes/scoring.py` `approve_scorecard()` passes
>   `dest_row` (the Form Responses 1 / Scores tab row from Stage 2) — not
>   `history_row` (the Analyst_History row from Stage 4).
> - `qa-automation/src/Main.js` `doPost(e)` reads `payload.rowNumber`, then
>   `sheet.getRange(rowNumber, 1, 1, 22).getValues()[0]` from
>   `CONFIG.QA_SHEET_NAME` (= `"Form Responses 1"`). The 22-col read width is
>   FR1-shaped (cols A–V); Sales' Scores tab is wider (A–Y) and Analyst_History
>   is wider still — so changing the source tab without updating the read width
>   produces silent zero-padded reads.
>
> **What Phase C must change atomically:**
>
> 1. `routes/scoring.py` — pass `history_row` to `trigger_apps_script` instead of
>    `dest_row`. (Both values already exist; just swap.)
> 2. `sheets_service.py` `trigger_apps_script` docstring — drop the "Phase B (a)"
>    note that says it still passes the destination-tab row.
> 3. `qa-automation/src/Main.js` `doPost` — change `CONFIG.QA_SHEET_NAME` lookup
>    to read from `Analyst_History` (likely via a new `CONFIG.HISTORY_SHEET_NAME`
>    in the generated `Config.js`).
> 4. `Main.js` row-read width — drop the hard `22` and use the team's
>    `HistoryLayout(N).total_width` (also surfaced via the generated `Config.js`).
> 5. `qa-automation/src/QAEntry.js` constructor — accept the new layout
>    (currently consumes FR1's column shape via `CONFIG.COL`; needs to consume
>    `CONFIG.HISTORY_LAYOUT` positions instead). The AnalystHistory.js refactor
>    (below) is the natural place to land this together.
>
> **PR description should include this as a top-line callout** so a reviewer
> can verify both ends of the wire moved together. Two short merges in
> sequence will break MS — the constraint is "all in one PR or nothing".

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

`scripts/migrate_ms_history.py`. Reads from `Analyst_History_legacy` (post-rename),
writes to a fresh `Analyst_History` tab using the derived layout
(`HistoryLayout(N=10)`, total width 42 cols A–AP).

**Source columns** are 0-based and reference the legacy MS layout codified in
`qa-automation/teams/member_support/Config.js` `HISTORY_COL`. **Destination
columns** are 0-based positions returned by the `HistoryLayout` helpers. Section
indices follow `section_number` order:

| `section_number` | section_id (history_id) | layout idx |
|---|---|---|
| 1 | greeting | 0 |
| 2 | caller_identity_validation (`identity_validation`) | 1 |
| 3 | purpose_of_call | 2 |
| 4 | matching_the_moment | 3 |
| 5 | process_adherence | 4 |
| 6 | call_resolution | 5 |
| 7 | communication | 6 |
| 8 | efficiency_call_handling (`efficiency`) | 7 |
| 9 | documentation | 8 |
| 10 | customer_resolution_indicator | 9 |

**Per-column mapping** (legacy → new):

| Old col | Old name (HISTORY_COL.\*)         | New col | New target                              |
|--------:|-----------------------------------|--------:|-----------------------------------------|
| 0       | AGENT_NAME                        | 0       | agent_name                              |
| 1       | AGENT_EMAIL                       | 1       | agent_email                             |
| 2       | TIMESTAMP                         | 2       | timestamp                               |
| 14      | MANAGER_EMAIL                     | 3       | evaluator_email (renamed)               |
| 15      | DIALPAD_LINK                      | 4       | dialpad_link                            |
| 3       | OVERALL_SCORE                     | 5       | overall_score                           |
| 4       | GREETING                          | 6       | `L.col_score(0)` greeting               |
| 12      | IDENTITY_VAL                      | 7       | `L.col_score(1)` identity_validation    |
| 5       | CALL_PURPOSE                      | 8       | `L.col_score(2)` purpose_of_call        |
| 6       | MATCH_MOMENT                      | 9       | `L.col_score(3)` matching_the_moment    |
| 7       | PROCESS_ADHERENCE                 | 10      | `L.col_score(4)` process_adherence      |
| 8       | CALL_RESOLUTION                   | 11      | `L.col_score(5)` call_resolution        |
| 9       | COMMUNICATION                     | 12      | `L.col_score(6)` communication          |
| 10      | EFFICIENCY                        | 13      | `L.col_score(7)` efficiency             |
| 11      | DOCUMENTATION                     | 14      | `L.col_score(8)` documentation          |
| 13      | CUSTOMER_RES                      | 15      | `L.col_score(9)` customer_resolution    |
| 20      | GREETING_REASON                   | 16      | `L.col_reasoning(0)`                    |
| 22      | IDENTITY_REASON                   | 17      | `L.col_reasoning(1)`                    |
| 24      | PURPOSE_REASON                    | 18      | `L.col_reasoning(2)`                    |
| 26      | MATCHING_REASON                   | 19      | `L.col_reasoning(3)`                    |
| 28      | PROCESS_REASON                    | 20      | `L.col_reasoning(4)`                    |
| 30      | RESOLUTION_REASON                 | 21      | `L.col_reasoning(5)`                    |
| 32      | COMMUNICATION_REASON              | 22      | `L.col_reasoning(6)`                    |
| 34      | EFFICIENCY_REASON                 | 23      | `L.col_reasoning(7)`                    |
| 41      | DOC_REASON                        | 24      | `L.col_reasoning(8)`                    |
| 36      | CUSTOMER_RES_REASON               | 25      | `L.col_reasoning(9)`                    |
| 19      | GREETING_CONF                     | 26      | `L.col_confidence(0)`                   |
| 21      | IDENTITY_CONF                     | 27      | `L.col_confidence(1)`                   |
| 23      | PURPOSE_CONF                      | 28      | `L.col_confidence(2)`                   |
| 25      | MATCHING_CONF                     | 29      | `L.col_confidence(3)`                   |
| 27      | PROCESS_CONF                      | 30      | `L.col_confidence(4)`                   |
| 29      | RESOLUTION_CONF                   | 31      | `L.col_confidence(5)`                   |
| 31      | COMMUNICATION_CONF                | 32      | `L.col_confidence(6)`                   |
| 33      | EFFICIENCY_CONF                   | 33      | `L.col_confidence(7)`                   |
| 40      | DOC_CONF                          | 34      | `L.col_confidence(8)`                   |
| 35      | CUSTOMER_RES_CONF                 | 35      | `L.col_confidence(9)`                   |
| 16      | KEY_STRENGTHS                     | 36      | key_strengths                           |
| 17      | IMPROVEMENTS                      | 37      | opportunities (renamed)                 |
| 37      | CALL_SUMMARY                      | 38      | call_summary                            |
| 38      | CALLER_NAME                       | 39      | caller_name                             |
| 39      | CALLER_PHONE                      | 40      | caller_phone                            |
| 18      | SOURCE                            | 41      | source                                  |

**Verification gates** (script bails on any mismatch):

1. Row counts match between legacy and new tabs.
2. For a 10-row sample: `overall_score` matches and section scores at indices
   0–9 match the legacy column values (validates the section-reorder mapping).
3. Reasoning/confidence pair re-pairing — for a 5-row sample, walk
   `[L.col_reasoning(i), L.col_confidence(i)]` and verify the cell content
   matches the legacy `(REASON, CONF)` pair from the section's old position
   (catches off-by-one in the un-pair → re-pair step).

### Sales — 243-row import from FR3 → new Analyst_History

`scripts/import_sales_history.py`. Reads FR3 (A–AA, 244 rows incl header),
writes to a fresh Sales `Analyst_History` tab using `HistoryLayout(N=19)`,
total width 69 cols A–BQ.

**Section index alignment.** Sales is fortunate: FR3's section columns D–V are
already in `section_number` order, so D maps to layout idx 0 through V mapping
to idx 18. No reorder within the score range required.

| `section_number` | section_id     | FR3 col | layout idx | new col |
|---:|------------------------|--------:|-----------:|--------:|
| 1  | greeting               | D       | 0          | 6       |
| 2  | pb_creation (manual)   | E       | 1          | 7       |
| 3  | mc_call_notes (manual) | F       | 2          | 8       |
| 4  | situation_match        | G       | 3          | 9       |
| 5  | reason_for_move_pitch  | H       | 4          | 10      |
| 6  | value_uplift           | I       | 5          | 11      |
| 7  | membership_explanation | J       | 6          | 12      |
| 8  | flex_long_stay_pitch   | K       | 7          | 13      |
| 9  | landing_guarantee      | L       | 8          | 14      |
| 10 | pricing_explanation    | M       | 9          | 15      |
| 11 | book_attempt           | N       | 10         | 16      |
| 12 | objection_handling     | O       | 11         | 17      |
| 13 | urgency_disclosure     | P       | 12         | 18      |
| 14 | followup_setup         | Q       | 13         | 19      |
| 15 | tonality_pace          | R       | 14         | 20      |
| 16 | hold_usage             | S       | 15         | 21      |
| 17 | audio_quality          | T       | 16         | 22      |
| 18 | screen_recording       | U       | 17         | 23      |
| 19 | pre_send_intro         | V       | 18         | 24      |

> Note for Q18 (`screen_recording`): the live config has `auto_value: "Yes"` so
> Stage 1 hardcodes "Yes" for new evaluations, but the migration **preserves
> the historical FR3 col U value verbatim** — old rows reflect what was
> actually scored at the time, not the new fixed value.

**Non-section columns** (FR3 → new layout):

| FR3 col | Source field          | New col | New target                                  |
|--------:|-----------------------|--------:|---------------------------------------------|
| A       | dialpad_link          | 4       | dialpad_link                                |
| B       | call_date             | 2       | timestamp (primary; backfill from Y or Dialpad if blank) |
| C       | agent_name            | 0       | agent_name                                  |
| —       | (Mails lookup on C)   | 1       | agent_email                                 |
| W       | evaluator_name/email  | 3       | evaluator_email                             |
| X       | combined feedback     | 64      | `L.col_opportunities` (key_strengths blank) |
| Y       | submission_timestamp  | 2       | timestamp (fallback if B blank)             |
| Z       | agent_email formula   | —       | ignore (resolved via Mails)                 |
| AA      | derived month         | —       | ignore                                      |

**Cells left blank** for migrated rows (no AI ran):

- `agent_email` (col 1) if Mails lookup misses — log + warn (see follow-up §3.4)
- `overall_score` (col 5) — re-derive from sheet formula or leave blank;
  analytics filter on `source = 'migrated'` is the safety net
- All reasoning cells `[L.col_reasoning(0..18)]` (cols 25–43)
- All confidence cells `[L.col_confidence(0..18)]` (cols 44–62)
- `key_strengths` (col 63) — combined feedback lands in `opportunities` only
- `call_summary` (col 65), `caller_name` (col 66), `caller_phone` (col 67)

**Constants written for every migrated row:**

- `source` (col 68) = `"migrated"`

**Date backfill (FR3 col B blank cases):**

1. Use FR3 col Y (form-submission timestamp) if present.
2. Otherwise call Dialpad `get_call_details(call_id)` parsed from FR3 col A.
3. Rate-limit: 5 calls/sec; 243 rows worst case ≈ 1 minute. Cache responses
   keyed on `call_id` to allow resumable runs.

**Verification gates:**

1. Row count is within ±20 of the documented ~243 (FR3 drifts as duplicates
   are cleaned up — exact match isn't required, just a sanity check against
   "wrong sheet" / "lost half the data").
2. Sample 10 random rows: section scores at layout idx 0–18 match FR3 cols D–V.
3. `source` column on every written row equals `"migrated"`.
4. Mails resolution rate logged; abort if < 70 % match (likely indicates a
   wrong `Mails` tab connection, not just stale evaluators).

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

#### 1.1 Migration script mapping tables (Phase E prerequisite) — ✅ RESOLVED 2026-05-10
**Resolution:** §"Migration" above now contains explicit per-column mapping
tables for both migrations (MS in-place reorder + Sales FR3 import), plus
section-index alignment tables and verification gates. A Phase E session can
implement the scripts directly from those tables without re-deriving the
layout.

#### 1.2 Manual-section dashboard input for Sales — ✅ RESOLVED 2026-05-10
**Resolution:** Pulled the frontend fix forward from Phase D scope.
- New endpoint `GET /api/{team_id}/sections` (`backend/routes/team.py`) returns
  the team's section list (id, name, section_number, score_type,
  audio_dependent, na_applicable, auto_value).
- `frontend/index.html` `buildScorecardPanel` now walks `_teamSections` in
  canonical order, rendering AI cards for AI-scored sections and manual-input
  cards for `score_type === "manual"`. `auto_value` sections are skipped.
- `checkManualScores` replaces `checkDocScore`; the Approve button stays
  disabled until **all** manual sections have a score selected, with status
  text naming the missing sections (e.g. Sales: "Score PB Created + MC Call
  Notes to enable Approve & Send.").
- Approve payload iterates manual sections from `_teamSections` instead of
  hardcoding the `'documentation'` push.
- Out of scope (still Phase D): `dashboard.html` and `datapoint.html`
  hardcoded `SECTION_KEYS` / `sectionOrder` / `'documentation'` literals.

#### 1.3 Apps Script trigger flip must be atomic (Phase C) — ✅ DOCUMENTED 2026-05-10
**Resolution:** §"Apps Script — generated Config.js, multi-team overlay,
push.sh" above now opens with a ⚠ atomicity callout enumerating the five
specific files Phase C must touch in a single PR (routes/scoring.py,
sheets_service.py, Main.js, Main.js read width, QAEntry.js). The actual code
flip lands as part of Phase C work; this entry stays in the doc as the
landmine warning a future reader will hit before opening the PR.

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

#### 4.6 Phase-C bridge cleanup
**Status:** Phase C ships a transitional payload bridge so Apps Script and Railway can
deploy in either order without producing wrong-row emails during the deploy window.

- Python (`sheets_service.trigger_apps_script`) sends both
  `{historyRowNumber, rowNumber}`.
- Apps Script `doPost` prefers `historyRowNumber` (new Analyst_History path); falls
  back to `rowNumber` (legacy Form Responses 1 path via `_processRow` →
  `AnalystHistory.append` → enrichment lookup).
- The legacy code paths kept alive solely for this fallback:
  - `Main.js` — the `else` branch in `doPost` that calls `_processRow(row)`
  - `Main.js` — `_processRow`, `processLatestRow`, `createDraftForLatest`,
    `rebuildHistory`, `_getLatestRow`
  - `QAEntry.js` — the FR1-shape `constructor(row)` and `toHistoryRow()`
  - `AnalystHistory.js` — `append`, `enrichEntry`, `_lookupEnrichment`,
    `_writeEnrichment`, `_attachToEntry`, `_getOrCreateSheet`
  - `member_support/Config.js` — `NUMERIC_CATEGORIES[*].col`,
    `BINARY_CATEGORIES[*].col`, `MANUAL_CATEGORIES[*].col` (the destination-tab
    column index used only by the legacy QAEntry constructor)
  - `scripts/build_config.py` — the `_render_legacy_compat` block that
    emits `CONFIG.COL`, `CONFIG.FORM_AI_COL`, `CONFIG.HISTORY_COL`, and
    `CONFIG.HISTORY_EXTENDED_LAYOUT` (the MS-specific hand-coded literals
    in particular)

**Silent failure if not cleaned up:** Maintenance burden — every future change to the
new path also has to consider whether the legacy fallback still makes sense; new
contributors get confused by parallel implementations of the same flow.

**Where to start, after MS+Sales cutover is verified live for at least one week:**
1. Drop `rowNumber` from `trigger_apps_script`'s payload (`sheets_service.py`); update
   docstring.
2. Drop the `else` (legacy) branch from `Main.js doPost`. Delete `_processRow`,
   `processLatestRow`, `createDraftForLatest`, `rebuildHistory`, `_getLatestRow`,
   `_handleError`. Adjust `onOpen()` (no menu items left → can also be deleted).
3. Trim `QAEntry.js` to keep only `fromHistoryRow` + the static helpers + the
   formatted-date getter + the static color/name helpers.
4. Trim `AnalystHistory.js` to keep only `getHistory` + a minimal sheet-getter.
5. Drop the `col` field from generated `NUMERIC_CATEGORIES` / `BINARY_CATEGORIES` /
   `MANUAL_CATEGORIES` in `build_config.py`; delete `_render_legacy_compat`
   (and its call site in `render_config_js`); regenerate every team's `Config.js`.
6. Manually delete the `onFormSubmit` installable trigger in each team's Apps Script
   editor (Triggers UI) — was meant to be done at cutover but easy to forget.

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
