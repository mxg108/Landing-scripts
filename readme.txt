================================================================================
  LANDING SCRIPTS — INTERNAL AUTOMATION TOOLS
  Business Intelligence & Operations Automation
================================================================================

  Maintained by: [Maximiliano Pérez / Member Support Management]
  Department:    Operations
  Last Updated:  May 2026
  Status:        Active — all systems live and in use

  Component versions (current):
    - Mass Notifications (Apps Script):     v3.3.0
    - QA Automation     (Apps Script):     v2.0.0  (multi-team, post-Phase 2)
    - AI-Scoring        (Python backend):  v2.1    (Railway, multi-team)

--------------------------------------------------------------------------------
  OVERVIEW
--------------------------------------------------------------------------------

This repository contains two internal automation tools built on Google Apps
Script and hosted inside Google Workspace. Both tools are designed to reduce
manual effort for operations and quality assurance workflows at Landing.

  1. Mass Notifications  — Bulk email system for member communications and updates
  once their reservation has started
  2. QA Automation       — Automated QA feedback and scoring emails for agents
  2.1 AI-Scoring          — AI-powered call scoring pipeline + agent progression dashboard

The QA Automation Apps Script and AI-Scoring Python backend work together:
AI-Scoring grades calls via Gemini, writes the draft to Form Responses AI,
and (after manager approval) finalizes the row in Analyst_History. The
Apps Script's sole responsibility is dispatching the QA evaluation email
from the Analyst_History row that the backend just wrote — it no longer
appends, enriches, or reads Form Responses 1.

As of Phase 2 (May 2026), the system supports multiple teams via per-team
JSON config. Member Support and Sales are both live, with distinct
rubrics, section counts (MS = 10, Sales = 19), Apps Script deployments,
and Google Sheets. Adding a new team requires authoring a JSON config
and regenerating its `Config.js` — no code changes.

Dashboard pages show per-agent trends, team analytics, and Gemini coaching.
DataPoint detail pages provide full drill-down into individual evaluations.
All multi-team routes are team-scoped (`/dashboard/{team_id}`,
`/score/{team_id}`, `/datapoint/{team_id}/{call_id}`, etc.).

Mass Notifications and QA Automation run inside Google Sheets as custom menus
or form triggers. AI-Scoring runs as a FastAPI server deployed on Railway
(Hobby tier) with per-team API key authentication and CORS restrictions.

--------------------------------------------------------------------------------
  REPOSITORY STRUCTURE
