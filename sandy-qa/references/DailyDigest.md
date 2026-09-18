# Daily Agent QA Digest — design (DD0) + ladder

*2026-09-17, owner-directed ("for tonight's sweep"). Closes the slice parked
in NightlyScoring.md §0.2 / line 261: "Consolidated per-agent nightly email
replaces suppression."*

## 0. Owner decisions (defaults applied — change by config, not code)

| # | Question | Default applied |
|---|---|---|
| 1 | Who gets a digest | Every **active roster agent** (`qa_agents.active=1`) with ≥1 **finalized** eval among the night's sweep picks. Agents whose only picks are still in human review get nothing tonight (the scorecard follows the analyst's approval, as today). |
| 2 | Per-call scorecard emails for sweep evals | **Stay suppressed** (`nightly_sweep.suppress_email` untouched). The digest *is* the re-enable. Console / lookup / rescore emails are unchanged. |
| 3 | When it sends | After the sweep's queue drains, or at `digest.send_deadline_utc` (**13:30 UTC** ≈ 07:30 MX) with whatever is finalized — MS morning shift wants it by 14:00 UTC (CostOptimization §0 #1). |
| 4 | Summarizer model | **`claude-sonnet-5`** via `qa-insights` (judge parity; adaptive thinking is the default on Sonnet 5 — no temperature; `max_tokens` 6000 to leave room for thinking). Opus 5 is a one-key config swap (`digest.model`). |
| 5 | "Monthly average" | Plain mean of the agent's finalized evals in the **current calendar month, America/Los_Angeles bucketing** — the same window the HR bonus uses (`hrBonus.monthWindowUtc`), so the number the agent tracks is the number HR pays on. Not EWMA. |
| 6 | If the AI summary fails / times out | Send anyway, facts-only (per-section averages, progression, month average) with a one-line "trend summary unavailable tonight". Never hold the digest on the model. |
| 7 | CC | None by default. `digest.cc_supervisor: true` cc's `qa_agents.supervisor_email`. |
| 8 | Teams | Any team whose `provider_config.nightly_sweep.digest.enabled` is true — MS only tonight (migration 0021). |

## 1. Shape

```
sweep enqueue → done            qa_cron_jobs (job='daily_digest', key='<team>:<pull_date>')
  └─ enqueueCronJob + stash       phase await_scores : picks' queue rows all terminal, or deadline
     picked call_ids in           phase summarize    : build facts/agent → qa_agent_digests rows
     qa_disposition_pulls.report                       → ONE qa-insights run (mode 'daily_digest',
                                                         ≤40 items/run, cursor over chunks)
                                  phase await_summary: every row has summary_json or 20-min timeout
                                  phase send         : one agent per step → render HTML app-side
                                                       → GAS {html_email} → email_status
                                  done
qa-insights callback  → insightsCallback ref.kind='daily_digest' → dailyDigest.persistSummary
```

- **Ticker cost of waiting**: `JobStepResult.wait_s` (new) → runner stamps
  `qa_cron_jobs.next_at`; rows with a future `next_at` are neither picked
  nor counted as "more", so the ticker goes quiet between polls instead of
  spinning every 15 s for three hours. The hourly cron re-enters.
- **Night's cohort** = evals whose `dialpad_call_id ∈ report.picked_call_ids`
  of that pull, `state='finalized'`. Rescore-in-place keeps the id, so a
  rescored pick still counts once. Deleted evals drop out naturally.
- **Idempotent**: `(team_id, pull_date, agent_id)` unique on
  `qa_agent_digests`; re-running the job (reset the cron row to pending)
  re-sends only rows with `email_status != 'ok'`.

## 2. Data (migration 0021)

