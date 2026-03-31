# Multi-Team QA Scoring Platform — Product Requirements Document

## Executive Summary (for CEO)

**What it is:** Landing's AI-powered QA scoring platform, currently live for Member Support (21 agents), expanded to serve Sales and future teams — each with their own rubric, data, and managers.

**What it costs:** ~$50-100/month in Gemini API usage for two teams (~500 calls/month). No additional infrastructure cost (Railway free tier). One developer maintains it.

**Timeline:** Sales scoring live within 6-8 weeks. Four phases, each independently shippable.

**Key guarantees:**
- Member Support continues working uninterrupted throughout migration
- Sales managers see only Sales data; Member Support managers see only theirs
- Every Gemini API call is logged with team, caller, and estimated cost
- Adding a third team (e.g., Renewals) requires 1-2 days of configuration, no code changes
- Per-team budget caps prevent runaway API costs

**Value:**
- Eliminates 4-6 hours/week of manual QA scoring per team
- Consistent, auditable scoring across teams — no more "my manager scores differently"
- Statistical dashboards (outlier detection, EWMA trends, SPC charts) surface coaching opportunities that manual review misses
- Platform scales linearly: same codebase, same infra, more teams

---

## 1. Current State

### What works today
- FastAPI backend scoring calls via Gemini 2.5 Flash
- Results written to Google Sheets with per-section AI reasoning
- Apps Script sends formatted QA emails to agents on manager approval
- Agent progression dashboard (per-agent trends, Gemini coaching assessments)
- Team analytics dashboard (EWMA, SPC, outlier detection, section weakness mapping, supervisor views)

### What's hardcoded to Member Support
- **QA rubric** (10 sections: Greeting, Identity Validation, etc.) embedded in 8+ files
- **Google Sheet ID** — single sheet in .env
- **Column layout** (A-AK) — indices hardcoded in Python and Apps Script
- **Gemini prompts** — rubric text and scoring instructions are inline strings
- **No authentication** — anyone on the network can access all endpoints
- **No audit logging** — no tracking of who triggered what or at what cost

---

## 2. Architecture

### Multi-tenancy model
Shared FastAPI server with URL-based team routing. One codebase, one deployment, team determined by URL path.

```
/score/{team_id}                → Scoring UI for that team
/dashboard/{team_id}            → Team analytics dashboard
/dashboard/{team_id}/agent/{name} → Per-agent drill-down
/api/{team_id}/score            → Scoring endpoints
/api/{team_id}/agents           → Agent endpoints
/api/{team_id}/stats            → Team stats endpoints
/api/admin/costs                → Cross-team admin (exec only)
```

### Rubric abstraction
Each team's rubric is a JSON configuration file at `/backend/config/teams/{team_id}.json`. Contains:
- Team display name and Google Sheet ID
- Tab names (Form Responses AI, Analyst_History, Mails)
- Section definitions: id, name, score_type (numeric/yn), range, rubric text, score descriptions, confidence rules, column mappings
- Gemini prompt customization (temperature, model, special instructions)
- Monthly budget cap for API calls

The scoring prompt, column layouts, dashboard labels, and statistical computations all derive from this single config file. No rubric information is hardcoded in application code.

### Data isolation
```
API Request → Auth Middleware (validates API key → team_id)
           → Team Config (loads {team_id}.json)
           → SheetsProvider(team_config) (connects to that team's Google Sheet)
           → Response (scoped to that team only)
```

A Sales API key cannot access Member Support data. The middleware enforces this before any business logic runs.

### Apps Script strategy
Each team's Google Sheet gets its own copy of the Apps Script project. Only `Config.js` differs (section definitions, column indices, thresholds). All other files (QAEntry, AnalystHistory, EmailSender, etc.) are identical copies. Changes to shared logic are deployed via `clasp push` to each sheet.

---

## 3. Security

### Authentication
API key per team, sent via `Authorization: Bearer {key}` header. Keys stored as Railway environment variables in production, .env locally.

- Each key maps to exactly one team_id
- Requests without a key: 401 Unauthorized
- Requests with a key for a different team: 403 Forbidden
- Admin key for cross-team executive endpoints (separate)

### CORS
Restrict `allow_origins` to known URLs: Railway deployment domain + localhost for dev. No more `["*"]`.