--------------------------------------------------------------------------------

  landing-scripts/
  ├── database/
  │   └── migrations/           SQL migration files
  │       ├── 001_mass_notifications_schema.sql
  │       ├── 002_add_property_event_columns.sql
  │       └── 003_qa_scoring_schema.sql
  │
  ├── mass-notifications/
  │   └── src/                  Google Apps Script files (.gs)
  │       ├── Config.gs         Runtime configuration management
  │       ├── Database.gs       PostgreSQL JDBC connection, queries, and migration
  │       ├── Mailer.gs         Core email sending logic
  │       ├── Templates.gs      Pre-built notification templates
  │       ├── Tokenizer.gs      Dynamic token substitution in email body
  │       ├── DryRun.gs         Preview and test-send modes
  │       ├── Recipients.gs     Recipient list handling
  │       ├── RunLog.gs         Audit trail logging + config/recipient restore
  │       ├── Attachments.gs    File attachment handling via Drive
  │       ├── Cards.gs          Reusable HTML content blocks
  │       ├── Reset.gs          Undo / archive / reset operations
  │       ├── UI.gs             Custom menus (Mass Notify + Database) and modals
  │       ├── Utils.gs          Shared utility functions + archive sheet management
  │       ├── lookerclient/     Looker API integration (v3.2.0+)
  │       │   ├── LookerAuth.gs    OAuth2 token management + credential setup
  │       │   ├── LookerQuery.gs   API wrappers (inline query for active occupants)
  │       │   └── LookerSync.gs    Orchestrator: fetch → sanitise → populate sheet
  │       └── webapp/           Web App front-end (HtmlService)
  │           ├── Index.html       Shell page; stitches section partials together
  │           ├── WebApp.gs        doGet entry point + server-side API
  │           ├── WebApp_Recipients.html  Recipients grid + Looker fetch panel
  │           ├── WebApp_Config.html      Config fields + rich-text editor
  │           ├── WebApp_Preview.html     Live email preview
  │           └── WebApp_Send.html        Send controls and status
  │
  └── qa-automation/
      ├── src/                  Shared Apps Script logic (multi-team)
      │   ├── Main.js           doPost endpoint — reads Analyst_History
      │   │                     row and dispatches the QA email
      │   ├── QAEntry.js        Data model — built via fromHistoryRow
      │   │                     static factory from the new AH layout
      │   ├── AnalystHistory.js Read-only access to Analyst_History
      │   │                     (getHistory for progression card)
      │   ├── ScoreCard.js      HTML score breakdown visualization
      │   ├── FeedbackCard.js   HTML strengths/improvements visualization
      │   ├── ProgressionCard.js HTML trend and progression visualization
      │   ├── HtmlRenderer.js   Full email template assembly
      │   └── EmailSender.js    Gmail integration
      ├── teams/                Per-team Apps Script overlay
      │   ├── member_support/
      │   │   ├── Branding.js       Hand-edited: colors, email copy, goal
      │   │   ├── Config.js         AUTO-GENERATED from JSON config
      │   │   └── .clasp.json       Member Support Script ID
      │   └── sales/                (same structure as member_support)
      ├── scripts/              Python utilities (run from repo root)
      │   ├── build_config.py            Regenerate teams/{id}/Config.js
      │   ├── migration_utils.py         Shared helpers + rate limiter
      │   ├── migrate_ms_history.py      Phase E — MS AH reorder
      │   ├── import_sales_history.py    Phase E — Sales FR3 → AH import
      │   └── backfill_sales_overall_scores.py   Scores!Y → AH!F
      ├── push.projects         Manifest of deployable Apps Script projects
      │                         consumed by ../push.sh
      └── AI-Scoring/           Python backend (v2.1 — deployed on Railway)
          ├── references/
          │   ├── CLAUDE.md         Full project spec and roadmap
          │   ├── PhaseZero.md      Phase 0 handoff doc
          │   ├── PhaseOne.md       Phase 1 handoff + unified roadmap
          │   ├── PhaseTwo.md       Phase 2 design + completion log
          │   │                     (schema convergence + Sales onboarding)
          │   ├── PRD-MultiTeam.md  Multi-team expansion spec
          │   ├── AgentProgressionDashboard.md  Dashboard design doc
          │   └── TeamStatsBoard.md Team analytics design doc
          ├── backend/
          │   ├── main.py           FastAPI app entry point
          │   ├── config/
          │   │   ├── team_config.py        Pydantic TeamConfig schema
          │   │   ├── history_layout.py     HistoryLayout(N) derived columns
          │   │   ├── env.py                Per-team env var lookup
          │   │   └── teams/                Per-team JSON config
          │   │       ├── member_support.json
          │   │       └── sales.json
          │   ├── middleware/
          │   │   ├── auth.py       Per-team API key authentication
          │   │   └── audit.py      JSONL request audit logging
          │   ├── models/
          │   │   ├── scorecard.py  Scoring + approval models
          │   │   ├── dashboard.py  EvaluationRecord + progression models
          │   │   └── team_stats.py Team analytics models
          │   ├── prompts/
          │   │   ├── qa_scoring_prompt.py   Call scoring prompt (rubric-driven)
          │   │   └── progression_prompt.py  Agent coaching prompt
          │   ├── routes/
          │   │   ├── scoring.py    /api/{team}/score + /score/{id}/approve
          │   │   ├── dashboard.py  Per-agent endpoints
          │   │   ├── team.py       /team/stats + /team/mails + /team/sections
          │   │   ├── datapoints.py /datapoints + /datapoints/{id}
          │   │   └── lookup.py     Cross-team Dialpad lookup
          │   └── services/
          │       ├── scoring_service.py     Gemini call scoring pipeline
          │       ├── sheets_service.py      4-stage write pipeline + email trigger
          │       ├── data_normalization.py  Canonical timestamp + name parsing
          │       ├── dialpad_client.py      Dialpad API client (cached, throttled)
          │       ├── data_provider.py       Abstract data provider
          │       ├── history_service.py     SheetsProvider (Analyst_History)
          │       ├── history_layout.py      Layout derivation utilities
          │       ├── team_stats.py          Statistical computations
          │       ├── progression_service.py Gemini coaching assessments
          │       ├── mails_service.py       Sales/MS Mails sheet readers
          │       └── notion_service.py      Notion SOP integration
          ├── frontend/
          │   ├── index.html        Scoring + editable scorecard (per-team)
          │   ├── dashboard.html    Per-agent progression
          │   ├── team_dashboard.html Team analytics (outliers, SPC, distribution)
          │   ├── datapoint.html    Single-evaluation drill-down
          │   └── lookup.html       Cross-team Dialpad lookup
          ├── Procfile              Railway deployment start command
          └── .env.example          Per-team env var documentation

