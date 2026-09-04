# Member Support shift-end report — calls, tickets, unattended mentions (spec)

*Design doc, 2026-09-02. Companion to NightlyScoring.md (whose Stats-API
client this extends) and NightWatch.md (whose supervised-night pattern
SR5 reuses). Doc-first; ladder SR0–SR5 below is the implementation plan.
Owner intent: at the end of each of the three Member Support shifts, post
one report to `#member-support-hub` with the shift's Dialpad abandon rate
and the staffing issues behind it, the open Mission Control ticket counts
for four categories, and every `@member-support` / `@member-support-mgrs`
mention nobody replied to or reacted to. No new service, no new cron slot:
the report rides the qa-scoring app's hourly pump.*

## 0. Owner decisions (2026-09-02, this session)

| # | Question | Decision |
|---|---|---|
| 1 | Mission Control source | **Snowflake via the Sandy MCP gateway** (`LANDING.MISSION_CONTROL`, Stitch replica, ~30 min lag). MC v3's `graph.hellolanding.com` API is NOT used (not in the Sandy allow-list; separate design pass if ever needed). |
| 2 | Ticket scope | The Member Support **queue** (`TICKETS.QUEUE_ID`; live data shows a single queue, id **1**, holding every worked type — §1.2). Market segment is NOT a filter (MS gets all segments). |
| 3 | Abandon definition | **Dialpad's official abandoned** metric, from the Stats API — not the "never connected" proxy from `dialpad_list_calls`. |
| 4 | Reuse | **Extend `dispositionSweep.ts`'s Stats-API machinery** (initiate / poll / download / CSV parse / tz helpers) into a shared module; never a second client. Same rule everywhere: reuse or extend a semantically-close module before writing a new one. |
| 5 | Shifts (America/Mexico_City, fixed UTC-6) | **Morning 06:00–16:00 · Afternoon 12:30–22:00 · Night 22:00–06:00 (+1 day)**. Morning/Afternoon overlap 12:30–16:00 by design; each shift reports its own window. |
| 6 | Timing | Fires automatically **at the end of each shift**: the hourly `7 * * * *` pump tick right after each end (16:07 / 22:07 / 06:07 local = 22:07 / 04:07 / 12:07 UTC). No new cron (2-schedule platform cap — NightlyScoring §0.5). |
| 7 | Delivery | Slack **`#member-support-hub`** (`C0664EX0SG3`, private) for now. Stored in D1 too (`qa_shift_reports`) so the console can render history later. |
| 8 | Mentions | `@member-support` = subteam **`S046UTKHHUZ`**, `@member-support-mgrs` = subteam **`S066VLZGGJ0`** (both observed live). Unattended = zero replies AND zero reactions at report time. |

## 1. What exists today (audited 2026-09-02)

### 1.1 Dialpad

```
Dialpad Stats API (POST /api/v2/stats → GET /stats/{id} → CSV)
   └─ sandy-qa src/lib/dispositionSweep.ts  initiateExport / pollAndDownload /
        parseCsv / naiveLocalToIso / localDay / tzOffsetMs   ← the client to extend
        (records export, stat_type dispositions, MS callcenter 5699048497577984,
         bounded ~12 s poll budget per tick, request_id resume on the next tick)
   └─ qa-automation/AI-Scoring/scripts/sales_csat_export.py::_stats_export
        (python twin; already pulls stat_type="calls" records — knows `email`,
         `date_started`, `date_connected`, `date_ended`, `entry_point_call_id`)
```

Stats-API facts (probed + docs): `export_type` ∈ {records, stats};
`stat_type` ∈ {calls, csat, dispositions, onduty, recordings, screenshare,
texts, voicemails}; `target_type: callcenter`; `timezone`; `days_ago_start`
/ `days_ago_end` or `is_today`; `group_by` (calls only) ∈ {date, group,
user}. Export timestamps are NAIVE in the row's `timezone` column
(NightlyScoring §5.1). **Not yet verified: the exact column set of the
`calls` records export and the `onduty` records export, and which column
carries Dialpad's abandoned marker** — that is SR0, and §4.1 branches on it.

Sandy MCP gateway `dialpad` (JWT auth, 16 read-only tools): `dialpad_list_calls`
works (epoch-ms args, pagination) — used only for the discovery sample below.
`dialpad_get_call_stats` / `dialpad_get_agent_metrics` are **broken upstream**
(gateway GETs `/stats/calls` → Dialpad 404 "Request with id calls not found");
reported to Sandy eng, not a dependency here. `dialpad_get_operator_status`
returns per-operator `on_duty_status` / `on_duty_started` — a live snapshot,
useful for a "now" line, not for a shift window.

