# Nightly auto-scoring for Member Support — disposition sweep (spec)

*Design doc, 2026-08-30. Companion to AgentAddition.md / SofiaRetellSpec.md.
Doc-first; ladder NS0–NS3 below is the implementation plan. Owner intent:
retire the manual daily selection routine (Lookup → agent email → filter →
pick calls, per agent) and make Member Support scoring **auto-only**: every
night, pull the day's dispositions from the Dialpad Stats API, select 3
calls per agent, ground them, and score them through the existing pipeline.*

## 0. Owner decisions (signed off 2026-08-30, this session)

| # | Question | Decision |
|---|---|---|
| 1 | Selection rule | **Random with preference for distinct disposition categories** |
| 2 | Per-eval scorecard emails | **Suppressed** for the nightly batch. A consolidated per-agent nightly email supersedes them — **out of scope this session** (future slice) |
| 3 | Direction | **Both** inbound and outbound |
| 4 | Reviewer identity on auto evals | **Fixed QA identity: "QA System"** (spec default: `qa-system@hellolanding.com` as the email value — evals/queries need an email-shaped identity) |
| 5 | Timing | **00:07 AM Mexico City** (06:07 UTC tick), pulling the **full previous day** — supersedes the literal-11:59-PM framing (2-cron platform cap; late calls + late-selected dispositions) |
| 6 | Agents with <3 eligible calls | Score what exists (1–2), no cross-day backfill |
| — | Human review | MS stays no-human-review (auto-finalize) — unchanged default |
| — | Manual Lookup/console flow | **Stays as the human backstop** (re-scores, ad-hoc). Deprecated = no longer the daily operating procedure, not deleted |

## 1. How the data flows TODAY (audited 2026-08-30)

```
Dialpad Stats API (dispositions records export, MS callcenter 5699048497577984)
   │
   ├─(Railway disposition_pull.py, is_today loop)──► Railway PG command_center.calls
   │      rows CREATED seen_via='stats_pull'; disposition filled under a
   │      "disposition_source IS NULL" wins-once guard. KNOWN GAP: after
   │      midnight the is_today loop never re-pulls yesterday → end-of-day
   │      calls and late-selected dispositions stay NULL in PG forever.
   │
   └─ Railway PG ──(shadow_sync.py::sync_cc_incremental, 30 min, laptop)──► D1 cc_calls
          upsert by last_updated_at watermark, ON CONFLICT(id) DO UPDATE.

D1 cc_calls ──(fetchCallContext at ENQUEUE)──► grounding block + SOP/RAG
   retrieval key (disposition_category) + cc_stamps on the queue payload
   ──(callback persist)──► qa_evaluations.dialpad_disposition_category/…
```

Live D1 facts (2026-08-30): 23 active MS agents; 70,456 MS cc_calls rows,
**100% seen_via='stats_pull'** (zero webhook rows → `has_hold_truth` is
false on every MS call, absence wording already handles it); 16,195 (23%)
have no disposition — the end-of-day-gap class this sweep will shrink.

The chain is **enqueue-time-grounded**: `scoreTriggerInternal` reads
cc_calls, keys SOP retrieval on the disposition, and freezes both into the
queue payload. So the owner requirement "dispositions in D1 BEFORE the
pipeline runs" means precisely: **fill before the autoScoreTrigger loop,
in the same sweep run** — ordering is structural.

## 2. Critical design constraints

1. **No cc_calls INSERTs while the shadow sync lives** (the AA0/CL0 class).
   A Sandy-born cc_calls row (D1 autoincrement id) whose PG twin later
   arrives via `ON CONFLICT(id) DO UPDATE` hits `uq_calls_team_call_id` →
   the sync batch fails/rolls back. During shadow the sweep is
   **UPDATE-only** on cc_calls; missing rows are handled by payload-level
   fallback grounding (§5.3). Full INSERT authority flips on at cutover
   (same module, one switch).
2. **Never write `last_updated_at`** in the fill. The sync watermark is
   `max(last_updated_at)` **over D1** — stamping it would silently skip PG
   updates (mirror data loss). SQLite has no auto-bump on UPDATE; just
   don't touch the column.
3. **Mirror Railway's wins-once guard**: only fill where
   `disposition_source IS NULL` and the incoming category is non-NULL.
   Idempotent re-runs, webhook-era stamps always win the seam.
4. **The export's `call_id` is the ENTRY-POINT id** (verified live
   2026-07-20, disposition_pull.py §fill). All matching is **triple-key**
   (`dialpad_call_id OR dialpad_entry_point_call_id OR
   dialpad_master_call_id`) — including the sweep's already-scored
   pre-check: manually-scored evals sometimes stored per-leg ids, and the
   in-trigger dedupe is single-column (kept as backstop 409).
