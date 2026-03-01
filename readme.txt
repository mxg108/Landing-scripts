================================================================================
  LANDING SCRIPTS — INTERNAL AUTOMATION TOOLS
  Business Intelligence & Operations Automation
================================================================================

  Maintained by: [Maximiliano Pérez / Member Support Management]
  Department:    Operations
  Last Updated:  February 2026
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

A third component, AI-Scoring, is currently in early development (Phase 0)
and will eventually extend the QA Automation system with AI-powered call
scoring. It is documented separately in qa-automation/AI-Scoring/CLAUDE.md.

Both live scripts run inside Google Sheets as custom menus (Mass Notifications)
or form triggers (QA Automation). No external servers are required for the
current production versions.

--------------------------------------------------------------------------------
  REPOSITORY STRUCTURE
--------------------------------------------------------------------------------

  landing-scripts/
  ├── mass-notifications/
  │   └── src/                  Google Apps Script files (.gs)
  │       ├── Config.gs         Runtime configuration management
  │       ├── Mailer.gs         Core email sending logic
  │       ├── Templates.gs      Pre-built notification templates
  │       ├── Tokenizer.gs      Dynamic token substitution in email body
  │       ├── DryRun.gs         Preview and test-send modes
  │       ├── Recipients.gs     Recipient list handling
  │       ├── RunLog.gs         Audit trail logging
  │       ├── Attachments.gs    File attachment handling via Drive
  │       ├── Cards.gs          Reusable HTML content blocks
  │       ├── Reset.gs          Undo / archive / reset operations
  │       ├── UI.gs             Custom menu and sidebar interface
  │       └── Utils.gs          Shared utility functions
  │
  └── qa-automation/
      └── src/                  Google Apps Script files (.js)
          ├── Main.js           Entry points and processing pipeline
          ├── Config.js         Column mappings and scoring thresholds
          ├── QAEntry.js        Data model for a single QA row
          ├── AnalystHistory.js Agent history tracking
          ├── ScoreCard.js      HTML score breakdown visualization
          ├── FeedbackCard.js   HTML strengths/improvements visualization
          ├── ProgressionCard.js HTML trend and progression visualization
          ├── HtmlRenderer.js   Full email template assembly
          └── EmailSender.js    Gmail integration (send or draft)

  qa-automation/AI-Scoring/     Python backend (Phase 0 — in development)
  └── See CLAUDE.md inside for detailed spec and roadmap

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
  Spreadsheet URL: https://docs.google.com/spreadsheets/d/1YBl8ePRVvtAuRmYO708Gif33iMRXxZyKNhGi9MY--FA/edit?gid=1543620309#gid=1543620309
  Apps Script project ID: 10uCQwh03sRsBbqb2LsWF9K2q3EwL396xxv4tySnwIsIZRTlaiEb53hpW
  Associated Looker Board: https://landing.cloud.looker.com/dashboards/4552?Reservation+Platform=&Property+Name=

HOW IT WORKS (HIGH LEVEL)
  1. The operator opens the Google Sheet and selects a notification template
     from the "Mass Notify" custom menu (e.g., "Water Outage").
  2. The Config sheet is pre-filled with subject, body, greeting, and
     disclaimer. The operator fills in property-specific fields (property name,
     dates, manager email, etc.).
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
  - Archive         Historical recipients moved here after a run is cleared.

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
  - Manually querying a Looker board and copy-pasting the active occupant table is a cumbersome
  and time-consuming process.
  - Bandwidth to send time-crucial notifications is ops-dependent. On a busy day critical
  notifications might not get sent immediately if there are no available Managers
  - Gmail daily send limits, 
  - Specific cases where a template may not be useful, i. e. a one-off extraordinary situation

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
  1. A QA Analyst submits a completed evaluation via the Google Form.
  2. Google Forms appends the response as a new row in "Form Responses 1" sheet.
  3. QA Analyst reviews row entry, and manually selects the option they want from the custom 
  UI Menu.
  4. The script parses the row into a structured QA entry.
  5. It retrieves the agent's past evaluations from the Analyst_History sheet.
  6. It builds three HTML components:
       - ScoreCard      — color-coded breakdown of all scored categories
       - FeedbackCard   — key strengths, areas to improve, Dialpad call link
       - ProgressionCard — trend table showing last N evaluations with score deltas
  7. The full HTML email is sent to the agent's email address.
  8. The new entry is appended to Analyst_History for use in future emails.

