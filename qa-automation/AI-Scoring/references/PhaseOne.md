# Landing QA Automation — Phase 1 Handoff

> **Purpose:** Captures the full state of the project at the end of Phase 1 (April 2026).
> Reconciles the original CLAUDE.md roadmap with the multi-team expansion plans in PRD-MultiTeam.md.
> Intended as a handoff document for continued development.

---

## What Phase 1 Set Out to Do (from CLAUDE.md)

The original Phase 1 spec (Weeks 4-7) called for:

1. FastAPI backend with 3 endpoints: upload audio, trigger scoring, return scorecard JSON
2. Pipeline: audio file -> Gemini 2.5 Flash -> structured transcript -> scoring prompt -> JSON
3. Write draft scorecard to Google Sheets via Sheets API
4. Manager reviews draft in Sheets, approves -> triggers existing Apps Script email flow
5. Host on Railway or Render, deploy from GitHub
6. Database schema with full call metadata from day one

---

## What Was Actually Built

Phase 1 delivered everything in the original spec **except deployment and database**, and
pulled forward significant work from Phase 3 (dashboards, agent tracking, team analytics).

### Core Scoring Pipeline (original Phase 1 scope)

| Component | File | Status |
|---|---|---|
| FastAPI app | `backend/main.py` | Complete |
| Scoring endpoints | `backend/routes/scoring.py` | Complete — `POST /api/score`, `GET /api/score/{job_id}`, `POST /api/score/batch`, `GET /api/calls` |
| Gemini integration | `backend/services/audio_service.py` | Complete — Gemini 2.5 Flash, native audio upload, temp=0.2, 8192 max tokens |
| Scoring prompts | `backend/prompts/qa_scoring_prompt.py` | Complete — full rubric, SOP injection, transcript/moments context, structured JSON output |
| Sheets writer | `backend/services/sheets_service.py` | Complete — writes to `Form Responses AI` tab, cell notes with per-section reasoning |
| Dialpad client | `backend/services/dialpad_client.py` | Complete — agent lookup, call listing, transcript + moments extraction |
| Scorecard models | `backend/models/scorecard.py` | Complete — `ScorecardSection`, `Scorecard`, `ScorecardWithMeta` |
| Upload UI | `frontend/index.html` | Complete — drag-and-drop audio, agent/manager fields, real-time polling, scorecard display |

### Apps Script Email System (original Phase 1 scope)

| Component | File | Status |
|---|---|---|
| Config | `qa-automation/src/Config.js` | Complete — column mappings, thresholds, brand colors |
| QA entry parser | `qa-automation/src/QAEntry.js` | Complete — parses Form Responses row, `toHistoryRow()` outputs 15 values |
| Analyst history | `qa-automation/src/AnalystHistory.js` | Complete — writes/reads cols A-O in Analyst_History tab |
| Email pipeline | `qa-automation/src/Main.js` | Complete — `_processRow()`: parse -> get history -> append -> build email -> send |
| Score card HTML | `qa-automation/src/ScoreCard.js` | Complete — per-section score bars |
| Progression card | `qa-automation/src/ProgressionCard.js` | Complete — historical scores, delta arrows, mini bars |
| Email sender | `qa-automation/src/EmailSender.js` | Complete — sends formatted HTML email to agent |

### Pulled Forward from Phase 3: Dashboards and Analytics

These were originally scoped for Weeks 13-18 but were built during Phase 1:

| Component | File | Status |
|---|---|---|
| Agent dashboard | `frontend/dashboard.html` | Complete — per-agent trends, EWMA trendline, per-section bars, Gemini coaching (opt-in) |
| Team dashboard | `frontend/team_dashboard.html` | Complete — score distribution, outlier detection, EWMA, SPC charts, supervisor views, section weakness mapping |
| Dashboard routes | `backend/routes/dashboard.py` | Complete — agent and team-level analytics endpoints |
| Team routes | `backend/routes/team.py` | Complete — team-level agent/stats endpoints |
| History service | `backend/services/history_service.py` | Complete — reads Analyst_History, normalizes duplicate names, filters test rows |
| Progression service | `backend/services/progression_service.py` | Complete — per-agent trend calculations |
| Team stats | `backend/services/team_stats.py` | Complete — modified z-score outliers, EWMA, SPC, distribution histograms |
| Data provider | `backend/services/data_provider.py` | Complete — provider factory with 5-min cache |
| Progression prompts | `backend/prompts/progression_prompt.py` | Complete — Gemini coaching assessments (opt-in, 1-hour cache) |

### Stubs for Future Work