Discovery sample (night shift 2026-09-01 23:00→07:00, `list_calls`): 56
inbound entry-point calls on the MS line, 16 never connected (waits 1 s –
12 min). Every hub call center overflows to the MS line when closed, so
after-hours volume includes ~60 hub lines' overflow — the report states
the line, not the caller's origin.

### 1.2 Mission Control tickets (Snowflake)

`LANDING.MISSION_CONTROL` (Stitch): `TICKETS` (TICKET_TYPE_ID,
TICKET_REASON_ID, TICKET_STATUS_ID, QUEUE_ID, DELETED_AT, CREATED_AT,
UPDATED_AT, `_SDC_BATCHED_AT`), `TICKET_TYPES`, `TICKET_STATUSES`,
`TICKET_REASONS`, `TICKET_COMMENTS`, `TICKETS__HISTORY`, `AGENTS` (21 Agent +
9 Supervisor rows, CURRENT_STATUS, DIALPAD_USER_ID), `AGENT_STATUSES`
(STATUS, STARTED_AT, ENDED_AT per agent — MC-side availability history).
Freshness measured: `MAX(_SDC_BATCHED_AT)` ≈ now − 27 min.

Vocabulary (live):

| Report category | Where it lives | Filter |
|---|---|---|
| Maintenance | `TICKET_TYPES` id **4** | `TICKET_TYPE_ID = 4` |
| I Need Something Else | `TICKET_TYPES` id **34** | `TICKET_TYPE_ID = 34` |
| Packages | `TICKET_REASONS` id **16** (under type 2 Home) | `TICKET_REASON_ID = 16` |
| Lockouts | `TICKET_REASONS` id **7** "Locked Out" (under type 2 Home) | `TICKET_REASON_ID = 7` |

Statuses: **New = 1, Working = 2, Needs action = 34** (also 35 Awaiting
response, 3 Closed, 68 Escalated — not counted). Queue: open tickets split
`QUEUE_ID = 1` (I need something else 468, Home 375, Reservation 176,
Maintenance 155, Billing 99, …) vs `QUEUE_ID` NULL (Conversation 10,367 =
Sofia chat tickets, plus a tail of unqueued Home/INSE). No QUEUES table is
replicated, so the queue's NAME is confirmed in the MC v3 UI (open item §9.3);
`queue_id` is config, not code.

### 1.3 Slack

Nothing in sandy-qa talks to Slack yet — `routes/teamApi.ts` carries a
deliberate "Slack notification seam — NOT built yet" comment on access
requests. This design IS that seam's first consumer: one `src/lib/slack.ts`
(post + search + fetch-one-message), which the access-request notification
can reuse later. Observed via the Slack search index: subteam mentions are
indexed by handle (searching `"member-support"` returns messages whose text
carries `<!subteam^S046UTKHHUZ>`); search results carry `reply_count` but
not reactions — reactions come from `conversations.history` for the single
message (§4.4).

## 2. Critical design constraints

1. **One Stats-API client.** SR1 lifts `initiateExport` (generalized
   options), `pollAndDownload`, `parseCsv`, `naiveLocalToIso`, `localDay`,
   `tzOffsetMs` out of `dispositionSweep.ts` into `src/lib/dialpadStats.ts`;
   `dispositionSweep.ts` imports them. Pure move: the disposition sweep's
   behaviour is pinned by a fixture test before the move and re-run after.
2. **No new cron.** Everything hangs off `runHourlyPump` (maintenance.ts),
   which already hosts two tick-gated jobs (Retell sweep, disposition sweep).
   The shift report runs FIRST in the pump: it is human-facing and
   time-sensitive; the sweeps are not.
3. **Bounded time per tick.** Same ~12 s poll budget as the disposition
   sweep (`POLL_ATTEMPTS`/`POLL_SPACING_MS`, shared constants). Exports not
   ready → the latch row keeps its request ids and the NEXT tick resumes
   (report ≤ 1 h late, and it says so). The 06:07 UTC-12 tick also hosts the
   disposition sweep; both budgets are bounded so the dispatch stays well
   under any platform timeout.