### Audit trail
Every API request logged to a JSONL file (Postgres table later):
```json
{
  "timestamp": "2026-04-01T14:23:00Z",
  "team_id": "member_support",
  "endpoint": "/api/member_support/score",
  "manager_email": "max@hellolanding.com",
  "action": "score_call",
  "model": "gemini-2.5-flash",
  "estimated_cost_usd": 0.12,
  "call_id": "5126733512974336",
  "agent_name": "Israel Valencia",
  "duration_ms": 8200,
  "status": "complete"
}
```

### Rate limiting
10 concurrent scoring jobs per team. Prevents accidental batch floods from consuming the Gemini budget.

### Cost controls
- Per-team monthly budget cap in team config JSON
- When budget is reached: scoring returns clear error, dashboards continue working (read-only)
- Admin endpoint: `GET /api/admin/costs?period=monthly` shows spend per team
- Every Gemini call logs estimated cost based on token count

---

## 4. Deployment Strategy

### Phase 1: Demo on localhost (CEO meeting)
- Current setup. Run locally, show Member Support dashboards + scoring pipeline.
- Show the team config JSON to demonstrate how Sales would be configured.

### Phase 2: Railway deployment with auth
- Deploy to Railway (free tier, HTTPS built-in)
- Auth middleware enforced
- CORS restricted to Railway URL
- Managers access via `https://{app}.railway.app/score/member-support`
- Environment variables for API keys and sheet IDs

### Scaling triggers
- 3+ teams or 2000+ calls/month: Railway Hobby plan ($5/month)
- 5+ teams or individual user auth needed: Add Google OAuth (Phase 3)
- 10+ teams: Consider Postgres as primary data store instead of Sheets

---

## 5. Implementation Phases

### Phase A: Rubric Abstraction (Week 1-2)
Extract all hardcoded rubric references into JSON config. Zero user-visible changes.

**Create:**
- `backend/config/team_config.py` — TeamConfig dataclass, loader, JSON schema validation
- `backend/config/teams/member_support.json` — current rubric extracted to JSON

**Modify (in dependency order):**
1. `backend/prompts/qa_scoring_prompt.py` — dynamic prompt from config sections
2. `backend/prompts/progression_prompt.py` — section list from config
3. `backend/services/sheets_service.py` — column mapping from TeamConfig
4. `backend/services/history_service.py` — SheetsProvider accepts TeamConfig
5. `backend/services/team_stats.py` — NUMERIC_SECTIONS, SECTION_LABELS from config
6. `backend/services/progression_service.py` — section mapping from config
7. `backend/services/scoring_service.py` — passes TeamConfig through pipeline
8. `backend/services/data_provider.py` — `get_provider(team_id)` with per-team cache

**Verification:** Score 5 real Member Support calls, compare output to pre-migration. Identical results = success.

### Phase B: Team Routing + Auth (Week 3-4)
API routes accept team_id, requests authenticated, data isolated.

**Create:**
- `backend/middleware/auth.py` — API key validation, team_id extraction
- `backend/middleware/audit.py` — request logging with cost attribution

**Modify:**
- `backend/main.py` — auth middleware, CORS restrictions, team-prefixed routes
- `backend/routes/scoring.py` — team_id from path, scoped job store
- `backend/routes/dashboard.py` — team_id scoping
- `backend/routes/team.py` — team_id scoping
- All frontend HTML — send auth header, team-aware API calls

**Backward compat:** Old routes (`/api/agents`) still work during 30-day transition, default to `team_id=member_support`.

**Verification:** Member Support API key works. No-key requests get 401. Wrong-team requests get 403.

### Phase C: Sales Onboarding (Week 5-6)
Sales team goes live.

**Steps:**
1. Define Sales rubric with Sales management (sections, scoring criteria)
2. Create `backend/config/teams/sales.json`
3. Set up Sales Google Sheet (they already have one — configure tabs)
4. Share sheet with service account
5. Deploy Apps Script to Sales sheet (copy, modify Config.js)
6. Generate Sales API key
7. End-to-end test: upload Sales call → Gemini scores with Sales rubric → results in Sales sheet

### Phase D: Cost Tracking + Admin Dashboard (Week 7-8)
Executive visibility.

