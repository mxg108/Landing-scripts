# Agent Progression Dashboard — Design Document

> Reference doc for adding per-agent progression tracking, trend visualization,
> and Gemini-powered coaching assessments to the QA AI-Scoring pipeline.

---

## Current State (as of 2026-03-23)

### What exists

**Phase 1 scoring pipeline** (FastAPI, in `qa-automation/AI-Scoring/`):

| Component | File | What it does |
| --- | --- | --- |
| FastAPI app | `backend/main.py` | Entry point, mounts scoring router, serves `frontend/index.html` |
| Scoring routes | `backend/routes/scoring.py` | `POST /api/score`, `GET /api/score/{job_id}`, `POST /api/score/batch`, `GET /api/calls` |
| Scoring service | `backend/services/scoring_service.py` | Sends audio to Gemini 2.5 Flash, returns `ScorecardWithMeta` |
| Sheets writer | `backend/services/sheets_service.py` | Appends scored row to `Form Responses AI` tab, inserts cell notes with reasoning |
| Dialpad client | `backend/services/dialpad_client.py` | Looks up agents + call lists from Dialpad API |
| Models | `backend/models/scorecard.py` | `ScorecardSection`, `Scorecard`, `ScorecardWithMeta` |
| Frontend | `frontend/index.html` | Upload UI — drag-and-drop audio, enter agent/manager, view scorecard results |

**Apps Script QA email system** (in `qa-automation/src/`):

| Component | File | What it does |
| --- | --- | --- |
| Config | `Config.js` | Column mappings for both sheets, thresholds, brand colors |
| QAEntry | `QAEntry.js` | Parses a Form Responses 1 row into a structured object, `toHistoryRow()` outputs 15 values |
| AnalystHistory | `AnalystHistory.js` | `append(entry)` writes 15 cols A-O, `getHistory()` reads them back |
| Main | `Main.js` | `_processRow()` pipeline: parse entry -> get history -> append -> build email -> send |
| ScoreCard | `ScoreCard.js` | HTML card with per-section score bars |
| ProgressionCard | `ProgressionCard.js` | HTML card with historical overall scores, delta arrows, mini bars |
| EmailSender | `EmailSender.js` | Sends the HTML email to the agent |

### The data flow today

```text
[Manager submits QA form]
        |
        v
[Form Responses 1]  (cols A-P, formula in Q = Overall Score, V = agent email lookup)
        |
        |--- Manager copy-pastes from Form_Responses_AI
        |
        v
[onFormSubmit / UI button]
        |
        v
_processRow(row)
  1. new QAEntry(row)           — parses cols A-V from Form Responses 1
  2. history.getHistory(name)   — reads Analyst_History (A-O)
  3. history.append(entry)      — writes 15 values (A-O) via toHistoryRow()
  4. Build ScoreCard + FeedbackCard + ProgressionCard
  5. Send email to agent
```

### Form Responses AI column layout (A-P)

The **only** sheet the AI scoring pipeline writes to:

| Col | Field | Notes |
| --- | --- | --- |
| A | Timestamp | UTC, `MM/DD/YYYY HH:MM:SS` |
| B | Manager Email | |
| C | Agent Name | |
| D | Greeting | 1-5 |
| E | Caller Identity Validation | Yes / No / Not Applicable |
| F | Purpose of the Call | 1-5 |
| G | Matching the Moment | 1-5 |
| H | Process Adherence | 1-5 |
| I | Call Resolution | 1-5 |
| J | Communication | 1-5 |
| K | Efficiency & Call Handling | 1-5 |
| L | Documentation | Always `1` (manual only) |
| M | Customer Resolution Indicator | Yes / No / Not Applicable |
| N | Key Strengths | Free text |
| O | Opportunities for Improvement | Free text |
| P | Dialpad Link | URL, may include `[LONG CALL]` flag |

AI reasoning is currently stored as **cell notes** on columns D-K and M (via `insert_note()`), not as column values.

### Analyst_History column layout (A-O) — current

Written by `QAEntry.toHistoryRow()` via `AnalystHistory.append()`:

| Col | Index | Field | `HISTORY_COL` key |
| --- | --- | --- | --- |
| A | 0 | Agent Name | `AGENT_NAME` |
| B | 1 | Agent Email | `AGENT_EMAIL` |
| C | 2 | Timestamp | `TIMESTAMP` |
| D | 3 | Overall Score | `OVERALL_SCORE` |
| E | 4 | Greeting | `GREETING` |
| F | 5 | Purpose of the Call | `CALL_PURPOSE` |
| G | 6 | Matching the Moment | `MATCH_MOMENT` |
| H | 7 | Process Adherence | `PROCESS` |
| I | 8 | Call Resolution | `RESOLUTION` |
| J | 9 | Communication | `COMMUNICATION` |
| K | 10 | Efficiency & Call Handling | `EFFICIENCY` |
| L | 11 | Documentation | `DOCUMENTATION` |
| M | 12 | Identity Validation (Y/N) | `IDENTITY_VAL` |
| N | 13 | Customer Resolution (Y/N) | `CUSTOMER_RES` |
| O | 14 | Manager Email | `MANAGER_EMAIL` |

**Note:** Column order differs from Form Responses AI. Analyst_History puts Agent Name first and Overall Score at col D; Form Responses AI puts Timestamp first with no Overall Score column (it's formula-driven in col Q).

### What does NOT exist yet

- No reasoning columns in either sheet — reasoning is trapped in cell notes
- No dashboard UI for viewing agent trends over time
- No per-section trend tracking with reasoning
- No Gemini-based coaching assessments across evaluations
- No PostgreSQL fallback for when Railway DB is available

---

## Goal

Build a dynamic agent progression dashboard that:

1. Auto-populates agent list from Analyst_History
2. Shows overall and per-section score trends with charts (7/14/30/60/90-day windows)
3. Uses Gemini to generate coaching assessments from historical evaluations + reasoning
4. Uses Google Sheets (Analyst_History) as primary data source, PostgreSQL as optional accelerator
5. Integrates into the existing FastAPI app alongside the scoring pipeline

---

## Architecture

```text
[Form Responses AI]               [Analyst_History]           [PostgreSQL]
  Cols A-P + cell notes             Cols A-O (existing)        qa_scoring schema
  + new cols Q+ (reasoning)         + new cols P+ (lookup)     (optional fallback)
  (scoring pipeline writes)         (populated by append())
         \                               |                    /
          \______________________________|___________________/
                                         |
                               [DataProvider abstraction]
                                         |
                                  [FastAPI Backend]
                                 /       |        \
                        /api/agents  /history  /progression
                                                 |
                                          [Gemini 2.5 Flash]
                                                 |
                                     [Progression Assessment]
                                                 |
                                       [dashboard.html]
                                       Chart.js + tiles
```

---

## Part 1: Extend Form Responses AI (Python — sheets_service.py)

The AI scoring pipeline currently writes 16 columns (A-P) and stores reasoning as cell notes. We need reasoning as actual column values so Analyst_History can pull them via lookup.

### New columns in Form Responses AI (Q+)

| Col | Field | Content |
| --- | --- | --- |
| Q | Source | `"ai"` (always, since this is the AI pipeline sheet) |
| R | Greeting Confidence | high / medium / low |
| S | Greeting Reasoning | Free text |
| T | Identity Validation Confidence | |
| U | Identity Validation Reasoning | |
| V | Purpose of Call Confidence | |
| W | Purpose of Call Reasoning | |
| X | Matching the Moment Confidence | |
| Y | Matching the Moment Reasoning | |
| Z | Process Adherence Confidence | |
| AA | Process Adherence Reasoning | |
| AB | Call Resolution Confidence | |
| AC | Call Resolution Reasoning | |
| AD | Communication Confidence | |
| AE | Communication Reasoning | |
| AF | Efficiency Confidence | |
| AG | Efficiency Reasoning | |
| AH | Customer Resolution Confidence | |
| AI | Customer Resolution Reasoning | |

9 sections x 2 cols = 18 + 1 source col = **19 new columns (Q-AI)**.

**Change:** Modify `append_scorecard_row()` in `sheets_service.py` to write these columns alongside the existing A-P row, so reasoning lives in columns AND cell notes (notes kept for backward compat with Form_Responses_1 copy-paste workflow).

---

## Part 2: Extend Analyst_History (Apps Script — AnalystHistory.js)

### New columns in Analyst_History (P+)

After `append(entry)` writes the existing 15 values (A-O), a new method looks up the matching row in Form Responses AI by Dialpad link and copies reasoning data into P+.

| Col | Index | Field | Source |
| --- | --- | --- | --- |
| P | 15 | Dialpad Link | QAEntry.dialpadLink (from Form Responses 1 col P) |
| Q | 16 | Key Strengths | Lookup from Form_Responses_AI col N (matched by Dialpad link) |
| R | 17 | Improvements | Lookup from Form_Responses_AI col O |
| S | 18 | Source | Lookup from Form_Responses_AI col Q |
| T | 19 | Greeting Confidence | Lookup from Form_Responses_AI col R |
| U | 20 | Greeting Reasoning | Lookup from Form_Responses_AI col S |
| V | 21 | Identity Validation Confidence | Lookup from Form_Responses_AI col T |
| W | 22 | Identity Validation Reasoning | Lookup from Form_Responses_AI col U |
| X | 23 | Purpose of Call Confidence | Lookup from Form_Responses_AI col V |
| Y | 24 | Purpose of Call Reasoning | Lookup from Form_Responses_AI col W |
| Z | 25 | Matching the Moment Confidence | Lookup from Form_Responses_AI col X |
| AA | 26 | Matching the Moment Reasoning | Lookup from Form_Responses_AI col Y |
| AB | 27 | Process Adherence Confidence | Lookup from Form_Responses_AI col Z |
| AC | 28 | Process Adherence Reasoning | Lookup from Form_Responses_AI col AA |
| AD | 29 | Call Resolution Confidence | Lookup from Form_Responses_AI col AB |
| AE | 30 | Call Resolution Reasoning | Lookup from Form_Responses_AI col AC |
| AF | 31 | Communication Confidence | Lookup from Form_Responses_AI col AD |
| AG | 32 | Communication Reasoning | Lookup from Form_Responses_AI col AE |
| AH | 33 | Efficiency Confidence | Lookup from Form_Responses_AI col AF |
| AI | 34 | Efficiency Reasoning | Lookup from Form_Responses_AI col AG |
| AJ | 35 | Customer Resolution Confidence | Lookup from Form_Responses_AI col AH |
| AK | 36 | Customer Resolution Reasoning | Lookup from Form_Responses_AI col AI |

**Total: 37 columns (A-AK).**

### How the lookup works

1. `append(entry)` writes 15 values (A-O) as it does today — unchanged
2. New: `toHistoryRow()` is extended to also include `dialpadLink` at position 15 (col P)
3. New: after `appendRow()`, a method `_enrichFromFormResponsesAI(rowNum, dialpadLink)` runs:
   - Opens `Form Responses AI` tab
   - Searches col P (Dialpad Link) for a matching URL
   - If found, reads that row's cols N, O, Q-AI (strengths, improvements, source, 9x confidence+reasoning)
   - Writes those values to Analyst_History cols Q-AK of the just-appended row
4. If no match found (manual QA, not AI-scored), cols Q-AK stay empty — zero breakage

### Files to modify (Apps Script)

**`AnalystHistory.js`:**
- `append(entry)` — after `appendRow()`, call `_enrichFromFormResponsesAI()`
- `_enrichFromFormResponsesAI(rowNum, dialpadLink)` — new private method
- `_getOrCreateSheet()` — add new headers for cols P-AK
- `getHistory()` — extend to also read cols P-AK into the returned objects

**`QAEntry.js`:**
- `toHistoryRow()` — add `this.dialpadLink` as position 15 (col P)

**`Config.js`:**
- `HISTORY_COL` — add new indices: `DIALPAD_LINK: 15`, `KEY_STRENGTHS: 16`, `IMPROVEMENTS: 17`, `SOURCE: 18`, then section confidence/reasoning pairs 19-36
- `FORM_AI_SHEET_NAME: 'Form Responses AI'` — new config entry
- `FORM_AI_COL` — new column mapping for the Form Responses AI extended columns

### Zero-breakage guarantees

- `toHistoryRow()` returns 16 values now (added dialpadLink) instead of 15 — `appendRow()` handles variable-length arrays
- `getHistory()` still reads indices 0-14 for existing callers; new fields are additive
- If `Form Responses AI` tab doesn't exist or has no extended columns, `_enrichFromFormResponsesAI()` silently skips
- Manual QAs (not AI-scored) simply have empty P+ columns
- The email pipeline (`ScoreCard`, `ProgressionCard`, `EmailSender`) reads from `getHistory()` — existing fields unchanged

---

## Part 3: Dashboard Backend (Python — new files)

### Files to create

| # | File | Depends on | What |
| --- | --- | --- | --- |
| 1 | `backend/models/dashboard.py` | — | `EvaluationRecord`, `SectionScore`, `ProgressionAssessment`, `SectionAssessment` |
| 2 | `backend/services/data_provider.py` | #1 | Abstract `DataProvider` + `get_provider()` factory |
| 3 | `backend/services/history_service.py` | #1, #2 | `SheetsProvider` — reads Analyst_History (A-AK) |
| 4 | `backend/services/db_provider.py` | #1, #2 | `PostgresProvider` — asyncpg, 3s timeout |
| 5 | `backend/prompts/progression_prompt.py` | #1 | Gemini prompt template |
| 6 | `backend/services/progression_service.py` | #2, #5 | Calls Gemini, 1-hour TTL cache |
| 7 | `backend/routes/dashboard.py` | #3, #4, #6 | API endpoints |
| 8 | `backend/main.py` (modify) | #7 | Add dashboard router + `/dashboard` route |

### API endpoints

```text
GET  /api/agents                            -> list of agent names
GET  /api/agents/{name}/history?days=30     -> evaluation records with scores + reasoning
GET  /api/agents/{name}/progression?days=30 -> Gemini progression assessment JSON
```

### SheetsProvider — Analyst_History column mapping

The provider reads Analyst_History using `GOOGLE_HISTORY_TAB` env var. Column mapping must match the History layout (A=Agent Name, not A=Timestamp):

| Index | Field | Notes |
| --- | --- | --- |
| 0 | Agent Name | |
| 1 | Agent Email | |
| 2 | Timestamp | |
| 3 | Overall Score | |
| 4-11 | Section scores | Greeting through Documentation |
| 12-13 | Binary checks | Identity Validation, Customer Resolution |
| 14 | Manager Email | |
| 15 | Dialpad Link | new |
| 16 | Key Strengths | new (from lookup) |
| 17 | Improvements | new (from lookup) |
| 18 | Source | new (from lookup) |
| 19-36 | 9x (Confidence, Reasoning) | new (from lookup) |

### Gemini progression prompt

- Temperature: 0.3, model: gemini-2.5-flash
- Input: agent name, serialized evaluations (scores + reasoning + strengths/improvements), time window
- Output: JSON with `overall_assessment` + 9 `section_assessments` each with `trend`/`summary`/`coaching_tip`
- Even with 1 evaluation, provides general coaching (no minimum threshold)
- Uses `google.genai` SDK (not deprecated `google.generativeai`)

---

## Part 4: Dashboard Frontend

**File:** `frontend/dashboard.html` (new — separate from `index.html`)

Single HTML file, no build step. Chart.js via CDN.

**Layout:**

- Navy header with agent selector dropdown + time range buttons (7d / 14d / 30d / 60d / 90d)
- Stats row: evaluation count, average score, latest score, highest score
- **Overall Score Trend** — Chart.js line chart, colored by threshold, 85% goal line (4.25/5)
- **Per-Section Averages** — Chart.js bar chart comparing section averages within time window
- **AI Progression Assessment** — text card with overall assessment + expandable per-section coaching tips with trend badges (improving/stable/declining)
- Footer: data source indicator ("Data source: Google Sheets" or "Data source: PostgreSQL")

**Styling:** Matches existing `index.html` palette — `--navy: #15192D`, `--accent: #1A61D9`, `--bg: #E7EFFB`. DM Mono + Fraunces fonts via Google Fonts.

---

## Part 5: Backfill + Integration

### Backfill script (`scripts/backfill_reasoning.py`)

One-time script for existing data:

1. Reads Form Responses AI (including cell notes via Sheets API)
2. Matches to Analyst_History rows by agent name + timestamp
3. Writes reasoning + strengths/improvements into extended columns
4. `--dry-run` flag, marks rows with `source = "backfilled"`

### Integration with scoring pipeline

Modify `append_scorecard_row()` in `sheets_service.py` to also write reasoning as column values (Q-AI) alongside the existing cell notes. This means future Analyst_History rows get enriched automatically via the lookup in `_enrichFromFormResponsesAI()`.

---

## Section Name Mapping

The codebase uses different naming conventions across Apps Script and Python:

| Display Name | `Config.js` key | `ScorecardSection.id` (Python) | Form_Responses_AI col |
| --- | --- | --- | --- |
| Greeting | `greeting` | `greeting` | D |
| Caller Identity Validation | `identityValidation` | `caller_identity_validation` | E |
| Purpose of the Call | `callPurpose` | `purpose_of_call` | F |
| Matching the Moment | `matchMoment` | `matching_the_moment` | G |
| Process Adherence | `processAdherence` | `process_adherence` | H |
| Call Resolution | `callResolution` | `call_resolution` | I |
| Communication | `communication` | `communication` | J |
| Efficiency & Call Handling | `efficiency` | `efficiency_call_handling` | K |
| Documentation | `documentation` | (not scored) | L |
| Customer Resolution Indicator | `customerResolution` | `customer_resolution_indicator` | M |

The DataProvider must normalize these when reading from Analyst_History.

---

## Parallelizable Work Units

These can be assigned to subagents working concurrently:

### Unit A: Form Responses AI extension (Python)

**Scope:** `backend/services/sheets_service.py`
**Task:** Extend `append_scorecard_row()` to write reasoning columns Q-AI alongside existing A-P.
**Test:** After scoring a call, verify Form Responses AI has values in cols Q-AI (not just cell notes).
**Depends on:** Nothing (existing file, additive change).

### Unit B: Apps Script extension (JavaScript)

**Scope:** `Config.js`, `QAEntry.js`, `AnalystHistory.js`
**Task:**

- Add `FORM_AI_SHEET_NAME` and `FORM_AI_COL` to Config
- Add extended `HISTORY_COL` entries (15-36) to Config
- Extend `toHistoryRow()` to include `dialpadLink` at index 15
- Add `_enrichFromFormResponsesAI(rowNum, dialpadLink)` to AnalystHistory
- Extend `_getOrCreateSheet()` headers for cols P-AK
- Extend `getHistory()` to read new columns

**Test:** Run `rebuildHistory()` from the Apps Script menu. Verify Analyst_History rows now have cols P-AK populated for AI-scored calls. Verify manual QAs have empty P+ cols. Verify email still sends correctly.
**Depends on:** Unit A must be done first (Form Responses AI needs extended columns for the lookup to find data).

### Unit C: Dashboard data models + data provider (Python)

**Scope:** `backend/models/dashboard.py`, `backend/services/data_provider.py`, `backend/services/history_service.py`, `backend/services/db_provider.py`
**Task:** Create Pydantic models, abstract DataProvider, SheetsProvider (reads Analyst_History A-AK), PostgresProvider.
**Test:** Write a standalone test script that instantiates SheetsProvider, calls `list_agents()` and `get_agent_history("SomeAgent", 30)`, verifies results include reasoning fields.
**Depends on:** Unit B (Analyst_History must have extended columns to read from). Can stub/mock for development.

### Unit D: Gemini progression service (Python)

**Scope:** `backend/prompts/progression_prompt.py`, `backend/services/progression_service.py`
**Task:** Build prompt template and service with 1-hour cache.
**Test:** Call `get_progression(provider, "SomeAgent", 30)` with mock data, verify valid JSON response with all 9 sections.
**Depends on:** Unit C (needs DataProvider interface). Can develop against mock data.

### Unit E: Dashboard API + frontend

**Scope:** `backend/routes/dashboard.py`, `backend/main.py` (modify), `frontend/dashboard.html`
**Task:** Create routes, wire into FastAPI, build Chart.js dashboard.
**Test:** Start server, open `/dashboard`, select agent, verify charts + assessment render.
**Depends on:** Units C + D (needs working providers and progression service).

### Unit F: Backfill script

**Scope:** `scripts/backfill_reasoning.py`
**Task:** One-time migration of existing data.
**Test:** Run with `--dry-run`, verify matched row count. Run for real, verify Analyst_History extended columns populated.
**Depends on:** Units A + B (both sheets need extended columns).

### Dependency graph

```text
Unit A (Form_Responses_AI extension)
  |
  v
Unit B (Apps Script extension)     Unit D (Gemini service) -- can mock data
  |                                  |
  v                                  |
Unit C (Data models + provider)      |
  |                                  |
  v                                  v
Unit E (API routes + frontend) <--- needs C + D
  |
  v
Unit F (Backfill) <--- needs A + B
```

---

## Verification Checklist

1. **Form Responses AI extended:** After scoring a call, cols Q-AI have reasoning values
2. **Analyst_History enriched:** After pressing UI button, cols P-AK populated from Form_Responses_AI lookup
3. **Manual QA unaffected:** Manual entries have empty P+ columns, email sends normally
4. **Agents list:** `GET /api/agents` returns names from Analyst_History
5. **History:** `GET /api/agents/{name}/history?days=30` returns records with reasoning
6. **Progression:** `GET /api/agents/{name}/progression?days=30` returns Gemini JSON
7. **Dashboard:** Open `/dashboard`, select agent, verify charts render
8. **Fallback:** If DB is down, dashboard works via Sheets only
9. **Apps Script email:** `rebuildHistory()` works, email pipeline unbroken

---

## Open Questions

- **Analyst_History column order verification** — the plan maps from `Config.js HISTORY_COL` but should be verified against the actual sheet headers
- **Form_Responses_AI extended columns** — need to pick a column start that doesn't collide with any existing formulas or manual data after col P
- **Cell notes retention** — keep writing cell notes alongside column values for backward compat, or phase them out?
- **`rebuildHistory()` behavior** — should it also run `_enrichFromFormResponsesAI()` for each rebuilt row, or only for new appends?