4. **Never post twice, never post silently-partial.** One `qa_shift_reports`
   row per (team, shift_date, shift_key) is the latch; `slack_ts` set ⇒
   never re-post. A failed section (Snowflake down, Slack search 4xx) posts
   as an explicit "unavailable: <reason>" line — the report still lands, the
   gap is visible, the error is in the row. (Silent-failure ordering rule.)
5. **Windows are exact, in team tz.** `[start, end)` computed from the
   config strings with `tzOffsetMs`; the night shift's `shift_date` is the
   date it STARTED (22:00), so the 06:07 report for the night of Sep 2→3 is
   `(member_support, 2026-09-02, night)`. Export rows are filtered to the
   window by `date_started` (localized via the row's `timezone` column).
6. **Secrets stay Dashboard-only** (Sandy rule): `DIALPAD_API_KEY` (exists),
   `SNOWFLAKE_MCP_TOKEN` (new, `sgmcp_*` from Manage → MCP Gateway — §9.1),
   `SLACK_BOT_TOKEN` + `SLACK_USER_TOKEN` (new — §9.2). Missing secret ⇒
   the affected section is "unavailable", the cron note says which.
7. **Snowflake is read-only and lagged.** Every ticket section carries
   `as_of = MAX(_SDC_BATCHED_AT)`; the message prints it ("as of 15:36").
8. **Overlap is intentional.** 12:30–16:00 calls count in both the Morning
   and Afternoon reports; the header states the window so nobody sums them.

## 3. Schema — migration `0016_shift_reports.sql`

```sql
-- One row per (team, local shift date, shift) — re-run latch, export resume
-- handle, Slack idempotency key, and the stored report. Sandy-only.
CREATE TABLE qa_shift_reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id       TEXT NOT NULL REFERENCES teams(id),
    shift_date    TEXT NOT NULL,            -- local date the shift STARTED
    shift_key     TEXT NOT NULL CHECK (shift_key IN ('morning','afternoon','night')),
    window_start  TEXT NOT NULL,            -- ISO UTC
    window_end    TEXT NOT NULL,            -- ISO UTC
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','fetching','completed','error')),
    export_ids    TEXT CHECK (export_ids IS NULL OR json_valid(export_ids)),
                                            -- {"calls":["<request_id>",…],"onduty":[…]}
    report        TEXT CHECK (report IS NULL OR json_valid(report)),
                                            -- shift_report.schema.json payload
    slack_ts      TEXT,                     -- chat.postMessage ts (idempotency)
    posted_at     TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_shift_reports UNIQUE (team_id, shift_date, shift_key)
);

-- SR4: fixed-shift assignment for absence detection (NULL = not on a fixed
-- shift; supervisors/admins stay NULL). Surfaced on the roster page.
ALTER TABLE qa_agents ADD COLUMN shift TEXT
    CHECK (shift IS NULL OR shift IN ('morning','afternoon','night'));

-- Config rides teams.provider_config (house pattern; callcenter_id already there).
UPDATE teams SET provider_config = json_set(provider_config, '$.shift_report', json('{
  "enabled": true,
  "timezone": "America/Mexico_City",
  "shifts": [
    {"key": "morning",   "label": "Morning",   "start": "06:00", "end": "16:00"},
    {"key": "afternoon", "label": "Afternoon", "start": "12:30", "end": "22:00"},
    {"key": "night",     "label": "Night",     "start": "22:00", "end": "06:00"}
  ],
  "catchup_window_hours": 3,
  "late_grace_min": 10,
  "tickets": {
    "queue_id": 1,
    "statuses": {"new": 1, "working": 2, "need_action": 34},
    "categories": [
      {"label": "Maintenance",           "type_id": 4},
      {"label": "I Need Something Else", "type_id": 34},
      {"label": "Packages",              "reason_id": 16},
      {"label": "Lockouts",              "reason_id": 7}
    ]
  },
  "slack": {
    "channel_id": "C0664EX0SG3",
    "subteams": {"member-support": "S046UTKHHUZ", "member-support-mgrs": "S066VLZGGJ0"},
    "search_cap": 200
  }
}')) WHERE id = 'member_support';
```

sqlite-validated before applying live (house pattern). The report payload
contract is `references/shift_report.schema.json` (JSON Schema 2020-12);
the TS type is derived from it, and the Slack renderer consumes only that
payload — so the D1 row, the console, and Slack never disagree.

## 4. Algorithms

### 4.1 Calls — abandon rate (Dialpad official)

Exports per report (all `target_type: callcenter`, `target_id` =
`provider_config.callcenter_id`, `timezone` = team tz):

| Shift | `calls` records exports | `onduty` records exports |
|---|---|---|
| Morning / Afternoon | `is_today: true` | `is_today: true` |
| Night | `is_today: true` **and** `days_ago_start: 1, days_ago_end: 1` (window straddles midnight) | same pair |

Rows are filtered to `[window_start, window_end)` by localized
`date_started`. Universe = **inbound entry-point calls on the MS line** in
the window. Abandoned = **the column SR0 identifies as Dialpad's abandoned
marker** in the calls records export. SR0's decision procedure:

1. Run `stats_schema_probe` (SR0) for yesterday: `calls` records, `calls`
   *stats* (`export_type: stats`, daily aggregate), `onduty` records. Commit
   the header rows (no data) to `references/fixtures/dialpad_stats_headers.md`.
2. If the records export carries an explicit result/abandoned column
   (`state`, `call_result`, `abandoned`, …): use it. Verify: the count over
   the whole local day must equal the daily *stats* export's abandoned
   figure for that day.
3. Otherwise derive: entry-point inbound row (`entry_point_call_id` empty /
   `target_type` = callcenter) with no `date_connected` on itself or on any
   leg sharing its `entry_point_call_id`, and no voicemail marker. Verify
   against the daily *stats* figure the same way. **If neither matches
   within ±1 for three consecutive days, stop and re-design §4.1** — the
   report never ships a number that disagrees with Dialpad Analytics.
4. Whichever rule passes becomes `calls.definition` in the payload (a short
   string the message footer prints), and the daily reconciliation stays on
   as a check: the 06:07 night report also computes yesterday's full-day
   figure from records and compares it with the daily `stats` export;
   mismatch > 1 ⇒ a `notes[]` line on the report ("Dialpad daily abandoned
   = 23, records-derived = 25 — definition drift?").

Payload: `inbound`, `abandoned`, `rate` (abandoned / inbound, 1 decimal),
`answered`, `voicemail` (if distinguishable), `avg_wait_abandoned_s`,
`longest_wait_s`, `by_hour[]` ({hour, inbound, abandoned}) — the last one
feeds §4.2's "issues" line.

### 4.2 Staffing — "shift issues impacting the abandon rate"

Source: the `onduty` records export (per-operator duty-status intervals;
columns per SR0). Per operator whose email matches an active `qa_agents`
row (team-scoped, lowercase — same matcher as the disposition sweep):
`first_on_duty`, `last_off_duty`, `on_duty_min`, `unavailable_min` inside
the window. With the SR4 roster (`qa_agents.shift`):

| Flag | Rule |
|---|---|
| `absent` | expected on this shift, zero on-duty minutes in the window |
| `late` | `first_on_duty > start + late_grace_min` |
| `left_early` | `last_off_duty < end − late_grace_min` |
| `coverage_gap` | any minute in the window with **0** operators on duty (merged into ranges) |
| `thin_hour` | an hour in `by_hour[]` whose abandons ≥ 3 and on-duty operators ≤ 1 |

The message renders the line only when at least one flag fires; otherwise
"none detected". Before SR4 lands (no roster), `absent` is omitted and the
line lists late / left-early relative to the shift bounds for whoever
appeared, plus coverage gaps — honest partial, labelled "(roster not
configured — absences not evaluated)". MC's `AGENT_STATUSES` is the
ticket-side twin of the same signal; SR4 may add it as a second column if
supervisors ask.

### 4.3 Tickets — Mission Control counts

One SQL through the gateway (`snowflake_sql_exec_tool`), parameters
substituted from config (ints only — no string interpolation of user input):

```sql
SELECT t.TICKET_TYPE_ID, t.TICKET_REASON_ID, t.TICKET_STATUS_ID, COUNT(*) AS n
FROM LANDING.MISSION_CONTROL.TICKETS t
WHERE t.DELETED_AT IS NULL AND t.QUEUE_ID = 1 AND t.TICKET_STATUS_ID IN (1,2,34)
GROUP BY 1,2,3
```
plus `SELECT MAX(_SDC_BATCHED_AT) FROM LANDING.MISSION_CONTROL.TICKETS` for
`as_of`. Aggregation happens in TS by `categories[]` (a category matches on
`type_id` OR `reason_id`; Packages/Lockouts match by reason regardless of
type). Output per category: `new`, `working`, `need_action`. Gateway
client: `src/lib/snowflakeMcp.ts` — JSON-RPC `tools/call` with
`X-MCP-Token`, SSE-or-JSON response handling (the gateway answers either;
the workflow template's `mcpRequest` is the reference shape).

### 4.4 Slack — unattended mentions

1. `search.messages` (user token) with `query = "member-support" after:<shift_date − 1>`
   sorted by timestamp, paginated up to `search_cap`. Keep hits whose text
   contains `<!subteam^S046UTKHHUZ>` or `<!subteam^S066VLZGGJ0>` and whose
   `ts` ∈ window. (`"member-support"` also matches the mgrs handle by
   prefix; the subteam-id filter is the real gate.)
2. For each hit: `conversations.history(channel, latest=ts, oldest=ts,
   inclusive=true, limit=1)` → `reply_count` and `reactions`. Threaded
   replies that are themselves mentions are kept (their own `reply_count`
   is what counts).
3. Unattended = `reply_count == 0` **and** no reactions. Excludes bot
   authors and messages posted in the report channel by the report itself.
4. Payload: `count`, `items[]` ({permalink, channel_name, author, ts_iso,
   subteams[]}); message lists up to 15 with a "+N more" tail.

### 4.5 Message (Block Kit, one post)

```
MS Shift Report — Morning · 06:00–16:00 · Tue 2 Sep 2026
Call metrics (Member Support Line)
• Shift-end abandon rate: 12.5 % (7 of 56 inbound)
• Total abandoned calls: 7 — avg wait before abandon 1 m 48 s, longest 12 m 01 s
• Shift issues impacting the abandon rate: late — A. Lopez 06:24, J. Barron 06:41 ·
  coverage gap 06:00–06:24 (0 on duty, 3 abandons)                   ← or "none detected"
Ticket counts — MC queue Member Support, as of 15:36 · New / Working / Need Action
• Maintenance 1 / 45 / 100     • I Need Something Else 77 / 3 / 708
• Packages 12 / 0 / 31         • Lockouts 4 / 1 / 9
Unattended mentions (@member-support, @member-support-mgrs — no replies, no reactions): 3
• #ert 15:01 Oswaldo B. → link   • #autopilot-ops 14:49 Trey D. → link   • …
↳ abandoned = Dialpad official (records: <definition>) · report delayed 1 tick   ← footer, only when relevant
```

## 5. Module — `src/lib/shiftReport.ts` (mirrors dispositionSweep)

Entry: `runShiftReports(db, request, env, testOpts)` called first in
`runHourlyPump`. Per team with `provider_config.shift_report.enabled`:

**5.1 Tick gate.** Resume any row in `pending`/`fetching` first (resume
seam). Else compute local `HH:MM`; if a shift's `end` hour equals the
current local hour (tick is at :07) → that shift just ended: derive
`shift_date` (= local date of the window start), `window_start/end` (UTC),
`INSERT … ON CONFLICT DO NOTHING`, read back. Catch-up: a row still
`pending`/`error` is retried on every tick until `end + catchup_window_hours`
(fresh exports on `error`, same row). Beyond that: `status='error'`,
`report.notes[]` says "missed catch-up window" — visible on the console,
and NightWatch's tier-0 poll (SR5) escalates it.

**5.2 Exports.** For each needed (`stat_type`, day-selector) pair without a
stored request id → `initiateExport`, store ids in `export_ids`, status
`fetching`. Then one bounded `pollAndDownload` pass per id; any not ready
→ return `{status:'fetching'}` (next tick resumes). CSVs are parsed with
`parseCsv` + per-stat_type row mappers (`parseCallsCsv`, `parseOndutyCsv`)
living next to `parseExportCsv`.

**5.3 Sections** run independently and never throw: `callsSection`,
`staffingSection`, `ticketsSection` (Snowflake), `mentionsSection`
(Slack). Each returns either data or `{error}`; the assembler writes the
payload (`shift_report.schema.json`) to `report`.

**5.4 Post.** If `slack_ts` is NULL → `chat.postMessage` (bot token) →
store `slack_ts`, `posted_at`, status `completed`. A post failure leaves
the row `error` with the payload intact; the next tick retries the POST
only (payload is not recomputed once complete — the numbers a human may
have already seen elsewhere must not drift).

**5.5 Test seams.** `testOpts = { nowMs, fetchImpl }` — frozen clock and an
injectable `fetch` so the harness (`tests/shift_report.test.mjs`, esbuild
bundle pattern like `tests/sop_scope.test.mjs`) stubs Dialpad, the gateway,
and Slack from fixtures; no network, no D1 beyond the in-memory shim the
parity harness already uses.

## 6. Scheduling + failure modes

| Tick (UTC) | Local | Fires | Shares the tick with |
|---|---|---|---|
| 22:07 | 16:07 | Morning report (06:00–16:00) | Retell sweep, pump |
| 04:07 | 22:07 | Afternoon report (12:30–22:00) | Retell sweep, pump |
| 12:07 | 06:07 | Night report (22:00–06:00, `shift_date` = yesterday) | Retell sweep, **disposition sweep** (its 06–12 UTC window), pump |

| Failure | Behaviour |
|---|---|
| Export not ready in budget | row stays `fetching`; next tick resumes; message footer "report delayed N tick(s)" |
| `DIALPAD_API_KEY` missing | calls + staffing sections "unavailable: no dialpad key"; report still posts |
| Gateway 4xx/5xx or `SNOWFLAKE_MCP_TOKEN` missing | tickets section "unavailable: <status>"; report still posts |
| Slack search fails | mentions section "unavailable"; report still posts |
| `chat.postMessage` fails | row `error`, payload kept; retried each tick in the catch-up window; never duplicated (`slack_ts` gate) |
| Worker dies mid-run | resume seam: request ids in `export_ids`, sections recomputed (idempotent), post gated by `slack_ts` |
| Reconciliation mismatch (§4.1.4) | `notes[]` line on the night report; three consecutive days ⇒ NightWatch escalation |
| Cron silently stops | existing `cron_runs` staleness check (0004) — nothing new |

## 7. What this replaces / does NOT touch

- Replaces the hand-written shift-end message (whoever posts it today) —
  the human can still add a thread reply with context under the bot post.
- Does NOT change scoring, the disposition sweep's behaviour (SR1 is a
  pure lift, fixture-pinned), the roster's meaning (a nullable `shift`
  column only), or any cc_calls/qa_evaluations row.
