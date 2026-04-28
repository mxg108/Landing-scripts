# Sales — QA Automation deployment

Placeholder until Sales' Apps Script is bound to their QA spreadsheet.

## Onboarding

1. Create a new Apps Script project bound to the Sales QA Google Sheet.
2. Drop `.clasp.json` here with the new scriptId:
   ```json
   {
     "scriptId": "<sales-script-id>",
     "rootDir": "."
   }
   ```
3. Drop `Config.js` here — copy from `../member_support/Config.js` and edit
   the team-specific values (sheet names, column indices, brand colors,
   email subject template, mails sheet, QA goal, etc.).
4. Verify the staging step:
   ```bash
   ./push.sh qa-sales --dry-run
   ```
5. Live deploy:
   ```bash
   ./push.sh qa-sales
   ```

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
ONLY `Config.js` and `.clasp.json` and break the script. Always go through
`./push.sh qa-sales`, which stages the full overlay into `.build/sales/`.
