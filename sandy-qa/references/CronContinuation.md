# Cron continuation — bounded ticks + durable ticker (design + incident record)

Written 2026-09-16 after the automated Member Support scoring stopped
(no nightly sweep since the 2026-09-13 tick). Companion to
`NightlyScoring.md` (§5/§6 supersede notes below), `ShiftReport.md` §10
(EOD sheet) and `SofiaRetellSpec.md` R4 (Retell sweep).

## 0. Incident (facts, UTC)

| When | What the data shows |
|---|---|
| Sep 11 10:03–13:58, Sep 12 01:01–15:34 | Daily `37 9 * * *` handler dispatched **~30 times** at ~15-min spacing (each completed; Jackson received ~17 Sofia digests). Hourly ticks in the Sep 12 06–12 window absent → the sweep for local day **09-11 never ran** (no `qa_disposition_pulls` row). |
| Sep 13 06:08–07:10 | Last healthy night: pull 09-12 completed (633 rows, 45 enqueued, 45 evals). Tick took **3.5 min** and was logged. |
| Sep 14 01:07–20:07 | **No hourly ticks at all** (cron_runs ids 952→955 contiguous). Daily fired at 18:32 + 18:37. Sweep for **09-13 never ran**. |
| Sep 14 (platform) | Sandy release note "Scheduled crons now fire reliably": scheduler recovers on its own, dispatches apps in parallel, **bounds how long any single app can hold the queue**, watchdog every 10 min. Bound value not published (`cron-schedules` is engineer-only; `event-log` 403 for builder). |
| Sep 15 + Sep 16 | Identical pattern: 06:07 initiate → `fetching` (logged); **07/08/09 ticks leave no cron_runs row**; 10:07 poll → `stats poll HTTP 404` (id expired ~1 h) → error; 11:07 fresh export → `fetching`; **12/13 no rows**; 14:07 → 404 → error; window closed. Pulls 09-14 and 09-15 end `error`. |
| Sep 15 17:07–21:07 | Sofia jobs enqueued at 17:xx, 18:xx, 19:xx, 20:xx, 21:xx (10/tick = the Retell sweep cap) yet **no cron_runs rows for those ticks**; 16:07 and 22:07 (0–1 enqueues) logged normally. |
| Sep 14–16 | Sofia evals scored normally (13 / 34 / 8, all cost-stamped, Gemini annotate + Claude judge). **Gemini quota was not the cause.** |

Reading: any tick that does real work (export download + fill + 45
enqueues ≈ 2–3 min; 10 Retell enqueues ≈ 20–40 s) is cut off before its
final `INSERT INTO cron_runs`; ticks that finish in a few seconds are
logged. The cut lands somewhere between ~10 s and ~60 s. Combined with
Dialpad's ~1 h request-id expiry and an hourly poll cadence, the nightly
sweep can no longer complete: the tick that finally gets the CSV is the
one that gets killed, and the next poll finds the id expired.

Sep 14's 20-hour hole and the Sep 11–12 daily storm were the platform
scheduler itself (the release note's "stop dispatching entirely" /
"run several minutes late" bugs) — nothing app-side could have helped.

## 1. Invariant we now build to

**A `/_sandy/cron` dispatch must be acknowledged in well under 10 s and
must never carry work that cannot be lost.** Everything heavier runs as
bounded, resumable steps outside the dispatch request. The platform's
bound is unknown and may tighten again; the design must not depend on it.

Secondary invariants (unchanged): errors never throw out of the cron
handler; every state machine resumes from D1; enqueue is idempotent
(one auto attempt per call); Sandy caps crons at 2 schedules.

## 2. Design

### 2.1 Instant-ack cron

`POST /_sandy/cron`:
1. `INSERT INTO cron_runs (cron, note) VALUES (?, '{"phase":"started"}')`
   **first** — a tick that leaves no row is now impossible; a row stuck on
   `started` is the new "cut off" signature.
2. Respond `200 {ok:true}` immediately.
3. In `ctx.waitUntil` (~30 s budget, empirically): run the **light tick**
   (`runHourlyPump` / `runDailyMaintenance`, both now ≤ a few seconds — see
   2.3), then `UPDATE cron_runs SET note=?` on the same row.

### 2.2 Ticker workflow — `qa-cron-ticker`

