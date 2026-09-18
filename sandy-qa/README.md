# Sandy App — qa-scoring

Landing's call-quality platform on Sandy: two-stage AI scoring (Gemini
annotate → Claude judge via the AI Gateway), analyst review, team and
agent analytics, the coaching loop, the Member Support EOD call report,
and — since v0.77 — the Supervisor Deliverables cycle (end-of-shift,
weekly, bi-weekly tracker, monthly) that the Member Support Direction index
requires.

Live: https://qa-scoring.sandy.hellolanding.tech · Design docs: `references/`
(one spec per feature; each carries its ladder and open items).

## Surfaces

| Path | What | Gate |
|---|---|---|
| `/dashboard/{team}` · `/dashboard/{team}/agent/{name}` | team + agent analytics, drill-downs | any SSO user |
| `/score/{team}` · `/lookup/{team}` · `/scorecard/{team}/{id}` | scoring console, call lookup, scorecard editor | admin \| qa |
| `/coaching/{team}` (+ `/session/{id}`) | coaching queue, sessions, tags | coach (admin \| qa \| manager) |
| `/supervisor/{team}` (+ `/report/{id}`, `/tracker/{date}`) | supervisor deliverables hub, report editor, Tuesday tracker | coach |
| `/admin` | roles + access requests | admin |
| `/api/v1/mcp` | MCP server over D1 | service token |

Cron (2-slot cap): hourly `7 * * * *` → qa-cron-ticker continuation (sweeps,
EOD report, supervisor pulls, queue pump); daily `37 9 * * *` → maintenance.

## Change log

> v0.78 : 2026-09-17
- Daily Agent QA Digest (references/DailyDigest.md): the nightly sweep's
  suppressed per-call scorecards are replaced by ONE email per active agent
  per sweep night — tonight-vs-month per-section averages, a Sonnet-written
  trend summary (qa-insights mode `daily_digest`, judge parity), the
  ProgressionCard strip (last 5 + tonight) and the plain month-to-date
  average (HR-bonus window). Migration 0021 (`qa_agent_digests`,
  `qa_cron_jobs.next_at`, MS `nightly_sweep.digest` config); `dailyDigest.ts`
  (facts, prompt, renderer, `daily_digest` cron-job handler); sweep done →
  digest job; `JobStepResult.wait_s` parks a waiting job instead of spinning
  the ticker. GAS gains a generic `{html_email}` sender branch
  (qa-automation/src/Main.js — push `qa-member-support`).
- Tests: `tests/daily_digest.test.mjs`.

> data migration 0020 : 2026-09-17 (no app deploy)
- Judge calibration — Member Support **Process Adherence is scoped
  exclusively to the retrieved SOP documents** (migration
  `0020_process_adherence_sop_scope.sql`, applied to live D1). The section's
  `rubric_question` now carries the scope rule (do not re-score greeting,
  verification, tone, holds, pacing, resolution — their own sections own
  those) and `special_reasoning_instructions` asks the judge to name the SOP
  document + step the score rests on. Call Resolution untouched (scores the
  outcome). Anchors, weights and `rubric_version` (member_support_v2)
  unchanged; picked up by the next enqueue.

> v0.77 : 2026-09-16
- Supervisor Deliverables (references/SupervisorDeliverables.md): migration
  0019 (`qa_sup_reports`, `qa_sup_pulls`, `qa_sup_action_plans`,
  `qa_sup_events`, `provider_config.deliverables`); facts library
  (`supervisorFacts.ts`), renderers (`supervisorRender.ts`), Snowflake
  gateway client (`snowflakeMcp.ts` — MC ticket counts, WFM parity), Slack
  seam (`slack.ts` — post + unattended-mention audit), routes
  (`routes/supervisor.ts`), three pages, "Supervisor" nav button, ticker
  step 3b (weekly Dialpad productivity + CSAT pull). New optional secrets:
  `SLACK_BOT_TOKEN`, `SLACK_USER_TOKEN`, `SNOWFLAKE_MCP_TOKEN`.
- Tests: `tests/supervisor.test.mjs` (period math, abandon rollup, QA
  coverage, coaching activity, plans, pull state machine, tracker,
  renderers).

> v0.76 and earlier
- See git history (`git log --oneline -- sandy-qa`) and the per-feature specs
  in `references/` (CronContinuation, NightlyScoring, ShiftReport,
  CoachingLoopSpec, CoachingTagsSpec, AgentAddition, CostOptimization, …).