| Component | File | Status |
|---|---|---|
| Notion sync | `rag/notion_sync/sync.py` | Stub — placeholder for Phase 2 SOP/RAG pipeline |
| Embeddings | `rag/embeddings/embed.py` | Stub — placeholder for Phase 2 ChromaDB integration |
| DB provider | `backend/services/db_provider.py` | Stub — placeholder for Postgres migration |
| Notion service | `backend/services/notion_service.py` | Stub — placeholder for server-side Notion MCP integration |

---

## What Was NOT Built (Deferred)

### From Original Phase 1 Spec

| Item | Why Deferred | Impact |
|---|---|---|
| **PostgreSQL database** | Sheets working as primary store; database adds complexity before multi-team need is real | Call metadata not stored before scoring; sampler can't run without it |
| **Railway/Render deployment** | Running locally is fine for single-team validation; deployment blocked on security (no auth, CORS wide open) | Managers can't access remotely; still requires dev to run locally |
| **Full call metadata schema** | No database to store it in | Stratified sampling strategy from CLAUDE.md not yet actionable |

### Known Technical Debt

| Issue | Severity | Where | PRD-MultiTeam Phase |
|---|---|---|---|
| CORS allows all origins `["*"]` | High | `backend/main.py:30` | Phase B |
| No authentication | High | All endpoints | Phase B |
| No audit logging | Medium | All endpoints | Phase D |
| Rubric hardcoded in 8+ files | High | prompts, services, frontend | Phase A |
| In-memory job store | Medium | `backend/routes/scoring.py` | — |
| Diagnostic print in main.py | Low | `backend/main.py:17` | Cleanup |

---

## The Data Flow Today

```
[Manager selects agent + call in Upload UI]
        |
        v
[POST /api/score]  (audio file + agent name + manager email + call ID)
        |
        v
[scoring_service.py]
  1. Fetch transcript + moments from Dialpad API
  2. Fetch SOP from Notion (if available)
  3. Upload audio to Gemini 2.5 Flash
  4. Send structured scoring prompt (rubric + transcript + SOP + audio)
  5. Parse JSON scorecard response
  6. Return ScorecardWithMeta
        |
        v
[sheets_service.py]
  Append row to "Form Responses AI" tab (cols A-P)
  Insert cell notes with per-section reasoning
        |
        v
[Manager reviews in Google Sheets]
  Copy-pastes approved scores to Form Responses 1
        |
        v
[Apps Script onFormSubmit / UI button]
  _processRow() -> QAEntry -> AnalystHistory -> Email
        |
        v
[Agent receives formatted QA email]
  ScoreCard + FeedbackCard + ProgressionCard
```

### Dashboard Data Flow (read-only)

```
[/dashboard]        -> team_stats.py reads Analyst_History -> team analytics
[/dashboard/agent/] -> history_service.py reads Analyst_History -> per-agent trends
                    -> progression_service.py (opt-in) -> Gemini coaching assessment
```

---

## Current Route Map

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Upload UI (`frontend/index.html`) |
| GET | `/dashboard` | Team analytics dashboard |
| GET | `/dashboard/agent/{name}` | Per-agent drill-down |
| POST | `/api/score` | Upload audio, start scoring job |
| GET | `/api/score/{job_id}` | Poll scoring result |
| POST | `/api/score/batch` | Batch scoring |
| GET | `/api/calls` | List calls for agent (Dialpad) |
| GET | `/api/agents` | List active agents |
| GET | `/api/agents/{name}/stats` | Per-agent statistics |
| GET | `/api/team/stats` | Team-wide statistics |
| GET | `/health` | Health check |

---

## File Inventory

### Backend

```
backend/
  main.py                          FastAPI entry point, CORS, route mounting
  models/
    scorecard.py                   ScorecardSection, Scorecard, ScorecardWithMeta
    dashboard.py                   Dashboard data models (EvaluationRecord, etc.)
    team_stats.py                  Team statistics models
  routes/
    scoring.py                     Scoring API endpoints
    dashboard.py                   Dashboard/analytics endpoints
    team.py                        Team-level endpoints
  services/
    audio_service.py               Gemini 2.5 Flash integration
    scoring_service.py             Pipeline orchestrator
    sheets_service.py              Google Sheets writer
    dialpad_client.py              Dialpad API client
    history_service.py             Analyst_History reader
    progression_service.py         Per-agent trends + Gemini coaching
    team_stats.py                  Statistical computations
    data_provider.py               Provider factory with cache
    db_provider.py                 Postgres stub
    notion_service.py              Notion MCP stub
  prompts/
    qa_scoring_prompt.py           Scoring rubric + prompt builder
    progression_prompt.py          Coaching assessment prompt
```