**Create:**
- `backend/services/audit_service.py` — audit log writes and queries
- `backend/routes/admin.py` — `GET /api/admin/costs`, usage stats
- `frontend/admin_dashboard.html` — cross-team cost/usage visualization

---

## 6. Sales-Specific Considerations

Sales already has a Google Sheet. Key differences from Member Support:
- **Different rubric sections** — likely includes: Needs Discovery, Product Knowledge, Objection Handling, Close Technique, Follow-up Commitment (TBD with Sales management)
- **Different column layout** — fewer or more sections means different A-Z mapping
- **Different Gemini prompt** — scoring criteria describe sales behaviors, not support behaviors
- **Potentially different Dialpad filtering** — Sales calls may have different metadata
- **Same infrastructure** — same Gemini API key, same service account, same FastAPI server

The JSON config approach handles all of these differences without code changes.

---

## 7. Feature: DataPoints — Evaluation Drill-Down

### Purpose
Every data point in every chart must be traceable to a specific call evaluation with full context. This provides end-to-end transparency and accountability when justifying dashboard data to supervisors, agents, and executives.

### Eval ID Strategy
Use the numeric `call_id` extracted from the Dialpad link URL (`https://dialpad.com/callhistory/callreview/{call_id}`) as the primary evaluation identifier. This is globally unique in Dialpad and already present in Analyst_History col P.

**Duplicate evaluation handling:** If the same call_id appears more than once in Analyst_History (e.g., appeal, re-score by different supervisor), the system detects this at query time and surfaces a warning: "This call has multiple evaluations." For v1, show all versions. Future: prompt the user to choose which score persists.

### Data Source
The DataPoint detail page reads from Analyst_History only (already cached via SheetsProvider). No Dialpad API call at view time.

**However:** The scoring pipeline must be modified to persist caller metadata (name, phone) at scoring time:
- `dialpad_client.py` — add `get_call_details(call_id)` to fetch caller name and phone
- `scoring_service.py` — call `get_call_details()` during scoring, pass to sheets writer
- `sheets_service.py` — write caller name and phone to new columns in Form Responses AI
- `AnalystHistory.js` — `_enrichFromFormResponsesAI()` copies caller metadata to Analyst_History extended columns

This means future evaluations will have caller context; historical ones won't (acceptable).

### Route Structure
```
GET /datapoint/{call_id}                → Evaluation detail page (HTML)
GET /api/datapoints/{call_id}           → Evaluation detail JSON (for API consumers)
GET /api/datapoints?bin=91-100&days=90  → List of eval summaries in a score range
GET /api/datapoints?agent={name}&days=90 → List of eval summaries for an agent
```

### Clickable Chart Interactions (all four for v1)

1. **Overall Score Trend** (agent view at `/dashboard/agent/{name}`)
   - Each dot on the line chart is clickable
   - Chart.js `onClick` handler extracts the data index, looks up the eval's call_id from the history array
   - Navigates to `/datapoint/{call_id}`

2. **Score Distribution** (team view at `/dashboard`)
   - Each histogram bar is clickable
   - On click: fetch `/api/datapoints?bin={range}&days={days}` to get the list of evals in that bin
   - Display as an expandable table below the chart: Agent, Date, Score, Call ID (each row links to `/datapoint/{call_id}`)

3. **Outlier Table** (team view, Outliers tab)
   - Score column is a clickable link: `<a href="/datapoint/{call_id}">{score}</a>`

4. **Agent Roster eval count** (team view, Agent Roster tab)
   - Eval count column is a clickable link: navigates to `/dashboard/agent/{name}` (already exists)
   - The agent view then shows clickable dots per point 1 above

### Detail Page Layout (Hybrid style)

Single-page HTML at `/datapoint/{call_id}`. Combines the scoring UI panel style (web-native CSS) with the email's FeedbackCard and ProgressionCard patterns.