5. **Suppression seam**: today's payload flag is rescore-specific
   (`rescore_suppress_email`, honored only when `rescore_of` is set —
   scoring.ts callback). The sweep needs a generic `persist.suppress_email`
   honored unconditionally by the callback's finalize-email branch.
6. **Cron slots**: both platform schedules are taken (`7 * * * *` pump,
   `37 9 * * *` maintenance). The sweep rides the hourly pump with an
   hour guard — no new schedule. Mexico City is fixed UTC−6 (no DST), so
   06:07 UTC = 00:07 local, year-round.
7. **CSV timestamps are naive in the row's own `timezone` column**
   (disposition_pull.py `_row_ts`) — localize before comparing/storing.
   Disposition labels split on `~` (`Category~Subdisposition`).

## 3. Schema — migration `0012_nightly_scoring.sql`

```sql
-- One row per (team, local day): the re-run latch, the resume state for
-- slow exports, and the audit trail of every nightly run.
CREATE TABLE qa_disposition_pulls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id      TEXT NOT NULL REFERENCES teams(id),
    pull_date    TEXT NOT NULL,             -- local day exported (YYYY-MM-DD, team tz)
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','fetching','completed','error')),
    request_id   TEXT,                      -- Stats API export id (resume handle)
    report       TEXT CHECK (report IS NULL OR json_valid(report)),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_pulls_team_date UNIQUE (team_id, pull_date)
);

-- Sweep config lives in teams.provider_config (house pattern: sofia's
-- agent_ids). member_support is provider 'dialpad', provider_config NULL
-- today; seeded here. Sales onboards later by config alone.
UPDATE teams SET provider_config = json('{
  "callcenter_id": "5699048497577984",
  "nightly_sweep": {
    "enabled": true,
    "per_agent": 3,
    "min_duration_s": 240,
    "max_duration_s": 1800,
    "suppress_email": true,
    "reviewer_email": "qa-system@hellolanding.com",
    "timezone": "America/Mexico_City",
    "local_hour_utc": 6,
    "max_enqueues": 120
  }
}') WHERE id = 'member_support';
```

sqlite-validated 0001→0012 before applying live (house pattern).

## 4. Selection algorithm (§0 decision 1)

Eligibility, from the parsed CSV (per call):
- `operator_email` matches an **active** roster row (`qa_agents`,
  team-scoped, lowercase email match). Unmatched operators → counted in
  the report, never scored.
- Duration = `date_ended − date_connected` in **[240s, 1800s]** inclusive.
  Rows without `date_connected` (missed/abandoned) drop naturally.
- `recording_url` present (no recording → no audio → skip; audio is SOT
  for Spanish calls).
- Direction: both. Disposition: **not required** (the absence path is a
  designed grounding state) but dispositioned calls are preferred (below).
- Not already scored (triple-key vs `qa_evaluations`) and no prior
  `qa_score_queue` row for the call (retellSweep one-auto-attempt rule:
  a failed job waits for a human re-score, never retries nightly).

Per agent, pick up to `per_agent` (3):
1. Group eligible calls by `disposition_category` (NULL = its own group).
2. Shuffle the non-NULL categories; take one **random** call from each
   distinct category until 3 or categories exhausted.
3. Fill any remainder randomly from the leftover pool (NULL-category
   calls included here).

RNG = `Math.random` (app worker, not a workflow script) behind an
injectable function so the E2E harness can pin it.

## 5. Module — `src/lib/dispositionSweep.ts` (mirrors retellSweep)

Entry: `sweepDispositions(db, request, env)` called from
`runHourlyPump` when the tick qualifies (§6). Phases, in order:

**5.1 Fetch/resume.** Look up `qa_disposition_pulls` for (team,
yesterday-local). Missing → INSERT `pending`, POST `/api/v2/stats`
(`export_type: records, stat_type: dispositions, timezone` from config,
`days_ago_start: 1, days_ago_end: 1`, callcenter target) → store
`request_id`, status `fetching`. Then poll `GET /stats/{id}` with a
**bounded budget (~15s)**; ready → download CSV and continue; not ready →
return, the **next hourly tick resumes** from `request_id`. (Railway
polls up to 600s for 20-day backfills; a 1-day export is seconds — the
resume seam is the safety net, not the expected path.)