- Does NOT use MC v3's API or the `dialpad` MCP gateway at runtime (the raw
  Stats API with the Company-Admin key is the only Dialpad dependency).

## 8. Ladder SR0–SR5 (pytest-style checkpoints = `node tests/*.test.mjs`)

| Stage | Work | Checkpoint | ~Effort |
|---|---|---|---|
| **SR0** schema capture | `qa-automation/AI-Scoring/scripts/stats_schema_probe.py` reusing `sales_csat_export.py::_stats_export`: pull yesterday's MS `calls` records, `calls` stats, `onduty` records; print headers + 3 rows; write `sandy-qa/references/fixtures/dialpad_stats_headers.md`. Decide §4.1 rule 2 vs 3. **Needs the `.env` key — owner runs it.** | headers file committed; §4.1 "definition" filled in; daily figure reconciles | 0.25 d |
| **SR1** shared client | lift Stats-API + tz helpers into `src/lib/dialpadStats.ts`; generalize `initiateExport(opts)`; `dispositionSweep.ts` imports. | `tests/dialpad_stats.test.mjs` (parseCsv/naiveLocalToIso/localDay/dedupe pinned by fixture; initiate/poll against a fetch stub); existing sweep tests green; version bump | 0.5 d |
| **SR2** core report | migration 0016 (minus the roster column); `shiftReport.ts` tick gate, latch/resume, calls + tickets sections, payload assembly; `snowflakeMcp.ts`; pump hook; console JSON at `GET /api/{team}/shift-reports` (read-only). | `tests/shift_report.test.mjs`: window math for all three shifts incl. night straddle + month/year rollover; resume after not-ready; both sections from fixtures; payload validates against the schema | 1.5 d |
| **SR3** Slack | `src/lib/slack.ts` (post, search, fetch-one); mentions section; Block Kit renderer; idempotent post. | renderer snapshot test; mentions filter (subteam ids, window, reply/reaction gate) from fixtures; post-once test | 1 d |
| **SR4** staffing | `onduty` mapper; roster `shift` column + roster-page field (`routes/roster.ts`); flags §4.2. | flag rules from fixtures (late/early/absent/gap/thin-hour); message line on/off | 1 d |
| **SR5** supervised nights | run 3 shift-ends supervised (NightWatch tier-0 poll on `qa_shift_reports` + `cron_runs`); reconciliation notes reviewed; then announce in `#member-support-hub`. | 3 consecutive clean reports, no delayed ticks, reconciliation ±1 | 0.5 d |