```
┌─────────────────────────────────────────────────────┐
│ [Team View]  [Agent View]            DataPoint       │  ← Navy header with nav
│ Agent: {name}  |  Call ID: {call_id}  |  {date}     │
│ Supervisor: {supervisor}  |  Scored by: {manager}    │
│ Caller: {caller_name} {caller_phone} (if available)  │
├─────────────────────────────────────────────────────┤
│ ┌─ Score Breakdown ──────────────────────────────┐  │
│ │ Greeting              ████████░░  4/5   HIGH   │  │  ← Scoring UI style rows
│ │ Purpose of Call       ██████░░░░  3/5   MED    │  │    with reasoning text
│ │ ...                                            │  │
│ │ Documentation         ░░░░░░░░░░  —    MANUAL  │  │
│ │ Identity Validation   ✓ Yes        HIGH        │  │
│ │ Customer Resolution   ✗ No         HIGH        │  │
│ │                                                │  │
│ │ Overall: 85.0                                  │  │
│ └────────────────────────────────────────────────┘  │
│                                                      │
│ ┌─ Key Strengths ────────────────────────────────┐  │  ← FeedbackCard style
│ │ (blue left border, light blue background)      │  │    from the email
│ │ "Agent demonstrated excellent rapport..."      │  │
│ └────────────────────────────────────────────────┘  │
│ ┌─ Opportunities for Improvement ────────────────┐  │
│ │ (navy left border, white background)           │  │
│ │ "Consider pausing before transferring..."      │  │
│ └────────────────────────────────────────────────┘  │
│                                                      │
│ [▶ Review Call Recording]                            │  ← Dialpad link button
│                                                      │
│ ┌─ Transcript (expandable) ──────────────────────┐  │
│ └────────────────────────────────────────────────┘  │
│ ┌─ Signal Moments ───────────────────────────────┐  │
│ └────────────────────────────────────────────────┘  │
│                                                      │
│ ┌─ QA Progression (mini-history table) ──────────┐  │  ← ProgressionCard style
│ │ Date       Score  Trend  Progress               │  │    from the email
│ │ Mar 23     85.0   ▲      ████████░░ [85]       │  │
│ │ Mar 15     72.0   ▼      ██████░░░░ [85]       │  │
│ │ ...                                            │  │
│ └────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

### New Columns in Form Responses AI and Analyst_History

Two new columns appended after the existing extended columns:

**Form Responses AI (after col AI):**
| Col | Field |
|---|---|
| AJ | Caller Name |
| AK | Caller Phone |

**Analyst_History (after col AK):**
| Col | Index | Field |
|---|---|---|
| AL | 37 | Caller Name (from Form AI lookup) |
| AM | 38 | Caller Phone (from Form AI lookup) |

### Implementation Phase
This feature can be implemented as **Phase 0** (before rubric abstraction) since it works within the current single-team architecture. It adds value immediately and doesn't depend on multi-team routing.

**Phase 0 sub-steps:**
1. Add `get_call_details(call_id)` to `dialpad_client.py`
2. Modify scoring pipeline to persist caller name/phone
3. Add `eval_id` field to `EvaluationRecord` model (extracted from Dialpad link)
4. Create `/api/datapoints/{call_id}` endpoint
5. Create `/api/datapoints?bin=X&days=Y` list endpoint
6. Create `frontend/datapoint.html` detail page
7. Add Chart.js click handlers to agent dashboard (Overall Score Trend)
8. Add Chart.js click handlers to team dashboard (Score Distribution)
9. Make Outlier scores clickable links
10. Update `_enrichFromFormResponsesAI()` and Config.js for new columns
11. Update `history_service.py` column mappings for new columns

### Files to Create
| File | Purpose |
|---|---|
| `backend/routes/datapoints.py` | DataPoint API endpoints |
| `frontend/datapoint.html` | Evaluation detail page |

### Files to Modify
| File | What changes |
|---|---|
| `backend/services/dialpad_client.py` | Add `get_call_details(call_id)` |
| `backend/services/scoring_service.py` | Fetch and pass caller metadata |
| `backend/services/sheets_service.py` | Write caller name/phone to new columns |
| `backend/services/history_service.py` | Add cols 37-38 mapping, add `eval_id` to parse |
| `backend/models/dashboard.py` | Add `eval_id` field to `EvaluationRecord` |
| `backend/main.py` | Include datapoints router, add `/datapoint/{call_id}` page route |
| `frontend/dashboard.html` | Chart.js onClick for trend dots |
| `frontend/team_dashboard.html` | Chart.js onClick for distribution bars, outlier links |
| `qa-automation/src/Config.js` | Add HISTORY_COL entries for cols AL-AM, FORM_AI_COL entries for AJ-AK |
| `qa-automation/src/AnalystHistory.js` | Extend `_enrichFromFormResponsesAI()` + headers for new columns |

---

## 8. Risk Matrix

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Rubric abstraction introduces scoring regression | Medium | High | Compare 10 pre/post scores for identical calls |
| Sales rubric takes longer to define | High | Low | Start with simplified rubric, iterate |
| API key leaked or shared | Medium | Medium | Keys are rotatable; audit log detects anomalies |
| Gemini cost spike | Low | Medium | Per-team budget caps; cost alerts |
| Solo developer unavailable | Medium | High | JSON config is readable; docs explain team onboarding |
| Google Sheets rate limits with 2 teams | Low | Medium | 5-min cache already in place |
| Member Support disrupted during migration | Low | High | Each phase is a separate branch with rollback |

---

## 8. Files Inventory

### New files to create
| File | Phase | Purpose |
|---|---|---|
| `backend/config/__init__.py` | A | Package init |
| `backend/config/team_config.py` | A | TeamConfig loader + validator |
| `backend/config/teams/member_support.json` | A | Member Support rubric |
| `backend/config/teams/sales.json` | C | Sales rubric |
| `backend/middleware/__init__.py` | B | Package init |
| `backend/middleware/auth.py` | B | API key auth |
| `backend/middleware/audit.py` | B | Request logging |
| `backend/services/audit_service.py` | D | Cost tracking |
| `backend/routes/admin.py` | D | Admin endpoints |
| `frontend/admin_dashboard.html` | D | Cross-team admin view |

### Files to modify
| File | Phase | What changes |
|---|---|---|
| `backend/prompts/qa_scoring_prompt.py` | A | Dynamic prompt from config |
| `backend/prompts/progression_prompt.py` | A | Section list from config |
| `backend/services/sheets_service.py` | A | Column mapping from TeamConfig |
| `backend/services/history_service.py` | A | SheetsProvider accepts TeamConfig |
| `backend/services/team_stats.py` | A | Sections from config |
| `backend/services/progression_service.py` | A | Section mapping from config |
| `backend/services/scoring_service.py` | A | Passes TeamConfig through pipeline |
| `backend/services/data_provider.py` | A+B | Team-aware provider factory |
| `backend/main.py` | B | Auth middleware, team routing |
| `backend/routes/scoring.py` | B | Team-scoped endpoints |
| `backend/routes/dashboard.py` | B | Team-scoped endpoints |
| `backend/routes/team.py` | B | Team-scoped endpoints |
| `frontend/index.html` | B | Auth header, team-aware |
| `frontend/dashboard.html` | B | Auth header, team-aware |
| `frontend/team_dashboard.html` | B | Auth header, team-aware |

### DataPoint feature files
| File | Phase | Purpose |
|---|---|---|
| `backend/routes/datapoints.py` | 0 | DataPoint API endpoints |
| `frontend/datapoint.html` | 0 | Evaluation detail page (hybrid layout) |

### DataPoint modifications
| File | Phase | What changes |
|---|---|---|
| `backend/services/dialpad_client.py` | 0 | Add `get_call_details(call_id)` for caller metadata |
| `backend/services/scoring_service.py` | 0 | Fetch caller name/phone during scoring |
| `backend/services/sheets_service.py` | 0 | Write caller name/phone to Form Responses AI cols AJ-AK |
| `backend/services/history_service.py` | 0 | Add cols 37-38, add eval_id to EvaluationRecord parsing |
| `backend/models/dashboard.py` | 0 | Add eval_id, caller_name, caller_phone fields |
| `backend/main.py` | 0 | Include datapoints router, add page route |
| `frontend/dashboard.html` | 0 | Chart.js onClick for trend dots |
| `frontend/team_dashboard.html` | 0 | Chart.js onClick for distribution bars, outlier links |
| `qa-automation/src/Config.js` | 0 | HISTORY_COL + FORM_AI_COL entries for new columns |
| `qa-automation/src/AnalystHistory.js` | 0 | Extend enrichment + headers for caller metadata |

### Files that stay unchanged
- `backend/models/scorecard.py` — already abstract
- `backend/models/team_stats.py` — already abstract
- `backend/services/db_provider.py` — already team-agnostic