--------------------------------------------------------------------------------
  1. MASS NOTIFICATIONS
--------------------------------------------------------------------------------

PURPOSE
  Sends bulk, personalized email notifications to residents across one or more
  properties. Designed for property managers who need to communicate scheduled
  events such as fire inspections, maintenance windows, water outages, and
  weather alerts.

WHERE IT LIVES
  Google Sheets — deployed as a bound Google Apps Script project.
  Spreadsheet URL: [REDACTED]
  Apps Script project ID: [REDACTED]
  Associated Looker Board: [REDACTED]

HOW IT WORKS (HIGH LEVEL)
  1. The operator types a property name into the Recipients section of the Web
     App (or uses Mass Notify → Looker Sync → Fetch recipients from Looker…
     from the sheet menu) to automatically pull active occupants from Looker
     dashboard 4552. The script sanitises duplicates, resolves shared emails
     using applicant alt-email columns, and populates the Mass_Notification
     sheet. Rows that cannot be automatically resolved are flagged REVIEW.
  2. The operator selects a notification template from the "Mass Notify" custom
     menu (e.g., "Water Outage"). The Config sheet is pre-filled with subject,
     body, greeting, and disclaimer. The operator fills in property-specific
     fields (property name, dates, manager email, etc.).
  3. The operator runs "Dry Run → Preview" to review a sample email.
  4. When ready, the operator runs "Send" to deliver emails to all eligible
     recipients in the Recipients sheet.
  5. Every send operation is logged to the Run_Log sheet with a full audit
     trail (timestamp, sender, recipient count, config snapshot, before/after
     state). Operations can be undone from the Run_Log.

KEY SHEETS
  - Config          Key-value configuration table. Controls all runtime behavior.
  - Mass_Notification  One row per resident: email, name, unit, status,
                       attachments. Used by Individual and BCC modes.
  - Move_In_Flow    (v3.3.0+) One row per approved reservation: reservation
                    ID, property name + email(s), apartment number, member
                    contact, move-in/out dates, vehicle/pet, occupants,
                    area-manager block, attachment IDs (file or folder),
                    status, last-sent, notes. Used by Move-In Flow mode.
  - Run_Log         Audit trail. Captures every send with before/after state.
                    The captured columns are now schema-aware: resident sends
                    log email/name/unit; Move-In sends log reservation_id /
                    property_email / member_name / apt_number. Status and
                    last-sent column indices are persisted in the snapshot
                    so "Undo last run" works across both layouts.

DATABASE (PostgreSQL on Railway)
  Campaign and recipient data is now archived to a PostgreSQL database instead
  of hidden Google Sheets tabs. The database serves as the long-term audit trail.

  Schema: mass_notifications
    - campaigns     One row per send operation (run_id, mode, actor, config snapshot)
    - recipients    One row per recipient per campaign (sent + skipped, all logged)

  A "Database" custom menu in the spreadsheet provides query access:
    - Query by date        All recipients from campaigns on a given date
    - Query by recipient   Full notification history for an email address
    - Query by property    All campaigns for a property (partial match)
    - Query by actor       All campaigns triggered by a specific sender
    - Test DB connection   Verify Railway PostgreSQL connectivity

  DB credentials are stored in Apps Script Script Properties (never in code).
  Migration SQL lives in database/migrations/ and is version-controlled.