```sql
qa_agent_digests (id, team_id, pull_date, agent_id, agent_email, agent_name,
  eval_ids JSON, facts JSON, summary JSON?, summary_model, summary_usage JSON?,
  summary_error, email_status (pending|ok|skipped|error), email_message,
  sent_at, created_at, updated_at, UNIQUE(team_id, pull_date, agent_id))
qa_cron_jobs + next_at TEXT
teams.provider_config.nightly_sweep.digest = {enabled, send_deadline_utc,
  model, max_tokens, cc_supervisor}
```

## 3. Facts per agent (deterministic; persisted beside the summary)

`tonight.evals[]` (per pick: ts, overall, disposition, per-section score/yn,
per-section reasoning, strengths, opportunities, sop refs) ·
`tonight.section_avgs` · `month {label, n, avg, section_avgs}` ·
`recent[]` = last 5 finalized overall scores **before** tonight + tonight's
(the ProgressionCard contract, oldest→newest) · `pending_review` count.
Section ids/names come from the current rubric (`assessedSections`), so
the digest follows rubric changes without code.

## 4. Prompt contract (second person, judge doctrine)

Output JSON only:
```
{"headline": "<1-2 sentences>",
 "sections": [{"section_id", "trend": "up|down|flat|new", "note": "<1-2 sentences with figures>"}],
 "focus": "<3-4 lines: the one thing to work on next shift, grounded in tonight's reasoning and SOP citations>"}
```
Validation mirrors the progression persist: every assessed section exactly
once, trend from the enum, else `summary_error` and the facts-only email.

## 5. Email (rendered app-side, inline CSS, GAS palette)

Header "Nightly QA Digest — {date}" · headline · **Tonight** table (section,
tonight avg, month avg, trend, note) · **Focus** paragraph · **QA
Progression** card (last 5 + tonight, ▲▼● + bars, current row highlighted
— same as ProgressionCard) with **Month to date: {avg} over {n} evals** ·
per-call strip (time, disposition, overall, datapoint link) · footer.
Transport: GAS `{html_email:{to, cc?, subject, html, text}}` — a new
generic branch in `qa-automation/src/Main.js` (`GmailApp.sendEmail` with
`htmlBody`), so the app owns the layout and GAS is a pure sender.

## 6. Ladder

| Step | What | Gate |
|---|---|---|
| DD0 | this doc + migration 0021 | sqlite-validated 0001→0021 |
| DD1 | `dailyDigest.ts` facts + prompt + render (pure) | `tests/daily_digest.test.mjs` on the D1 shim: cohort math, month window, section avgs, progression order, render contains every section, no-summary fallback |
| DD2 | cron job handler + `wait_s`/`next_at` runner change + sweep hook + insights callback branch | same test: phase walk on stubs (await → summarize → await_summary → send → done), deadline path, idempotent re-run |
| DD3 | GAS `html_email` branch + `./push.sh qa-member-support` + `clasp deploy -i <versioned deployment>` | dry-run then live; curl a `{html_email}` to the MS webapp with `to` = owner. **Manifest rule (learned 2026-09-17):** `qa-automation/src/appsscript.json` is gitignored and MUST carry `"webapp": {"executeAs":"USER_DEPLOYING","access":"ANYONE_ANONYMOUS"}` — `clasp deploy -i` on a manifest without it drops the web-app entry point and `/exec` 404s (MS incident, restored at @19). |
| DD4 | v0.78 publish; `db migrate` 0021; live probe: run the handler for a synthetic key against last night's pull with `to` override | tonight's sweep → digests by 13:30 UTC |

## 7. Live verification (morning after)

```sql
SELECT agent_name, email_status, email_message, summary_error, json_array_length(eval_ids) AS n
FROM qa_agent_digests WHERE team_id='member_support' AND pull_date='<yesterday MX>' ORDER BY agent_name;
SELECT job, key, status, phase, report FROM qa_cron_jobs WHERE job='daily_digest' ORDER BY id DESC LIMIT 3;
```
Expect: one row per active agent with finalized picks, `email_status='ok'`,
`summary_error` null. `summary_error` set = model/JSON failure (email still
went facts-only). `email_status='error'` = GAS receipt in `email_message`.