Ship order follows silent-failure impact: SR0–SR2 give supervisors the two
numbers they act on; SR3 adds the mention audit; SR4 adds the "why".

## 9. Open items / dependencies (owner or eng)

1. **Snowflake gateway token for the app** — an `sgmcp_*` permission token
   scoped to `snowflake_sql_exec_tool` (Manage → MCP Gateway → Permission
   Tokens; engineer/administrator — Drew). Stored as `SNOWFLAKE_MCP_TOKEN`
   on qa-scoring. SR2's first live tick also verifies the outbound-worker
   passes `X-MCP-Token` through to `/mcp-gateway/*` (docs say the two auth
   paths are exclusive; untested from an app).
2. **Slack app** ("MS Shift Reports"): bot scope `chat:write` (+ invite to
   `#member-support-hub`); user scopes `search:read`, `channels:history`,
   `groups:history` installed by a member of every channel the groups get
   pinged in (owner for v1; a service user later). Secrets `SLACK_BOT_TOKEN`,
   `SLACK_USER_TOKEN`.
3. **Confirm `QUEUE_ID = 1` is the Member Support queue** in the MC v3 UI
   (no QUEUES table is replicated). Config-only change if not.
4. **SR0 run** needs the Company-Admin Dialpad key (`qa-automation/AI-Scoring/.env`).
5. **Roster shift assignments** (who is Morning / Afternoon / Night) for SR4.
6. FYI to Sandy eng (not blocking): `dialpad_get_call_stats` /
   `dialpad_get_agent_metrics` 404 upstream; the "Mission Control"
   allow-list regex `missioncontrol[3]?` misses `missioncontrolv3` and
   `graph.hellolanding.com`.