SENDING MODES
  - Individual      One personalized email per recipient (uses first name, unit).
  - BCC             Batched sends with all recipients in BCC (less personalized,
                    useful for very large lists).
  - Move-In Flow    (v3.3.0+) Property-facing notifications. One email per row
                    in the dedicated "Move_In_Flow" tab, sent to that row's
                    Property Email contacts (not to the member). Each email
                    carries the approved member's apartment, contact info,
                    move-in date, vehicle info (pipe-delimited
                    "Year|Make|Model|Color|License Plate|State"; multi-vehicle
                    rows separated by ";"; blank renders as N/A; legacy free
                    text falls through unchanged), pet/ESA info (pipe-delimited
                    "Animal|Breed|Weight|ESA?|Name"; multi-pet rows separated
                    by ";"; blank renders as N/A; legacy free text falls
                    through unchanged),
                    additional occupants (pipe-delimited "Name|Phone|Email"),
                    and a per-row Landing area-manager sign-off block.
                    Background-check + ID-scan PDFs travel as attachments.
                    Renders inside the official Landing branded wrapper
                    (cream background, LANDING wordmark header, dark-navy
                    footer with phone/address/copyright).

TEMPLATES
  Seven pre-built templates are included. Each populates the Config sheet
  with appropriate subject, body, greeting, and disclaimer HTML:
    - Annual Fire Inspection
    - Water Outage
    - General Maintenance
    - Weather Alert
    - Power Outage
    - WiFi Outage
    - Move-In Notification    (v3.3.0+, switches send_mode to MOVE_IN and
                               points the recipients sheet at "Move_In_Flow")

  Templates support dynamic tokens: {{first_name}}, {{unit}}, {{property_name}},
  {{event_name}}, {{date_range}}, {{today}}. Fallback syntax: {{first_name | Resident}}
  uses "Resident" if the field is empty.

  Move-In Notification additionally exposes per-row tokens:
  {{member_name}}, {{member_email}}, {{member_phone}}, {{apartment_number}},
  {{move_in_date}}, {{move_out_date}}, {{vehicle_info}}, {{pet_info}},
  {{area_mgr_name}}, {{area_mgr_phone}}, {{area_mgr_email}}.

ATTACHMENTS
  Attachments can be added at the Config level (sent to all recipients) or
  per-row in the Recipients / Move_In_Flow sheet (sent only to that row).
  Files are retrieved from Google Drive by ID. Google Docs are auto-exported
  as PDFs.

  As of v3.3.0, the Attachment IDs cell accepts either a comma-separated list
  of file IDs OR a single Drive FOLDER ID — every direct file in the folder
  is attached automatically (subfolders are not recursed). The 20 MB Gmail
  per-message cap still applies to the expanded list; rows that exceed it
  are flagged REVIEW with the offending total in Notes.

SAFETY FEATURES
  - Dry Run mode: preview HTML, create Gmail drafts, or test-send to yourself.
  - Run_Log undo: "Undo Last Run" reverses status changes from the previous send.
  - Validation: script validates config before any send attempt.
  - Recipients are skipped if their status is not blank, PENDING, or READY.

CURRENT LIMITATIONS / KNOWN ISSUES
  - Bandwidth to send time-crucial notifications is ops-dependent. On a busy day critical
  notifications might not get sent immediately if there are no available Managers.
  - Gmail daily send limits apply.
  - Specific cases where a template may not be useful (e.g. a one-off extraordinary situation)
  require manually composing the body HTML in the Config sheet.
  - Looker sync rows flagged REVIEW (triplicate+ emails with no resolvable alt address) require
  manual resolution before sending.

CONTACTS / OWNERSHIP
  Script maintained by: Maximiliano Pérez García
  Frequent users: Member Support Managers, Specialists, and Remote GMs
  For issues: Message Max Pérez via Slack or create a post in #ert-member-support

--------------------------------------------------------------------------------
  2. QA AUTOMATION
--------------------------------------------------------------------------------

PURPOSE
  Automatically sends formatted QA feedback emails to call center agents
  immediately after a Quality Assurance evaluation is submitted via Google Form.
  Each email includes a color-coded score breakdown, strengths and improvement
  areas, a link to the call recording, and a historical trend chart showing the
  agent's progression over past evaluations.

