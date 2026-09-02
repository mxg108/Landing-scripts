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
| `drained` | push the final verdict; EXIT `CLEAN <summary>` |
| anything else / malformed | EXIT `ESCALATE unknown_event` |

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
(America/Mexico_City)> --baseline <last publish ts> --window-start 0555`.
Every watcher Bash call sets `timeout: 580000` (the poller's budget is
9 min; the default 120 s would kill it mid-block). Delete the state file
before the first invocation of a new night.

## Future

- Cron-spawned watcher (schedule skill / routines) instead of manual arm.
- Tier-0 gains provider-latency + per-eval section digests once the PID
  calibration loop (AriaIntegrationSpec) needs nightly sensor data.