A generic durable loop (Sandy Workflow, `workflows/qa-cron-ticker.js`).
Payload: `{ callback_url, callback_token, max_iterations, reason }`.

```
for i in 1..max_iterations:
  res = step.do("tick-i")  → POST callback_url {run_id, callback_token, status:"running", tick:i}
                             read JSON {done, sleep_s, summary}
  if res.done: break
  step.sleep(res.sleep_s)     (clamped 10–120 s; default 30 s on parse failure)
final step: sendCallback(callback_url, {status:"complete", ticks, capped})
```

Each callback is a fresh app request (HMAC-verified by the dispatch
worker, `X-Sandy-Workflow-Callback: verified`), so every step gets its own
time budget independent of the cron dispatcher. The workflow is the only
component that can wait minutes between steps (`step.sleep`), which also
removes the hourly-poll vs 1-hour-expiry race for Dialpad exports.

One active run at a time (platform rule, 409 on a second trigger — the
trigger treats 409 as "already running"). Cap `max_iterations` = 400 (≈ 3 h
at the queue-wait cadence); a capped run ends and the next hourly cron
starts a fresh one. A run that dies (platform 5xx, cut request → step
retry 1× then run error) leaves all state in D1; the next cron tick
re-triggers.

Triggered by: every hourly and daily cron tick (cheap: a run with nothing
to do returns `done` on its first step). Console/API paths keep the
existing callback re-drain chain; the ticker additionally pumps the queue
every 45 s while anything is queued.

### 2.3 Step protocol — `runTickerStep(db, request, env)` (app side)

One bounded unit of work per pending job, in this order, each guarded
(never throws; each returns `{..., more: boolean}`):

| Job | Step unit | `more` when |
|---|---|---|
| Disposition sweep (§3 state machine) | one phase transition per team | phase ≠ completed/error/none |
| Retell sweep | list (1 call) + up to **3** enqueues | unattempted candidates remain |
| EOD sheet report | one bounded poll pass (existing `fetchExports`, 4×3 s) | row still `fetching` |
| Queue pump | `drainScoreQueue` (one trigger) | `queued > 0` |

Response: `done = !(any more)`, `sleep_s` = 15 when a sweep/EOD phase
progressed, 45 when only waiting on the scoring queue. The hourly cron's
light tick runs exactly ONE `runTickerStep` inline (inside `waitUntil`) as
the fallback when the ticker cannot be triggered — that preserves today's
"at least one unit of work per hour" floor.

### 2.4 Disposition sweep as a state machine (supersedes NightlyScoring §5.1–5.3 timing)

`qa_disposition_pulls` gains `phase`, `cursor`, `export_json`, `picks`
(migration 0017). `status` keeps its CHECK set; `phase` refines
`fetching`:

| phase | step does | then |
|---|---|---|
| `null`/`initiate` (status pending, no request_id) | POST /stats → request_id | `poll` |
| `poll` | ONE `pollOnce`; 400/404 (expired) → re-initiate on the spot (like `fetchExports`), ≤3 re-inits; ready → download, parse, dedupe → `export_json` | `fill` (cursor 0) |
| `fill` | next 200 dispositioned rows → `fillDispositions` chunk; cursor += 200 | `select` when cursor ≥ rows |
| `select` | roster + eligibility + `attemptedCallIds` + `pickForAgent` → `picks` (≤ max_enqueues), cursor 0, `export_json` cleared | `enqueue` |
| `enqueue` | next **3** picks → `autoScoreTrigger` (suppressEmail + statsContext); cursor += 3 | `completed` when cursor ≥ picks (report written) |

Every phase is ≤ ~5 s. Resume is exact (cursor), so a run cut mid-phase
re-does at most one chunk (fill is wins-once; enqueue is 409-idempotent).
Window/latch/catch-up semantics (§6) are unchanged: rows are created only
inside `[local_hour_utc, +6h)`, `error` rows retry with a fresh export
inside that window, and pending/fetching rows resume on ANY step.

### 2.5 Retell sweep + EOD

Retell: `MAX_ENQUEUES_PER_STEP = 3` (was 10 per tick) and the result
carries `more`. EOD: unchanged state machine; the result carries `more`
while `fetching`, and because the ticker re-polls every 15 s the "today"
export becomes ready within a few steps instead of expiring.

