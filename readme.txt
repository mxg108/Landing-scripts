================================================================================
  LANDING SCRIPTS — INTERNAL AUTOMATION TOOLS
  Business Intelligence & Operations Automation
================================================================================

  Maintained by: [Maximiliano Pérez / Member Support Management]
  Department:    Operations
  Last Updated:  March 2026
  Status:        Active — both scripts are live and in use

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
AI-Scoring grades calls via Gemini and writes to Form Responses AI, while
the Apps Script enriches Analyst_History with AI reasoning when a manager
approves and sends the email. The agent progression dashboard reads from
Analyst_History to show trends and Gemini-generated coaching assessments.

Mass Notifications and QA Automation run inside Google Sheets as custom menus
or form triggers. AI-Scoring runs as a FastAPI server (local or Railway).

--------------------------------------------------------------------------------
  REPOSITORY STRUCTURE
--------------------------------------------------------------------------------

  landing-scripts/
  ├── database/
  │   └── migrations/           SQL migration files
  │       └── 001_mass_notifications_schema.sql
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
      ├── src/                  Google Apps Script files (.js)
      │   ├── Main.js           Entry points and processing pipeline
      │   ├── Config.js         Column mappings, scoring thresholds, sheet names
      │   ├── QAEntry.js        Data model for a single QA row
      │   ├── AnalystHistory.js Agent history tracking + Form AI enrichment
      │   ├── ScoreCard.js      HTML score breakdown visualization
      │   ├── FeedbackCard.js   HTML strengths/improvements visualization
      │   ├── ProgressionCard.js HTML trend and progression visualization
      │   ├── HtmlRenderer.js   Full email template assembly
      │   └── EmailSender.js    Gmail integration (send or draft)
      │
      └── AI-Scoring/           Python backend (v1.1.0 — Phase 1 live)
          ├── CLAUDE.md         Full project spec and roadmap
          ├── AgentProgressionDashboard.md  Dashboard design doc
          ├── backend/
          │   ├── main.py           FastAPI app entry point
          │   ├── models/
          │   │   ├── scorecard.py  Scoring pipeline models
          │   │   └── dashboard.py  Dashboard / progression models
          │   ├── prompts/
          │   │   ├── qa_scoring_prompt.py   Call scoring prompt
          │   │   └── progression_prompt.py  Agent coaching prompt
          │   ├── routes/
          │   │   ├── scoring.py    /api/score endpoints
          │   │   └── dashboard.py  /api/agents + /api/agents/{name}/*
          │   └── services/
          │       ├── scoring_service.py     Gemini call scoring
          │       ├── sheets_service.py      Write to Form Responses AI
          │       ├── dialpad_client.py      Dialpad API integration
          │       ├── data_provider.py       Abstract data provider
          │       ├── history_service.py     SheetsProvider (Analyst_History)
          │       ├── db_provider.py         PostgresProvider (stub)
          │       └── progression_service.py Gemini coaching assessments
          ├── frontend/
          │   ├── index.html        Call scoring upload UI
          │   └── dashboard.html    Agent progression dashboard
          └── scripts/
              └── score_call.py     Phase 0 CLI scoring script

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
  - Recipients      One row per resident: email, name, unit, status, attachments.
  - Run_Log         Audit trail. Captures every send with before/after state.

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

TEMPLATES
  Six pre-built templates are included. Each populates the Config sheet
  with appropriate subject, body, greeting, and disclaimer HTML:
    - Annual Fire Inspection
    - Water Outage
    - General Maintenance
    - Weather Alert
    - Power Outage
    - WiFi Outage

  Templates support dynamic tokens: {{first_name}}, {{unit}}, {{property_name}},
  {{event_name}}, {{date_range}}, {{today}}. Fallback syntax: {{first_name | Resident}}
  uses "Resident" if the field is empty.

ATTACHMENTS
  Attachments can be added at the Config level (sent to all recipients) or
  per-row in the Recipients sheet (sent only to that recipient). Files are
  retrieved from Google Drive by ID. Google Docs are auto-exported as PDFs.

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

HOW IT WORKS (HIGH LEVEL)
  Manual flow (Google Form):
    1. A QA Analyst submits a completed evaluation via the Google Form.
    2. Google Forms appends the response as a new row in "Form Responses 1" sheet.
    3. QA Analyst reviews the row and selects an action from the custom UI Menu.
    4. The script parses the row into a structured QA entry.
    5. It retrieves the agent's past evaluations from the Analyst_History sheet.
    6. It builds three HTML components:
         - ScoreCard      — color-coded breakdown of all scored categories
         - FeedbackCard   — key strengths, areas to improve, Dialpad call link
         - ProgressionCard — trend table showing last N evaluations with score deltas
    7. The full HTML email is sent to the agent's email address.
    8. The new entry is appended to Analyst_History (cols A-O).
    9. If the call was AI-scored, AnalystHistory enriches cols P-AK by looking
       up the matching Dialpad link in Form Responses AI and copying reasoning,
       confidence, key strengths, and improvements.

  AI-assisted flow (Gemini):
    1. Manager uploads audio to localhost:8000 (AI-Scoring frontend).
    2. Gemini 2.5 Flash scores all sections with confidence + reasoning.
    3. Results are written to Form Responses AI (cols A-P + reasoning cols Q-AI).
    4. Manager copy-pastes to Form Responses 1 and triggers the UI button.
    5. The Apps Script enrichment in step 9 above pulls the AI reasoning
       into Analyst_History automatically.

KEY SHEETS
  - Form Responses 1    QA form submissions (auto-populated by Google Forms).
  - Form Responses AI   AI-scored calls from the Gemini pipeline. Cols A-P mirror
                        Form Responses 1; cols Q-AI contain per-section reasoning.
  - Analyst_History     Running log of all past evaluations per agent (cols A-O
                        from Apps Script + cols P-AK enriched from Form AI).
                        Source of truth for the agent progression dashboard.
  - Mails              Agent name-to-email mapping (cols A and B).

QA SCORECARD CATEGORIES
  Numeric (scored 1–5):
    - Greeting & Introduction
    - Call Purpose / Needs Assessment
    - Match Moment
    - Process Adherence
    - Call Resolution
    - Communication
    - Efficiency
    - Documentation (manual-only; not targeted for AI automation)

  Binary (Yes / No):
    - Identity Validation
    - Customer Resolution

  Overall score is calculated on a 0–100 scale. Goal threshold: 85.

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
  A custom "QA Automation" menu is available in the Google Sheet with:
    - Process Latest Row   — manually processes the most recent form response
    - Create Draft         — generates a Gmail draft instead of sending (safe review)
    - Rebuild History      — reconstructs Analyst_History from scratch from form data

CURRENT LIMITATIONS / KNOWN ISSUES
    - Columns with ARRAYFORMULAs (Overall Score, Agent Email) may not resolve
      before the trigger fires; a 3-second sleep + Mails sheet lookup mitigates this.
    - QA Analysts still manually pick and score calls unless using AI-Scoring.
    - AI-scored calls require a copy-paste from Form Responses AI to Form
      Responses 1 before the email can be triggered.

CONTACTS / OWNERSHIP
  Script maintained by: Maximiliano Pérez García
  QA Automation users: Member Support Managers, Gaby (MX Ops VP), Executive Resolutions
  For issues: Message Max Pérez via Slack or create a post in #ert-member-support

--------------------------------------------------------------------------------
  3. AI-SCORING (v1.1.0 — PHASE 1 LIVE + AGENT DASHBOARD)
--------------------------------------------------------------------------------

PURPOSE
  Python-based backend that uses Gemini 2.5 Flash to score agent calls from
  audio recordings. Reduces QA analyst workload and provides AI-generated
  coaching assessments via an agent progression dashboard.

STATUS
  Phase 0 — complete. Validated against real calls.
  Phase 1 — live. FastAPI scoring pipeline + agent progression dashboard.

  Scoring: Managers upload audio at localhost:8000, Gemini scores all sections,
  results land in Form Responses AI with per-section reasoning.

  Dashboard: localhost:8000/dashboard shows per-agent score trends (7-90 days),
  per-section averages, and Gemini-generated coaching assessments. Reads from
  Analyst_History (enriched by Apps Script on email send).

CURRENT STACK
  - Google Gemini 2.5 Flash   AI audio analysis and scoring
  - FastAPI (Python 3.9+)     Backend API (uvicorn)
  - gspread + Sheets API      Read/write Google Sheets
  - Chart.js                  Dashboard visualizations (CDN, no build step)
  - Dialpad API               Agent lookup and call list retrieval
  - PostgreSQL                Stub ready for future use (Railway free tier)

DEPLOYMENT ROADMAP
  Phase 0 (complete)     Validated AI scoring with real calls (score_call.py)
  Phase 1 (live)         FastAPI backend + Sheets integration + dashboard
  Phase 1.1 (current)    Agent progression dashboard with Gemini coaching
  Phase 2 (planned)      Notion SOP sync + ChromaDB RAG integration
  Phase 3 (planned)      Full manager dashboard (Next.js or Retool)
  Phase 4 (planned)      Dialpad webhook automation (fully hands-free)

KEY PRINCIPLES
  - Human review is mandatory. AI proposes; managers approve via UI button.
  - Documentation (Section 9 of scorecard) is never automated.
  - The existing Apps Script email flow is preserved.

  Full specification: qa-automation/AI-Scoring/CLAUDE.md
  Dashboard design doc: qa-automation/AI-Scoring/AgentProgressionDashboard.md

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
    - Google Sheets API   (read/write Form Responses AI and Analyst_History)
    - Dialpad API         (agent lookup, call list retrieval)

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
  Railway       Cloud hosting platform for the PostgreSQL database
  JDBC          Java Database Connectivity — used by Apps Script to connect to Postgres
  JSONB         Binary JSON type in PostgreSQL; supports querying inside JSON fields

--------------------------------------------------------------------------------
  CONTRIBUTING & CHANGE MANAGEMENT
--------------------------------------------------------------------------------

  Change strategy: Notify Max Pérez via Slack of necessary changes or bugs. 
  As of today, there are no other contributors to this repo so all commits, PRs, and 
  issues are managed by Max.

  Branch strategy: feature branches → PR → merge to main.
  Active branches:
    feature/agent-progression-dashboard  Agent dashboard + Analyst_History enrichment
    feature/ai-scoring                   AI-Scoring Phase 1 pipeline (merged)
    feature/database-integration         PostgreSQL for Mass Notifications (merged)

--------------------------------------------------------------------------------
  SUPPORT & CONTACT
--------------------------------------------------------------------------------

  For questions, bug reports, or feature requests:
    Slack channel #ert-member-support, maximiliano.perez@hellolanding.com

  For urgent issues affecting live sends:
    Max Pérez on Slack

================================================================================