WHERE IT LIVES
  Google Sheets (with bound Form) — deployed as a bound Google Apps Script project.
  Spreadsheet URL: https://docs.google.com/spreadsheets/d/1DRGWd-YgOrAQyGJdZ6VVTP54FbG9vKPMAwUCmBMPV8k/edit?gid=884336497#gid=884336497
  Apps Script project ID: 1fuuvwcA4Z3aka1rkJ9ixlRL8nFWD98BrFqseJw-T_k2t5LiE2iQDlpSu
  Google Form URL: https://docs.google.com/forms/d/e/1FAIpQLSchilaGKHW2fwD-IeslNq20NiWoQmsBHFcxSycEj2-ElljYng/viewform

HOW IT WORKS (HIGH LEVEL, POST-PHASE-2)

  Single AI-assisted flow — the legacy Google-Form-trigger path was
  retired with the Phase-C bridge cleanup (May 2026). All scoring now
  goes through the Python backend; Apps Script's role is dispatching
  the QA email from a finalized Analyst_History row.

  Stage 1 (Score):
    Manager uploads audio at /score/{team_id}. Gemini 2.5 Flash scores
    AI-applicable sections with confidence + reasoning. The full draft
    row (scores, reasoning, confidence, feedback, caller meta) is
    written to that team's Form Responses AI sheet.

  Stage 1.5 (Edit):
    Manager reviews + edits the scorecard in the frontend. Manual
    sections (Y/N or 1-5 depending on score_type) are scored at this
    step with required reasoning. Edits stage back to FR-AI on Approve.

  Stage 2 (Score destination):
    Backend mirrors the row to the team's score-destination tab.
    Member Support: a separate "Form Responses 1" tab with formulas.
    Sales (post-May 2026): destination collapsed onto Form Responses
    AI itself — Stage 2 short-circuits (no append), the same row holds
    the ARRAYFORMULA-computed overall score in col F.

  Stage 3 (Readback):
    Backend polls the score_readback_col until the team's weighted
    ARRAYFORMULA fires (3-5 s typical). MS writes the resolved score
    back to FR-AI col F; Sales skips the writeback (would clobber the
    formula's output range on the same cell).

  Stage 4 (Finalize):
    Backend writes the canonical row to Analyst_History — agent_email
    resolved via Mails lookup, evaluator_email locked in, timestamp
    refreshed to approval time.

  Stage 5 (Email):
    Backend POSTs the Analyst_History row number to the team's Apps
    Script doPost endpoint. Apps Script reads the row, constructs
    ScoreCard + FeedbackCard + ProgressionCard, and sends the QA
    evaluation email via Gmail. No second append, no enrichment lookup
    — the row is already complete.

KEY SHEETS (per-team — same tab names, different layouts)
  - Form Responses AI   Draft row written by Gemini scoring + analyst
                        edits. For Sales, also holds the
                        ARRAYFORMULA-computed overall score (col F) —
                        the score destination collapsed onto this tab.
                        For MS, the score destination remains a
                        separate "Form Responses 1" tab.
  - Analyst_History     Source of truth for dashboards + DataPoint
                        pages. Derived layout (HistoryLayout(N)):
                        6 prefix + N scores + N reasoning + N
                        confidence + 6 trailing. MS N=10 (42 cols);
                        Sales N=19 (69 cols).
  - Mails               Agent name-to-email mapping (cols A-B),
                        supervisor (C), canonical name (D). Drives
                        active-agent filtering on the team dashboard.
  - Scores              (MS only — Sales' has been deprecated.) Mirror
                        of FR-AI per row with the weighted scoring
                        formula at the readback column.

QA SCORECARD CATEGORIES (rubric per team — driven by JSON config)
  Member Support (N=10):
    Numeric 1-5 (AI-scored):  Greeting, Purpose of Call, Matching the
      Moment, Process Adherence, Call Resolution, Communication,
      Efficiency & Call Handling
    Numeric 1-5 (manual):     Documentation
    Y/N (AI-scored):          Caller Identity Validation,
                              Customer Resolution Indicator

  Sales (N=19):
    Numeric 1-5 (AI-scored):  Situation Match, Landing Value Uplift,
                              Landing Guarantee, Objection Handling
    Y/N (manual, "manual_yn"): PB Created, MC Call Notes
                              (supervisor-verified; AI cannot score)
    Y/N (AI-scored):          13 sections covering greeting, reason
                              for move, membership, FLEX, pricing,
                              book attempt, urgency, follow-up,
                              tonality/pace, hold usage, audio
                              quality, screen recording, pre-send
                              intro

  Overall score is calculated on a 0–100 scale (per-section weights
  live on each team's Sheet as an ARRAYFORMULA reference range). Goal
  threshold: 85 (both teams).

COLOR CODING (score → color)
  Numeric categories (1–5):   Green ≥ 4.25 / Amber ≥ 3.5 / Red < 3.5
  Overall score (0–100):      Gold ≥ 100 / Green ≥ 85 / Amber ≥ 70 / Red < 70

TREND / PROGRESSION
  The ProgressionCard shows the agent's last 5 evaluations with:
    - Date, overall score, trend arrow (▲ up / ▼ down / ● neutral), mini progress bar
    - Goal marker at 85 on the progress bar
    - Special messaging for a perfect call (≥ 100) or goal reached (≥ 85)
    - First-evaluation messaging for brand-new agents

DRY RUN / MANUAL TRIGGERS
  Removed in v2.0.0. The custom "QA Automation" menu (Process Latest
  Row, Create Draft, Rebuild History) operated on the legacy Google
  Form path and the FR1 22-column layout. With Phase 2's schema
  convergence + Tier 4.6 bridge strip, those entry points no longer
  match the data shape. The /score frontend is now the only path.

WHAT'S NEW IN v2.0.0 (Phase 2 — May 2026)
  - Multi-team support. Sales onboarded as the second team. Rubric +
    sheet layout + branding all per-team via JSON config. Adding a
    new team requires authoring `backend/config/teams/{id}.json`,
    `teams/{id}/Branding.js`, running `build_config.py {id}`, then
    `./push.sh qa-{id}`. No core code edits.
  - HistoryLayout(N) derived schema. Both MS and Sales Analyst_History
    tabs share a canonical shape: 6 prefix + N scores + N reasoning +
    N confidence + 6 trailing. Cross-team analytics walk this shape
    using only `section_number` and `history_id`.
  - Bridge strip (Tier 4.6). Apps Script reduced to a dumb email
    dispatcher — no more `_processRow`, `onFormSubmit`,
    `rebuildHistory`, FR1 layout, or enrichment lookup. ~994 lines
    deleted across `src/`.
  - `score_type='manual_yn'`. New schema value for analyst-input Y/N
    sections that AI cannot score (Sales PB Created + MC Call Notes).
  - Sales score destination collapsed onto Form Responses AI itself —
    no more separate "Scores" tab. Stage 2 short-circuits, Stage 3
    skips the writeback so the on-row ARRAYFORMULA stays intact.
  - Team-level dashboards (/dashboard/{team_id}) data-drive every
    section column from `/api/{team_id}/team/sections`. Per-agent
    chart hides sections with no parseable data so legacy migrated
    rows don't render as zero-height bars.
  - Frontend pages emit `Cache-Control: no-cache` so reloads always
    pick up fresh HTML during development.
  - All team-aware page routes follow `/{role}/{team_id}/...` (e.g.
    `/score/sales`, `/dashboard/member_support/agent/Erick`,
    `/datapoint/sales/{call_id}`). Legacy single-team URLs still
    redirect to `member_support` for 30 days.

CURRENT LIMITATIONS / KNOWN ISSUES
    - Per-team ARRAYFORMULAs may take 3-5 s to resolve; the backend
      polls with a bounded retry (5 attempts × 800 ms = 4 s ceiling).
    - Manager-side: Sales `Mails` sheet currently has 0 supervisors
      populated; supervisor filter on the Sales team dashboard is
      always empty until that's filled in.
    - Dialpad API key expires regularly (~1 hour); must be refreshed
      in Railway env vars until OAuth with Client ID/Secret is set up
      with engineering.
    - Historical DataPoints from the Sales Phase-E migration may lack
      caller metadata and AI reasoning (only scores + Y/N values were
      migrated). Forward-going evaluations have the full set.

CONTACTS / OWNERSHIP
  Script maintained by: Maximiliano Pérez García
  QA Automation users: Member Support Managers, Gaby (MX Ops VP), Executive Resolutions
  For issues: Message Max Pérez via Slack or create a post in #ert-member-support

--------------------------------------------------------------------------------
  3. AI-SCORING (v2.0 — DEPLOYED ON RAILWAY)
--------------------------------------------------------------------------------

PURPOSE
  Python-based backend that uses Gemini 2.5 Flash to score agent calls from
  audio recordings. Managers review and edit scores in an interactive frontend,
  then approve to trigger the Apps Script email pipeline. Dashboards provide
  per-agent progression, team analytics, and evaluation drill-down pages.

STATUS
  Phase 0 — complete. Validated against real calls.
  Phase 1 — complete. FastAPI scoring pipeline + Sheets integration.
  Step 1  — complete. Security hardening + Railway deployment (API key auth,
            CORS restrictions, JSONL audit logging, dual credential loading).
  Step 1.5 — complete. Manager approval workflow (editable scorecard,
            Approve & Send button, doPost integration).
  Step 2  — complete. DataPoints (evaluation drill-down, clickable charts,
            detail page, caller metadata, backfill script).

  Deployed at: landing-scripts-production.up.railway.app (Railway Hobby tier)
  Local dev:   localhost:8000

  Scoring: Managers upload audio, Gemini scores all sections, results shown
  as an editable scorecard. Manager adjusts scores/reasoning, adds Documentation
  score, clicks "Approve & Send". Email is sent, history is updated.

  Dashboards:
    /dashboard              Team analytics (EWMA, SPC, outliers, distribution
                            with drill-down, section analysis, supervisor views)
    /dashboard/agent/{name} Per-agent trends, section averages, Gemini coaching
    /datapoint/{call_id}    Single evaluation detail (scores, reasoning, caller
                            info, feedback, QA progression mini-history)

CURRENT STACK
  - Google Gemini 2.5 Flash   AI audio analysis, scoring, coaching assessments
  - FastAPI (Python 3.9+)     Backend API (uvicorn, deployed on Railway)
  - gspread + Sheets API      Read/write Google Sheets
  - Chart.js                  Dashboard visualizations (CDN, no build step)
  - Dialpad API               Agent lookup, call list, transcripts, call details
  - Railway                   Hosting (Hobby tier), auto-deploy from GitHub
  - PostgreSQL                Schema stubbed (database/migrations/003), future use

SECURITY
  - API key authentication per team (Authorization: Bearer header)
  - CORS restricted to Railway domain + localhost
  - JSONL audit logging on every request
  - Frontend prompts for API key on first use (sessionStorage)

DEPLOYMENT ROADMAP
  Phase 0 (complete)     Validated AI scoring with real calls
  Step 1 (complete)      Security hardening + Railway deployment
  Step 1.5 (complete)    Manager approval workflow (Approve & Send)
  Step 2 (complete)      DataPoints — evaluation drill-down + clickable charts
  Step 3 (complete)      Rubric abstraction (JSON config per team)
  Step 4 (complete)      Team routing + multi-team support
  Step 5 (complete)      Sales team onboarding — May 2026
  Step 6 (next)          SOP/Notion RAG integration
  Step 7 (planned)       Cost tracking + admin dashboard
  Step 8 (planned)       Dialpad webhook automation (fully hands-free)
  Step 9 (planned)       PostgreSQL migration

  Phase 0/1 history:  qa-automation/AI-Scoring/references/PhaseOne.md
  Phase 2 history:    qa-automation/AI-Scoring/references/PhaseTwo.md

KEY PRINCIPLES
  - Human review is mandatory. AI proposes; managers approve via Approve & Send.
  - Manual sections (MS Documentation, Sales PB Created + MC Call Notes)
    are always scored by the manager — AI never sees them.
  - Apps Script is a dumb email dispatcher. The entire scoring +
    finalization pipeline lives in Python; Apps Script only reads the
    Analyst_History row and sends the email.
  - Data isolation: original AI scores preserved in Form Responses AI as audit trail.
  - Adding a new team requires configuration, not code changes:
    1) Author backend/config/teams/{id}.json, 2) Write teams/{id}/Branding.js,
    3) Generate teams/{id}/Config.js via build_config.py,
    4) ./push.sh qa-{id} to deploy.

  Full specification: qa-automation/AI-Scoring/references/CLAUDE.md
  Multi-team PRD: qa-automation/AI-Scoring/references/PRD-MultiTeam.md
  Phase 2 retrospective: qa-automation/AI-Scoring/references/PhaseTwo.md