## 10. EOD Google Sheet sink — SR1 + the daily report on Sandy (2026-09-03)

*Added after SR0 closed (owner declared 2026-09-03; probe results in
`references/fixtures/dialpad_stats_headers.md`). The owner's first ask was a
daily EOD report in a Google Sheet; it landed first as
`qa-automation/AI-Scoring/scripts/ms_eod_report.py` (Python, gspread — the
reference implementation and the manual backfill tool). This section moves
the same computation onto the qa-scoring app's hourly pump, and lands SR1
(the shared Stats client) on the way. Owner answers 2026-09-03: day boundary
= America/Mexico_City; "windowed" = the three shifts in §0; report BOTH
agent counts (handled a call / went on duty — off-the-phone specialists);
one row per entry-point call with the answering agent joined; sheet
`1IF2vpb7oo3gybCkX82Wmk1YtYuwwszFqwIDHxy_032Y` shared only with the service
account + MS Direction.*

### 10.1 Definitions locked by SR0 (reconciled against Dialpad's daily export)

| Term | Rule on the `calls` records export | Check |
|---|---|---|
| a call | `target_kind = CallCenter` row (entry-point leg; `UserProfile` rows are agent legs joined via `entry_point_call_id`) | inbound 396/345 exact |
| answered | `category = incoming` | 353/289 exact |
| abandoned | `category = abandoned` | 21/37 exact (Dialpad official) |
| short abandoned | abandoned ∧ `date_ended − date_started` < **6 s** | 5/11 exact |
| SL count | answered inbound ∧ `time_to_answer` ≤ `cc_service_level_seconds` (30 s, fetched live from `GET /callcenters/{id}`; units are MINUTES) | 286 exact / 223 vs 225 |
| **SL %** | `sl_count / (inbound − short_abandoned − missed)` | **74 % / 67 % = Dialpad Analytics** (owner-read); spam is NOT excluded |
| agents handled | distinct agent-leg emails started in the window | 19/18 = per-user export rows |
| agents on duty | distinct emails with an available/occupied/wrap-up interval (`onduty` export state machine) overlapping the window | 21/19 |

