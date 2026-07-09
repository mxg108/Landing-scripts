# HR Bonus Sheet — GAS suite

Monthly renderer for the HR-facing Agent Bonus workbook. Spec:
`qa-automation/AI-Scoring/references/HRBonusSheet.md` (§6). The backend
computes every number (`GET /api/{team_id}/hr-bonus/{month}`); this
suite fetches the JSON and writes the tabs — zero math on this side.

| File | Responsibility |
|---|---|
| `Main.js` | trigger entry (`runMonthlyExport`), manual re-run, trigger install, failure alerts |
| `ApiClient.js` | authenticated GET to the backend |
| `SheetWriter.js` | summary + detail tab rendering (clear-in-place, idempotent) |
| `Config.js` | static — team id only (everything dynamic arrives in the payload) |

## One-time setup (operator)

1. **Create the standalone Apps Script project** (not container-bound —
   the workbook stays swappable): `clasp create --type standalone
   --title "HR Bonus Sheet — Member Support"` from this directory, or in
   the web editor. Put the resulting script id into `.clasp.json`.
2. **Register is already done** — `push.projects` has the `hr-bonus`
   row; deploy with `./push.sh hr-bonus` from the repo root.
3. **Script Properties** (Project Settings → Script Properties):
   - `BACKEND_BASE_URL` — the Railway app origin, no trailing slash
   - `HR_BONUS_API_KEY` — the member_support team API key
   - `HR_BONUS_SPREADSHEET_ID` — the HR bonus workbook id
   - `ALERT_EMAIL` — (optional) failure-alert recipient; defaults to the
     trigger owner
4. **Install the trigger**: run `installMonthlyTrigger()` once from the
   editor. It fires on the 1st of each month at ~06:00
   America/Los_Angeles (the script timezone) and exports the month that
   just ended. Re-running the installer never stacks duplicate triggers.

## Operations

- **Re-run any month**: set Script Property `MANUAL_MONTH` to `YYYY-MM`
  and run `runManualExport()`. Re-runs are idempotent — tabs are cleared
  and rewritten in place; nothing is ever deleted.
- **Failures**: the operator gets a `MailApp` alert with the month and
  error; the execution also shows in the Apps Script executions log.
  Fix the cause and re-run — no cleanup needed.
- **First supervised run** (spec P4): 2026-08-01 for July data, operator
  watching; verify the alert path by running once with a deliberately
  wrong `HR_BONUS_API_KEY` beforehand if desired.