--------------------------------------------------------------------------------
  GOOGLE APIS & PERMISSIONS USED
--------------------------------------------------------------------------------

  Both scripts require the following Google Workspace APIs:

  Mass Notifications:
    - Google Sheets API   (read config, recipients; write run log, archive)
    - Gmail API           (send emails, create drafts)
    - Google Drive API    (retrieve and export attachments)
    - Looker API          (retrieve occupants list)

  QA Automation (Apps Script):
    - Google Sheets API   (read form responses; read/write Analyst_History)
    - Gmail API           (send emails, create drafts)
    - Google Forms        (onFormSubmit trigger)

  AI-Scoring (Python backend):
    - Google Gemini API   (audio scoring + progression assessments)
    - Google Sheets API   (read/write Form Responses AI, Form Responses 1,
                           and Analyst_History via service account)
    - Dialpad API         (agent lookup, call list, transcripts, call details)

  Mass Notifications also uses:
    - PostgreSQL (JDBC)  (archive campaigns and recipients to Railway-hosted DB)

  All Apps Scripts run under the Google account of the authorized operator.
  AI-Scoring uses a Google service account for Sheets access and a Gemini API key.

--------------------------------------------------------------------------------
  DEPLOYMENT & SETUP
--------------------------------------------------------------------------------

  Both scripts are deployed as bound Google Apps Script projects within their
  respective Google Sheets. No additional installation is required for end users.

  For developers making changes:
    1. Open the Google Sheet → Extensions → Apps Script.
    2. Edit the relevant .js / .gs files directly in the Apps Script editor,
       OR use the clasp CLI to push changes from this repository.
       (clasp config is available for Mass Notifications in mass-notifications/.clasp.json)
    3. For trigger-based scripts (QA Automation), ensure the onFormSubmit trigger
       is registered under Triggers in the Apps Script editor.
    4. Test changes using Dry Run / Draft mode before enabling live sends.
    5. Looker API credentials (Client ID and Client Secret) are stored in GAS
       Script Properties — never in the sheet. Run Mass Notify → Looker Sync →
       Setup credentials once after a fresh deployment. Credentials are provided
       by the BI Manager and are scoped to the landing.cloud.looker.com instance.

 