The daily `stats` export (`group_by: date`) is the Full-day source of
truth; the records-derived numbers are the per-shift source and are
reconciled against the daily row (a `CHECK:` string in the sheet, never a
silent drift).

### 10.2 Schedule + gate (no new cron — 2-slot cap)

Rides `runHourlyPump` like the disposition sweep: one latch row per
(team, local report date) in `qa_eod_reports` (migration 0016), status
`pending → fetching → completed | error`, `export_ids` = the Stats request
ids keyed by selector (resume handle), `report` = aggregates only (no
caller numbers in D1). Gate: `local_hour_utc` (default **13** = 07:07
America/Mexico_City) with a 6 h catch-up window, same shape as
`nightly_sweep`. Why 07:07 and not 06:07: the night shift ends at 06:00 the
next day, so the report needs an `is_today` export, and Dialpad's real-time
tables refresh every 30 minutes — at 06:07 the 05:30–06:00 calls can be
missing. One tick later they are complete. A slow export still resumes on
the following tick (≤ 1 h late, visible in `cron_runs`).

Exports per report date D (today-local = D+1): `calls` records
`days_ago 1..1` + `is_today`; `onduty` records same pair; `calls` stats
`group_by date` and per-user `days_ago 1..1`. Resumed after another
midnight (today = D+n): the pair collapses to one `days_ago (n−1)..n`
records export and no `is_today`; selector keys make the stored ids
self-describing so stale ids are simply ignored.

