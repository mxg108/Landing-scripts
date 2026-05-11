# Sales — QA Automation deployment

Placeholder until Sales' Apps Script is bound to their QA spreadsheet.

## Onboarding

1. Create a new Apps Script project bound to the Sales QA Google Sheet.
2. Drop `.clasp.json` here with the new scriptId. Use `rootDir: "./src"`
   (NOT `"."`) — `push.sh` rewrites it to `"."` automatically in the build
   dir. Keeping `"./src"` here is a deliberate foot-gun guard: if anyone
   runs `clasp push` directly from this directory, clasp resolves into a
   non-existent `src/` and no-ops safely instead of half-deploying only
   `Config.js`.
   ```json
   {
     "scriptId": "<sales-script-id>",
     "rootDir": "./src"
   }
   ```
3. Drop `Branding.js` here — copy from `../member_support/Branding.js` and
   tweak the team-specific brand/email constants (COLORS, EMAIL.SUBJECT_TEMPLATE,
   QA_GOAL, THRESHOLDS, FIRST_EVAL_MESSAGE). Apps Script loads files
   alphabetically, so Branding.js must declare `var CONFIG = {...}` first;
   the auto-generated `Config.js` mutates that object.
4. Generate `Config.js` from the team's JSON config:
   ```bash
   python qa-automation/scripts/build_config.py sales
   ```
   Re-run this whenever `qa-automation/AI-Scoring/backend/config/teams/sales.json`
   changes — Config.js is AUTO-GENERATED and any hand edits will be clobbered
   on the next regen.
5. Verify the staging step:
   ```bash
   ./push.sh qa-sales --dry-run
   ```
6. Live deploy:
   ```bash
   ./push.sh qa-sales
   ```
7. In the Apps Script editor: Deploy → Web App (execute as me, access anyone with
   link). Capture the deploy URL into the Sales API key's secret bundle as
   `APPS_SCRIPT_WEBAPP_URL_SALES`.

## What is shared with Member Support

Everything in `qa-automation/src/` (Main.js, ScoreCard.js, FeedbackCard.js,
HtmlRenderer.js, ProgressionCard.js, QAEntry.js, AnalystHistory.js,
EmailSender.js, appsscript.json) is shared logic and gets staged into
`qa-automation/.build/sales/` automatically by push.sh.

If Sales ever needs to override a shared file, drop a same-named file in
this directory — the overlay step replaces the shared one. Prefer pushing
variability into `Config.js` first; only fork a logic file when behavior
genuinely diverges.

## Do NOT run `clasp push` directly from here

This directory contains only overlays — pushing from here would deploy
ONLY `Config.js`, `Branding.js`, and `.clasp.json` and break the script.
Always go through `./push.sh qa-sales`, which stages the full overlay
into `.build/sales/`.