### 2.6 Daily maintenance

Prune + digest + drain stay inline in the `waitUntil` continuation (a
few seconds). **Open item (before Oct 1):** the EOM assessments trigger +
HR-bonus GAS dispatch also live there today; the GAS POST alone can take
10–30 s. Move them to ticker steps (a `qa_cron_jobs` progress row) as
phase 2 — tracked in §7.

## 3. Schema — migration `0017_cron_continuation.sql`

```sql
ALTER TABLE qa_disposition_pulls ADD COLUMN phase TEXT;          -- initiate|poll|fill|select|enqueue|done
ALTER TABLE qa_disposition_pulls ADD COLUMN cursor INTEGER NOT NULL DEFAULT 0;
ALTER TABLE qa_disposition_pulls ADD COLUMN export_json TEXT;    -- parsed rows between poll and select (cleared after)
ALTER TABLE qa_disposition_pulls ADD COLUMN picks TEXT;          -- selected calls between select and completed
ALTER TABLE qa_disposition_pulls ADD COLUMN reinits INTEGER NOT NULL DEFAULT 0;
```

`export_json` ≈ 100 KB for a 633-row day (well inside D1 row limits);
cleared at `select` so the table does not grow.

## 4. Module map

| File | Change |
|---|---|
| `src/index.tsx` | cron handler: insert-first + instant ack + `waitUntil` continuation; callback route: `qa-cron-ticker` → `runTickerStep`, response `{done, sleep_s}` |
| `src/lib/maintenance.ts` | `runHourlyPump` = trigger ticker + one inline step; `runDailyMaintenance` = prune/digest/drain + trigger ticker; `runTickerStep`; `triggerTicker` |
| `src/lib/dispositionSweep.ts` | phase state machine (§2.4); `sweepDispositions` returns per-team `{phase, more}` |
| `src/lib/retellSweep.ts` | 3 enqueues per step + `more` |
| `src/lib/eodReport.ts` | `more` on fetching results |
| `workflows/qa-cron-ticker.js` | new workflow |
| `migrations/0017_cron_continuation.sql` | §3 |
| `tests/cron_ticker.test.mjs` | sweep phases on node:sqlite (migrations 0001→0017) with a stubbed `autoScoreTrigger` (`tests/routes/scoring.js`), ticker step composition, retell cap |

## 5. Observability

- `cron_runs`: one row per dispatch, inserted first; `note.phase` =
  `started` → replaced by the full tick note. A lingering `started` row =
  the continuation was cut.
- `workflow_runs` row per ticker run (`qa-cron-ticker`): `result` holds
  `{ticks, last_summary, started_at}` updated every step.
- `qa_disposition_pulls.phase/cursor` show exactly where a sweep is.

## 6. Rollout + recovery

1. Migration 0017 (sqlite-validated in the harness, then `db migrate`).
2. Create + publish `qa-cron-ticker` v0.1 (allowed app: `qa-scoring`).
3. Push + publish app v0.74; verify the next hourly tick: `cron_runs` row
   goes `started` → full note, a `qa-cron-ticker` run appears and ends
   `done`.
4. Recovery: pull rows for local days **09-11, 09-13, 09-14, 09-15** were
   lost (2 never created, 2 `error`). Re-arming a day = one row set to
   `pending` with `request_id/phase/cursor` reset (write path: migration
   file, the db query endpoint is read-only). Each day ≈ 45 evals ≈ $4 —
   owner decision per day; 09-15 re-armed in-session as the live proof.
5. Workflow statuses `qa-scoring-pipeline`, `qa-insights` → `ready`
   (the Sep 11 dormancy sweep retires `in_draft` workflows 28 days after
   the last push; both were last pushed Sep 8).

## 7. Ladder

- **CC0** this doc. **CC1** migration + sweep state machine + harness
  (`node tests/cron_ticker.test.mjs`). **CC2** ticker workflow + app
  plumbing + build. **CC3** live: publish, watch one hourly tick + the
  09-15 recovery sweep end-to-end. **CC4 (before Oct 1)** EOM/HR dispatch
  as ticker steps. **CC5** ask Engineering for the dispatch bound value
  and whether callback requests carry a bound; record here.