### 10.3 Modules

| File | Role |
|---|---|
| `src/lib/dialpadStats.ts` | **SR1.** `initiateExport(apiKey, opts)` (any export_type / stat_type / group_by / days_ago / is_today), `pollOnce`, `pollAndDownload` (bounded), `parseCsv`, `csvRecords`, `tzOffsetMs`, `naiveLocalToIso`, `localDay`. Pure move of the sweep's code; `dispositionSweep.ts` imports and re-exports the names it used to own. |
| `src/lib/googleSheets.ts` | Service-account JWT (RS256 via WebCrypto) → access token; Sheets v4 REST: meta, get values (UNFORMATTED), `upsertTab` = drop rows whose col-A date is being rewritten, append, sort, clear, resize, write RAW, freeze header. No gspread. |
| `src/lib/eodReport.ts` | Pure compute (port of `ms_eod_report.py`: entry-point join, outcome rules, shift windows incl. the night straddle, duty intervals, official-day mapping, reconcile) + the latch state machine + the three-tab sink. Same headers as the Python script so both can maintain one sheet. |
| `src/lib/maintenance.ts` | pump hook (before the queue drain), `CronEnv.GSHEETS_SA_JSON` |
| `src/index.tsx` | forwards `GSHEETS_SA_JSON`; `Env` doc comment |
| `migrations/0016_eod_reports.sql` | latch table + `provider_config.eod_sheet` for member_support |

### 10.4 Config + secrets

`teams.provider_config.eod_sheet` (member_support):
`{"enabled":true,"spreadsheet_id":"1IF2…032Y","timezone":"America/Mexico_City","local_hour_utc":13,"catchup_hours":6,"short_abandon_s":6,"sl_seconds_fallback":30,"shifts":[…§0 rows…]}`.
Secret **`GSHEETS_SA_JSON`** (Dashboard-only; names are capped at 20 chars)
= the service-account JSON for `maxp-rezauto@qa-ai-scoring.iam.gserviceaccount.com`
(~2.3 KB). Missing secret ⇒ the report computes, the row goes `error`
("no sheets credentials"), retried inside the catch-up window — a sheet that
did not get written is the failure, never a silent skip.

### 10.5 Ladder E0–E3 (`node tests/*.test.mjs`)

| Stage | Work | Checkpoint |
|---|---|---|
| **E0** design | this section | owner read |
| **E1** SR1 lift | `dialpadStats.ts`; sweep imports; `tests/dialpad_stats.test.mjs` (csv/tz helpers pinned by fixture; initiate/poll against a fetch stub) | existing sweep tests green; version bump |
| **E2** EOD job | `googleSheets.ts`, `eodReport.ts`, 0016, pump hook, `GET /api/{team}/eod-reports`; `tests/eod_report.test.mjs` (same synthetic day as the Python test → identical numbers; gate + latch + resume on node:sqlite; Sheets calls asserted on a fetch stub) | numbers match the Python reference; push v0.67; migration applied; secret set; one manual tick verified against the sheet |
| **E3** supervised | 3 mornings watched (NightWatch tier-0 on `qa_eod_reports` + `cron_runs`), reconciliation `OK` or explained; Python script retired to backfill-only | 3 clean days |

Not in scope here: Slack posting (SR3), staffing flags (SR4), monthly tab
rotation of `Calls` (~500 rows/day; revisit when the tab passes ~50k rows).