KEY SHEETS
  - Form Responses 1   Raw QA form submissions (auto-populated by Google Forms).
  - Analyst_History    Running log of all past evaluations per agent.
                       Used to generate trend data in the ProgressionCard.

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
    - onFormSubmit runs automatically, there is no way for a score to be logged without 
    an email being sent.
    - Columns with ARRAYFORMULAs might not resolve some triggers/function calls
    - QA Anallysts still need to manually pick and score every call

CONTACTS / OWNERSHIP
  Script maintained by: Maximiliano Pérez García
  QA Automation users: Member Support Managers, Gaby (MX Ops VP), Executive Resolutions
  For issues: Message Max Pérez via Slack or create a post in #ert-member-support

--------------------------------------------------------------------------------
  3. AI-SCORING (IN DEVELOPMENT — PHASE 0)
--------------------------------------------------------------------------------

PURPOSE
  A planned Python-based backend that will use AI (Google Gemini 2.5 Flash or newer) to
  automatically score agent calls from audio recordings. This is intended to
  reduce the manual workload of QA analysts and increase scoring throughput.

STATUS
  Phase 0 — validation. All backend files are scaffolded but not yet implemented.
  Phase 0 goal: validate AI scoring quality against 20–25 real calls before
  committing to full buildout.

PLANNED STACK
  - Google Gemini 2.5 Flash   AI audio analysis and scoring
  - FastAPI                   Backend API
  - Google Sheets API         Write AI-proposed scores back to the QA sheet
  - ChromaDB                  Vector store for SOPs (RAG-based rubric grounding)
  - PostgreSQL                Persistent database
  - Next.js or Retool         Manager review dashboard (Phase 3)

DEPLOYMENT ROADMAP
  Phase 0 (Weeks  1–3)  Validate AI scoring with real calls (score_call.py)
  Phase 1 (Weeks  4–7)  FastAPI backend; AI writes draft scores to Sheets
  Phase 2 (Weeks  8–12) Notion SOP sync + ChromaDB RAG integration
  Phase 3 (Weeks 13–18) Manager review dashboard (Next.js or Retool)
  Phase 4 (Weeks 19–24) Dialpad webhook automation (fully hands-free)

KEY PRINCIPLES
  - Human review is mandatory at launch. AI proposes; managers approve.
  - Documentation (Section 9 of scorecard) is never automated.
  - The existing Apps Script email flow is preserved until Phase 3.

  Full specification: qa-automation/AI-Scoring/CLAUDE.md

--------------------------------------------------------------------------------
  GOOGLE APIS & PERMISSIONS USED
--------------------------------------------------------------------------------

  Both scripts require the following Google Workspace APIs:

  Mass Notifications:
    - Google Sheets API   (read config, recipients; write run log, archive)
    - Gmail API           (send emails, create drafts)
    - Google Drive API    (retrieve and export attachments)
    - Looker API          (retrieve occupants list)

  QA Automation:
    - Google Sheets API   (read form responses; read/write Analyst_History)
    - Gmail API           (send emails, create drafts)
    - Google Forms        (onFormSubmit trigger)

  All scripts run under the Google account of the authorized operator.
  No external servers or third-party services are used in production.

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

--------------------------------------------------------------------------------
  CONTRIBUTING & CHANGE MANAGEMENT
--------------------------------------------------------------------------------

  Change strategy: Notify Max Pérez via Slack of necessary changes or bugs. 
  As of today, there are no other contributors to this repo so all commits, PRs, and 
  issues are managed by Max.

  Branch strategy: feature branches → PR → merge to main.
  Current active branch: feature/ai-scoring (AI-Scoring Phase 0 scaffolding)

--------------------------------------------------------------------------------
  SUPPORT & CONTACT
--------------------------------------------------------------------------------

  For questions, bug reports, or feature requests:
    Slack channel #ert-member-support, maximiliano.perez@hellolanding.com

  For urgent issues affecting live sends:
    Max Pérez on Slack

================================================================================