--------------------------------------------------------------------------------
  GLOSSARY
--------------------------------------------------------------------------------

  GAS           Google Apps Script (JavaScript runtime inside Google Workspace)
  clasp         Command-line tool for pushing/pulling GAS projects to/from a repo
  Dry Run       A test mode that previews or drafts emails without sending them
  Run_Log       Audit trail sheet in Mass Notifications tracking every send
  Analyst_History  Sheet in QA Automation tracking all past evaluations per agent
  Dialpad       VoIP platform used for call center operations; source of call recordings
  RAG           Retrieval-Augmented Generation — using a knowledge base to ground AI
  Gemini Flash  Google's cost-efficient multimodal AI model used for audio scoring
  DataPoint     A single evaluation record accessible via /datapoint/{call_id}
  Railway       Cloud hosting platform for AI-Scoring backend + PostgreSQL
  JDBC          Java Database Connectivity — used by Apps Script to connect to Postgres
  JSONB         Binary JSON type in PostgreSQL; supports querying inside JSON fields

--------------------------------------------------------------------------------
  CONTRIBUTING & CHANGE MANAGEMENT
--------------------------------------------------------------------------------

  Change strategy: Notify Max Pérez via Slack of necessary changes or bugs. 
  As of today, there are no other contributors to this repo so all commits, PRs, and 
  issues are managed by Max.

  Branch strategy: feature branches → PR → merge to main.
  Railway auto-deploys on push to main (root dir: qa-automation/AI-Scoring).
  Apps Script changes deployed via clasp push from qa-automation/ directory.

--------------------------------------------------------------------------------
  SUPPORT & CONTACT
--------------------------------------------------------------------------------

  For questions, bug reports, or feature requests:
    Slack channel #ert-member-support, maximiliano.perez@hellolanding.com

  For urgent issues affecting live sends:
    Max Pérez on Slack

================================================================================
