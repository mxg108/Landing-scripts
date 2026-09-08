# NightWatch — tiered overnight sweep monitoring (design + runbook)

*2026-09-02, owner-directed. The 2026-09-01 supervised night proved the
routine's value (caught the enqueue-burst rate-limit class and the
platform-5xx stall cadence live) but ran every routine tick through a
frontier-model session. NightWatch splits the work into three tiers so
routine ticks cost near-zero and the big model only wakes for judgment.*

## Tiers

| Tier | Runs on | Job |
|---|---|---|
| 0 | `scripts/night_poll.py` (no model) | ALL mechanics: D1 polling, delta detection, stall clocks, leak SQL. One invocation = block until the next notable event or ~9 min, print ONE JSON event line, exit. State rides a JSON file between invocations. Read-only (SELECTs only). |
| 1 | small-model watcher agent (Haiku) | Loop tier 0; apply the decision table below verbatim; send the routine push notifications; EXIT with a structured report when the night ends or anything needs judgment. |
| 2 | main session (Fable) | Woken by tier 1's exit. Investigates escalations with full codebase context; owns any fix. Never polls. |

The escalation contract is the watcher's EXIT — its completion
notification is what wakes tier 2. A watcher must never investigate,
edit files, run other commands, or write to the database.

## Tier-1 decision table

| Event | Action |
|---|---|
| `idle_wait`, `heartbeat`, `sweep_row`, `evals_progress` (leaked=0) | continue; no push |
| `sweep_completed` | push the report brief; continue |
| `stall` | continue (known-benign: platform 5xx breaks the callback chain; the hourly `:07` pump self-heals — observed 4× on 2026-09-01, all recovered) |
| `stall_persistent` (≥75 min = a pump tick passed without rescue) | push + EXIT `ESCALATE stall_persistent` |
| `leak_detail` (any `system:sofia` doc in MS/Sales provenance) | push + EXIT `ESCALATE leak` |
| `deferred_stuck` (finalized eval still carrying `sop_skipped_reason: deferred_to_trigger`) | EXIT `ESCALATE deferred_stuck` (trigger-time SOP resolution was skipped — v0.64 failure signature) |
| `sweep_error` | push + EXIT `ESCALATE sweep_error` |
| `query_error` with `consecutive >= 3` | EXIT `ESCALATE auth` (Sandy token likely expired) |
| `drained` | push the sweep verdict. WITHOUT `--eod-date`: EXIT `CLEAN <summary>`. WITH it: continue — the watch enters the EOD-report phase (the 13:07 UTC Daily Service Level report) |
| `eod_row` | continue; no push |
| `eod_completed` | push "Daily SL report written" + the report brief; EXIT `CLEAN <drained summary + eod brief>` |
| `eod_error` | push + EXIT `ESCALATE eod_error` |
| `eod_missing` (15:45 UTC, no terminal row — the report legitimately takes 2-3 hourly resume ticks on slow-Dialpad days) | push + EXIT `ESCALATE eod_missing` |
| anything else / malformed | EXIT `ESCALATE unknown_event` |

The `drained` summary's per-team `cost_null` count (evals missing their
v0.72 `estimated_cost_usd` stamp) rides the exit report for tier 2 — the
watcher does not act on it.

Exit report format (first line is the contract):
`ESCALATE <type> | last_event=<the JSON line> | counts=<total/clean/leaked/queued>`
or `CLEAN | <drained summary JSON>`.

## Push policy (tier 1)

Push = sweep_completed brief, final verdict, and every escalation.
Routine progress stays silent — the 2026-09-01 night generated ~25
routine ticks and exactly 4 moments worth a phone buzz.

## Arming (each supervised night)

Spawn the watcher agent (model: haiku) with the runbook prompt, passing:
`--state <scratch>/nightwatch-<date>.json --pull-date <yesterday local
(America/Mexico_City)> --baseline <last publish ts> --window-start 0555
--eod-date <yesterday local>` (the EOD flag extends the watch through the
13:07 UTC Daily Service Level report; omit it for sweep-only nights).

Watchdog loop budget: **≥250 for a two-phase night** (110 proved too
small on 2026-09-08 — the watcher starved at 13:31 UTC mid-drain: a
7.5h+ watch emits ~50 heartbeats alone, and post-drain daytime console
scoring keeps generating evals_progress ticks). If the watcher exits
`watchdog_budget`, tier 2 re-arms a bounded shell-loop Monitor over the
SAME state file (it survives the watcher) rather than a fresh agent —
`while true; do night_poll…; done` breaking on `eod_*` terminals.
Every watcher Bash call sets `timeout: 580000` (the poller's budget is
9 min; the default 120 s would kill it mid-block). Delete the state file
before the first invocation of a new night.

## Future

- Cron-spawned watcher (schedule skill / routines) instead of manual arm.
- Tier-0 gains provider-latency + per-eval section digests once the PID
  calibration loop (AriaIntegrationSpec) needs nightly sensor data.