**5.2 Disposition fill (UPDATE-only, before any enqueue).** For each
dispositioned CSV row: `UPDATE cc_calls SET disposition_category=?,
disposition=?, disposition_source='stats_pull' WHERE team_id=? AND
(triple-key match) AND disposition_source IS NULL` — constraints §2.2/2.3
(no last_updated_at, wins-once). Also back-fill **Sandy-born** evals
(id ≥ 10M) whose `dialpad_disposition_category IS NULL` (triple-key +
review-link match, mirror of Railway's `_EVALS_FILL_ROW`; Railway-born
rows get theirs from Railway's own fill via the shadow sync).

**5.3 Select + enqueue.** Run §4, then for each selected call:
`autoScoreTrigger(request, db, teamId, env, { callId, agentEmail,
managerEmail: reviewer_email, suppressEmail: true, statsContext })`.

Trigger extensions (routes/scoring.ts):
- `opts.suppressEmail` → `persist.suppress_email` → callback finalize
  branch honors it unconditionally (generalizes the rescore-only seam).
- `opts.statsContext = {disposition_category, disposition, connected_at,
  ended_at}` — **fallback grounding**: when `fetchCallContext` returns no
  row (call not yet mirrored — sync lag, laptop asleep, Railway's
  end-of-day gap) or a row with NULL disposition, the stats-derived
  disposition feeds the grounding block, the SOP retrieval query, and
  `cc_stamps`. A verified D1 row wins wherever present; stats fills
  holes. Never fabricates hold truth (`has_hold_truth` stays false).

**5.4 Report.** `{rows_in_export, with_disposition, fill_updated,
fill_missing, evals_backfilled, agents_matched, agents_unmatched,
eligible, selected, enqueued, skipped_existing, errors}` → the pull row's
`report` + the `cron_runs` note (`sweep_dispositions` key). Hard cap
`max_enqueues` (120) as a runaway guard; anything dropped is logged.

## 6. Scheduling + failure modes

- Hook in `runHourlyPump`: for each team whose
  `provider_config.nightly_sweep.enabled`, qualify the tick when UTC hour
  ∈ **[local_hour_utc, local_hour_utc+6)** (06–12 UTC catch-up window)
  AND no completed pull row exists for yesterday-local. The UNIQUE latch
  makes re-fires idempotent; a missed 06:07 tick self-heals at 07:07…
- `fetching` rows found at ANY hourly tick are resumed (poll → download →
  continue) — this also covers a worker death mid-run: phases 5.2/5.3 are
  idempotent (wins-once fill; one-auto-attempt enqueue checks), so a
  resumed run re-walks them safely.
- Export failed / request expired → status `error` + report; the catch-up
  window retries with a fresh export (new row is NOT created — same row,
  new request_id) until 12:00 UTC, then it stays `error` for the morning
  human to see. Errors never throw out of the cron handler (house rule).
- Queue drain: ≤69 jobs (23 agents × 3) serialized at ~2–4 min/job =
  ~2.5–4.5h; the v0.58 callback re-drain chain + hourly pump carry it;
  done well before the workday. Sofia's hourly sweep queues behind it
  overnight — acceptable; revisit the deferred priority-lane option B if
  Sales joins nightly.

## 7. What this deprecates / does NOT touch

| What | Status |
|---|---|
| Daily manual selection routine (Lookup → filters → pick per agent) | **Deprecated as procedure** — replaced by the sweep |
| Lookup page + scoring console | **Kept** — human backstop: re-scores, ad-hoc scoring, review queue |
| Per-eval GAS scorecard emails (MS nightly batch) | Suppressed (§0.2); consolidated nightly email = future slice |
| Railway disposition_pull.py | Untouched (freeze doctrine); its PG gap dies at cutover |
| Sales / Sofia | Untouched; Sales onboards later via provider_config |

## 8. Ladder NS0–NS3

- **NS0 — this doc** + owner sign-off on §0 defaults (done in-session).
- **NS1 — migration 0012** (sqlite-validated 0001→0012, applied live) +
  trigger extensions (`suppress_email` generic seam, `statsContext`
  fallback). Gate: E2E — suppressed finalize sends nothing, fallback
  grounding block renders from stats data, rescore seam byte-identical.
- **NS2 — dispositionSweep.ts** + `runHourlyPump` hook. Gate: full-cycle
  E2E on the house harness (esbuild + node:sqlite, migrations 0001→0012,
  CSV fixture from `database/dialpad_stats_records_dispositions_example
  .csv`, pinned RNG): fill wins-once + watermark untouched; selection
  distinct-disposition preference; triple-key dedupe; resume from
  `fetching`; per-agent <3 case; unmatched-operator skip.
- **NS3 — live gate**: deploy vN + one supervised night. Verify: pull row
  `completed` with sane report; ≤69 jobs drained by morning; evals carry
  dispositions + `evaluator=qa-system`; **zero scorecard emails**; spot-
  check 2–3 scorecards for SOP grounding on the disposition. Then Max
  announces the manual-selection retirement to the QA routine.

## 9. Cutover deltas (park on the sit-down list)

- Sweep gains the **INSERT arm** on cc_calls (`seen_via='stats_pull'`,
  Sandy id space) once `sync_cc_incremental` dies — it becomes the sole
  MS call-ingest path. Revisit hold-interval ingest then (webhook ask).
- Railway disposition_pull.py + its is_today loop → kill list.
- Consolidated per-agent nightly email replaces suppression (§0.2).