### Frontend

```
frontend/
  index.html                       Upload + scoring UI
  dashboard.html                   Per-agent dashboard
  team_dashboard.html              Team analytics dashboard
```

### Apps Script

```
qa-automation/src/
  Config.js                        Column mappings, thresholds, brand colors
  QAEntry.js                       Form response parser
  AnalystHistory.js                History tab read/write
  Main.js                          _processRow() pipeline
  ScoreCard.js                     Score card HTML builder
  ProgressionCard.js               Progression card HTML builder
  FeedbackCard.js                  Feedback card HTML builder
  EmailSender.js                   Email dispatcher
```

### Other

```
scripts/score_call.py              Phase 0 standalone CLI scoring script
rag/notion_sync/sync.py            Stub
rag/embeddings/embed.py            Stub
tests/test_scoring.py              Scoring tests
requirements.txt                   Python dependencies
.env                               API keys (gitignored)
references/
  CLAUDE.md                        Canonical project briefing
  PhaseZero.md                     Phase 0 handoff
  PhaseOne.md                      This file
  PRD-MultiTeam.md                 Multi-team expansion spec
  AgentProgressionDashboard.md     Dashboard design doc
  TeamStatsBoard.md                Team stats design doc
```

---

## Tech Stack (Phase 1)

| Layer | Tool | Notes |
|---|---|---|
| AI scoring | Gemini 2.5 Flash | Native audio upload, temp=0.2, 8192 max tokens |
| Backend | FastAPI (Python) | Async, 4 route modules |
| Transcript source | Dialpad API v2 | Agent lookup, call listing, transcript + moments |
| Data store | Google Sheets (gspread) | Form Responses AI + Analyst_History tabs |
| Email pipeline | Google Apps Script | ScoreCard + FeedbackCard + ProgressionCard |
| Frontend | Vanilla HTML/CSS/JS | Chart.js for visualizations, no framework |
| Statistics | pandas + numpy | EWMA, SPC, z-score outliers |
| Fonts | Fraunces + DM Mono | Consistent brand across all pages |

---

## Revised Roadmap: Reconciling CLAUDE.md with PRD-MultiTeam.md

The original CLAUDE.md roadmap assumed a linear progression:

```
Phase 0 (validation) -> Phase 1 (backend) -> Phase 2 (RAG) -> Phase 3 (dashboard) -> Phase 4 (automation)
```

Reality diverged: Phase 1 absorbed most of Phase 3 (dashboards) but skipped deployment and database.
Meanwhile, the multi-team expansion (PRD-MultiTeam.md) introduced its own phases (0/A/B/C/D) that
partially overlap with the original Phases 2-4.

### What's next: unified roadmap

The following merges both roadmaps into a single sequence. Each step is independently shippable.

#### Step 1 — Security Hardening + Deployment (PRD Phase B, partially) -- COMPLETE

**Goal:** Get the current single-team system deployed and accessible to managers remotely.

**Completed 2026-04-06:**
- API key authentication middleware (`backend/middleware/auth.py`) with `secrets.compare_digest()`
- Audit logging middleware (`backend/middleware/audit.py`) — JSONL file
- CORS restricted to `ALLOWED_ORIGINS` env var (Railway domain + localhost)
- Dual credential loading (file path for local dev, inline JSON for Railway)
- Deployed to Railway Hobby tier at `landing-scripts-production.up.railway.app` (port 8080)
- Frontend auth via `sessionStorage` prompt on first API call
- Health endpoint (`/api/health`) remains unauthenticated
- Procfile, .env.example, diagnostic print removed

#### Step 1.5 — Manager Approval Workflow

**Goal:** Managers review, edit, and approve AI-proposed scores entirely in the frontend.
No manual Sheets work required. Approval triggers the existing Apps Script email pipeline.

This is the missing link between AI scoring and manager delivery. Without it, managers must
manually copy scores between Sheets tabs — friction that blocks adoption. CLAUDE.md principle:
"AI proposes; managers approve." This feature IS the approval mechanism.

**Flow:**
1. AI scores call -> writes draft to Form Responses AI (existing)
2. Manager reviews scorecard in frontend, edits scores/reasoning/feedback as needed
3. Clicks "Approve & Send"
4. Backend updates Form Responses AI with manager edits:
   - Score columns D-M (if changed)
   - Feedback columns N-O (if changed)
   - AI reasoning columns S, U, W, Y, AA, AC, AE, AF, AI (with edited reasoning)
5. Backend copies cols A-P to Form Responses 1 (appends new row)
6. Backend waits 3-4 seconds for ARRAYFORMULA to calculate Overall Score (col Q) and agent email (col V)
7. Backend calls Apps Script `doPost()` web app endpoint
8. Apps Script `_processRow()` handles: Analyst_History append + enrichment + email send

**Backend changes:**
- New endpoint: `POST /api/score/{job_id}/approve` — receives edited scorecard
- `sheets_service.py` — add methods to update reasoning columns + copy row to Form Responses 1
- New: call Apps Script web app `doPost()` after buffer

**Apps Script changes:**
- Add `doPost(e)` function to `Main.js` — calls `_processRow()` on latest row
- Deploy as Web App (execute as: me, access: anyone with link)
- No other Apps Script changes required

**Frontend changes:**
- Make scorecard panel editable: score dropdowns, reasoning textareas, feedback fields
- Add "Approve & Send" button
- Show confirmation on successful approval + email send

**Deliverables:** Manager clicks "Approve & Send" in the frontend -> scores land in both sheets,
email sent to agent. Zero Sheets interaction required.

#### Step 2 — DataPoints: Evaluation Drill-Down (PRD Phase 0)

**Goal:** Every data point in every chart is traceable to a specific call evaluation.

This can be built on the current single-team architecture. It adds immediate value to
the existing dashboards and doesn't depend on multi-team routing.

- Add `get_call_details(call_id)` to `dialpad_client.py` for caller metadata
- Persist call_summary, caller name, caller phone as columns in Form Responses AI (cols AJ-AL)
- Create `/api/datapoints/{call_id}` endpoint and `frontend/datapoint.html` detail page
- Add Chart.js click handlers to agent dashboard (trend dots) and team dashboard (distribution bars)
- Make outlier scores clickable links
- Update Apps Script column mappings for new columns

**Deliverables:** Click any data point -> see full evaluation detail with scorecard breakdown,
strengths/improvements, call summary, and mini-history table.

#### Step 3 — Rubric Abstraction (PRD Phase A)

**Goal:** Extract all hardcoded rubric references into a single JSON config file per team.

This is the foundation for multi-team support. Zero user-visible changes — the system behaves
identically, but rubric definitions live in config instead of scattered across 8+ files.

- Create `backend/config/team_config.py` — TeamConfig dataclass, loader, JSON schema validation
- Create `backend/config/teams/member_support.json` — current rubric extracted to JSON
- Modify (in dependency order): `qa_scoring_prompt.py`, `progression_prompt.py`, `sheets_service.py`,
  `history_service.py`, `team_stats.py`, `progression_service.py`, `scoring_service.py`, `data_provider.py`

**Deliverables:** Score 5 real calls, compare output to pre-migration. Identical results = success.

#### Step 4 — Team Routing + Multi-Team Support (PRD Phase B, remainder)

**Goal:** API routes accept team_id, requests authenticated per team, data fully isolated.

- URL-based team routing: `/api/{team_id}/score`, `/dashboard/{team_id}`, etc.
- Each API key maps to exactly one team_id
- Sales API key cannot access Member Support data
- Backward compat: old routes (`/api/agents`) default to `team_id=member_support` for 30-day transition
- All frontend pages become team-aware

**Deliverables:** Member Support continues working. New team endpoints ready.

#### Step 5 — Sales Onboarding (PRD Phase C)

**Goal:** Sales team goes live with their own rubric, sheet, and dashboard.

- Define Sales rubric with Sales management (sections, scoring criteria, column layout)
- Create `backend/config/teams/sales.json`
- Set up Sales Google Sheet, share with service account
- Deploy Apps Script to Sales sheet (copy, modify Config.js)
- Generate Sales API key
- End-to-end test: upload Sales call -> Gemini scores with Sales rubric -> results in Sales sheet

**Deliverables:** Sales managers score calls independently. Sales data isolated from Member Support.

#### Step 6 — SOP/Notion RAG Integration (original Phase 2)

**Goal:** Accurate scoring of Process Adherence and Call Resolution against actual SOPs.

This was the original Phase 2 from CLAUDE.md. It becomes more valuable after multi-team
support since each team can have their own SOP corpus.

- Connect Notion API -> export SOP content nightly
- Chunk and embed SOPs using embedding model -> store in ChromaDB
- At scoring time: identify call type from transcript -> retrieve relevant SOP chunks -> inject into scoring prompt
- Per-team SOP namespaces in ChromaDB

**Deliverables:** Sections 5 and 6 score against actual policy. Dramatically improved accuracy.

#### Step 7 — Cost Tracking + Admin Dashboard (PRD Phase D)

**Goal:** Executive visibility into cross-team usage and costs.

- `backend/services/audit_service.py` — audit log queries and aggregation
- `backend/routes/admin.py` — `GET /api/admin/costs?period=monthly`
- `frontend/admin_dashboard.html` — cross-team cost/usage visualization
- Per-team monthly budget caps with clear error when exceeded

**Deliverables:** CEO-ready dashboard showing cost per team, usage trends, budget status.

#### Step 8 — Dialpad Webhook Automation (original Phase 4)

**Goal:** Zero manual steps. Calls score themselves at end of call.

- Investigate Dialpad webhook for call completion events
- Implement stratified random sampling (30-35 calls/agent/week)
- Auto-trigger scoring pipeline on call end for sampled calls
- Requires database (Postgres) for call metadata storage and sampling state
- Manager opens dashboard next morning to review overnight scores

**Deliverables:** Fully automated end-to-end pipeline. Human role is review-only.

#### Step 9 — PostgreSQL Migration (original Phase 1 deferred item)

**Goal:** Move from Google Sheets to Postgres as primary data store.

This can happen at any point after Step 4 (multi-team routing) but becomes necessary
at Step 8 (automation) when the system needs to store call metadata before scoring
and manage sampling state. The `db_provider.py` stub exists for this.

- Design schema covering call metadata, scorecards, agent history, audit logs, sampling state
- Migrate existing Sheets data
- DataProvider abstraction already exists — swap Sheets for Postgres behind the same interface
- Keep Sheets as a read-only sync target for managers who prefer the spreadsheet view

**Note:** Steps 8 and 9 are tightly coupled. The sampler needs persistent call metadata
that Sheets can't efficiently provide. Plan them together.

---

## Scaling Triggers (from PRD-MultiTeam.md)

These thresholds determine when to upgrade infrastructure:

| Trigger | Action |
|---|---|
| 3+ teams or 2000+ calls/month | Railway Hobby plan ($5/month) |
| 5+ teams or individual user auth needed | Add Google OAuth |
| 10+ teams | Postgres as primary data store (Step 9 becomes mandatory) |

---

## Key Principles (unchanged from CLAUDE.md)

1. **Human review is non-negotiable at launch.** AI proposes scores; managers approve.
2. **Documentation (Section 9) is always manual.** Never build automation for it.
3. **The existing Apps Script email flow is sacred** until fully replaced.
4. **Build for maintainability over cleverness.** This outlasts any one person.
5. **Start with one team's rubric and validate before expanding.**
6. **Audio is the source of truth.** Transcripts miss tone, emotion, and nuance.
7. **Cost matters at scale.** Gemini Flash at $0.40/M tokens.

### Added by PRD-MultiTeam.md

8. **Data isolation is non-negotiable.** A Sales API key must never access Member Support data.
9. **JSON config is the rubric source of truth.** No rubric information hardcoded in application code.
10. **Adding a new team should require configuration, not code changes.**
11. **Per-team budget caps prevent runaway API costs.**

---

## Decision Log

| Decision | Date | Rationale |
|---|---|---|
| Gemini coaching assessments are opt-in, not default | 2026-03-24 | $0.15/call adds up; AI summaries should be deliberate |
| Security hardening before multi-team (Step 1 first) | 2026-04 | Cannot expose unauthed endpoints to additional teams |
| DataPoints before rubric abstraction (Step 2 before 3) | 2026-04 | Adds immediate value on current architecture, doesn't depend on multi-team |
| Postgres deferred to Step 9, coupled with automation | 2026-04 | Sheets working fine for 1-2 teams; database needed when sampling state enters the picture |
| Dashboards pulled into Phase 1 instead of Phase 3 | 2026-03 | Analytics needed immediately for coaching value; waiting until Phase 3 would delay the primary use case |
| Railway deployment blocked on auth | 2026-04 | Cannot deploy with CORS `["*"]` and no authentication |
| Step 1 completed, deployed to Railway | 2026-04-06 | Auth, CORS, audit logging, dual credentials, Hobby tier |
| Approval workflow added as Step 1.5 (before DataPoints) | 2026-04-06 | Without frontend approval, managers still live in Sheets — blocks adoption more than missing drill-down |
| Apps Script doPost() for triggering email pipeline | 2026-04-06 | Keeps Apps Script sacred; backend writes to Sheets + calls web app rather than duplicating email logic |
| 3-4 second buffer before doPost() call | 2026-04-06 | ARRAYFORMULA needs time to calculate Overall Score (col Q) and agent email (col V) after row write |

---

## Repository

- **GitHub:** `https://github.com/mxg108/Landing-scripts`
- **Primary branch:** `main`
- **Working directory:** `qa-automation/AI-Scoring/`
