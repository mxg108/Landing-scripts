# Phase 3 — PostgreSQL as Primary Store (Step 9)

> **Purpose:** Design doc for migrating the system's source of truth from
> Google Sheets to PostgreSQL on Railway, while keeping `Analyst_History`
> as a read-only visibility window. Companion to `PhaseTwo.md` (which
> closed schema convergence + Sales onboarding) and `PhaseOne.md` (which
> framed Step 9 as the natural Postgres landing point, coupled with
> Step 8 — Dialpad webhook automation).
> **Author session:** 2026-05-14.
> **Status:** ⏭️ Planned. No code written yet. Schema below is the
> proposal; implementation begins after sign-off.

---

## What hasn't been built into the app so far

For orientation. Step references match the unified roadmap in
`PhaseOne.md §Roadmap`. Phase 2 (this branch) closed Steps 3–5.

| Step | Title | Status |
|---|---|---|
| 3 — Rubric abstraction (JSON config per team) | TeamConfig + `backend/config/teams/{id}.json` | ✅ Done |
| 4 — Team routing + multi-team support | `/api/{team_id}/...` everywhere, per-team API keys | ✅ Done |
| 5 — Sales team onboarding | End-to-end live, May 2026 | ✅ Done |
| 6 — SOP/Notion RAG integration | Notion sync + ChromaDB embeddings + per-team SOP namespaces | ⏭️ Not started. `notion_service.py` is a stub. Lifts scoring accuracy for Process Adherence / Call Resolution. |
| 7 — Cost tracking + admin dashboard | `audit_service.py` aggregation + `/api/admin/costs` + `admin_dashboard.html`. Per-team monthly budget caps. | ⏭️ Not started. JSONL audit log exists today and would feed this. |
| 8 — Dialpad webhook automation | Call-end webhook → stratified random sampler (~30/agent/week) → auto-score → manager review only | ⏭️ Not started. Tightly coupled to Step 9 — sampler needs persistent state Sheets can't provide. |
| **9 — PostgreSQL migration (this doc)** | `evaluations` table + `PostgresProvider` swap + Sheets as visibility window | ⏭️ Active design |

Phase 3 prioritizes **Step 9** because every other unbuilt step gets
easier on top of a real database:

- Step 6 (RAG): per-team SOP versions stored in PG, joined to
  evaluations for "did this call cite the right SOP?" analytics.
- Step 7 (cost dashboard): JSONL audit log already ships per request;
  PG just turns it into a queryable surface.
- Step 8 (webhook sampler): needs to remember "we sampled call X for
  agent Y this week" — Sheets can't carry that cleanly.

After Step 9, Steps 6/7/8 each become a feature on top of an
operational DB rather than a structural change.

---

## Why we're moving off Sheets as primary

Phase 2 made the schema rectangular and predictable; Phase 3 takes the
next obvious step. The case isn't urgent, but the operational ceiling
is visible:

| Pain | Today | Post-PG |
|---|---|---|
| Sheets API rate limits | 60 writes/min/user — Phase E's 500-row chunked import had to hand-roll throttling and backoff | Postgres takes thousands of writes/sec; the throttler in `migration_utils.py` becomes obsolete |
| Query expressiveness | gspread reads the whole sheet, filters in pandas | SQL `WHERE team_id = $1 AND timestamp > now() - interval '90 days'` with proper indexes |
| Transactions | None — Stage 4 + Stage 5 can desync if Apps Script fails | `BEGIN ... COMMIT` around Stage 4's PG insert + Stage 5's trigger |
| ARRAYFORMULA races | 3-5 s `arrayformula_buffer_seconds` poll on every approval | Overall score is computed in Python at write time |
| Eventual consistency | Stage 3 polls col F until non-empty | Stage 3 disappears entirely |
| Audit trail | JSONL file on Railway disk; lost on container restart | `audit_log` table or jsonb on the eval row |
| Cross-team analytics | One spreadsheet per team — must aggregate in app code | One SQL query: `GROUP BY team_id` |
| Historical retention | Sheets pages get slow past ~5000 rows | Partition or just index — both teams could grow 10× |

`Analyst_History` stays useful as a **human-readable spreadsheet view
of the last 6 months** plus the email-pipeline source row. Anything
older lives in PG only and is queryable through a new Sheets custom
menu.

---

## Schema — unified `evaluations` table

One table, all teams. `team_id` is the discriminator; section-shape
differences (MS N=10, Sales N=19, future teams N=?) live inside `jsonb`
columns. This keeps cross-team analytics trivial and lets new teams
land without an `ALTER TABLE`.

```sql
CREATE TABLE evaluations (
  -- Identity
  id            BIGSERIAL PRIMARY KEY,
  team_id       TEXT      NOT NULL,                -- 'member_support' | 'sales' | ...
  call_id       TEXT      NOT NULL,                -- eval_id parsed from dialpad_link
  rubric_version TEXT,                              -- snapshot of TeamConfig.rubric_version at scoring time

  -- Who / when
  agent_name        TEXT NOT NULL,
  agent_email       TEXT,                          -- resolved at Stage 4 via Mails (or agents table once F lands)
  evaluator_email   TEXT,                          -- the analyst who approved
  call_started_at   TIMESTAMPTZ,                   -- Dialpad's call-start time (from get_call_details)
  approved_at       TIMESTAMPTZ NOT NULL,          -- when the analyst hit Approve & Send, UTC

  -- Score
  overall_score   NUMERIC(5,2),                    -- 0-100 from the team's ARRAYFORMULA, snapshotted at approval

  -- Section data — keys are section.id (matches team_config)
  sections    JSONB NOT NULL,                      -- {greeting: {score: 5, yn_value: null, score_type: 'numeric'}, ...}
  reasoning   JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {greeting: 'You used...'}
  confidence  JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {greeting: 'high'}

  -- Feedback + caller meta
  key_strengths TEXT,
  opportunities TEXT,
  call_summary  TEXT,
  caller_name   TEXT,
  caller_phone  TEXT,

  -- Provenance
  source        TEXT NOT NULL DEFAULT 'ai',        -- 'ai' | 'migrated' | 'manual'
  dialpad_link  TEXT,                              -- full URL with [LONG CALL] suffix if present

  -- Bookkeeping
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  UNIQUE (team_id, call_id)                         -- idempotency on (team, dialpad call)
);

-- Analytics index on call-start time (the natural "when did this happen?"
-- axis for trends + dashboards). Approval time is queried less often but
-- still indexable separately if needed.
CREATE INDEX evaluations_team_agent_call_ts ON evaluations (team_id, agent_name, call_started_at DESC NULLS LAST);
CREATE INDEX evaluations_team_call_ts       ON evaluations (team_id, call_started_at DESC NULLS LAST);
CREATE INDEX evaluations_team_approved_ts   ON evaluations (team_id, approved_at DESC);
CREATE INDEX evaluations_call_id            ON evaluations (call_id);

-- Optional jsonb GIN if we need section-level WHERE clauses; defer
-- until query patterns prove it out.
-- CREATE INDEX evaluations_sections_gin ON evaluations USING GIN (sections jsonb_path_ops);
```

`call_started_at` defaults to `NULL` for backfilled MS rows that
pre-date the Dialpad backfill (see Phase C below). New evaluations
from Phase D onward populate it from `dialpad_client.get_call_details`
at Stage 1 (the call is already fetched there for caller_name +
caller_phone, so `date_started` rides along for free).

### How team-rubric differences map

The `sections` jsonb column carries the section keys, not the column
schema. For Member Support an `evaluations` row's `sections` jsonb
holds 10 keys (`greeting`, `documentation`, etc.); for Sales it holds
19 (`pb_creation`, `mc_call_notes`, ..., `pre_send_intro`). Each value
is a small object that captures everything the score-shape requires:

```jsonc
// Member Support row (N=10)
{
  "greeting":                     {"score": 5,   "yn_value": null, "score_type": "numeric"},
  "caller_identity_validation":   {"score": null, "yn_value": "Y", "score_type": "yn"},
  "purpose_of_call":              {"score": 4,   "yn_value": null, "score_type": "numeric"},
  // ...7 more
  "documentation":                {"score": 5,   "yn_value": null, "score_type": "manual"}
}

// Sales row (N=19)
{
  "greeting":         {"score": null, "yn_value": "Y",  "score_type": "yn"},
  "pb_creation":      {"score": null, "yn_value": "Y",  "score_type": "manual_yn"},
  "mc_call_notes":    {"score": null, "yn_value": "N",  "score_type": "manual_yn"},
  "situation_match":  {"score": 4,    "yn_value": null, "score_type": "numeric"},
  // ...15 more
}
```

Queries that need a specific section know the team and the section id:

```sql
-- Sales — average Situation Match (numeric) over last 90 days, by agent
-- (uses call_started_at — what we mean by "in the last 90 days" is when
-- the call happened, not when it was scored)
SELECT
  agent_name,
  AVG((sections->'situation_match'->>'score')::numeric) AS avg_score
FROM evaluations
WHERE team_id = 'sales'
  AND call_started_at > now() - interval '90 days'
  AND sections->'situation_match'->>'score' IS NOT NULL
GROUP BY agent_name;

-- Member Support — Caller Identity Validation pass-rate
SELECT
  agent_name,
  AVG(CASE WHEN sections->'caller_identity_validation'->>'yn_value' = 'Y' THEN 1.0 ELSE 0.0 END) AS pct_yes
FROM evaluations
WHERE team_id = 'member_support'
  AND call_started_at > now() - interval '90 days'
GROUP BY agent_name;
```

`PostgresProvider` (see Phase B below) wraps these patterns so route
handlers don't write raw SQL.

### Companion tables (in scope for Phase 3)

Three sibling tables ship alongside `evaluations`. Schemas land in the
same migration; reads / writes are added incrementally per phase.

```sql
-- Mirrors the Mails sheet for both teams, with supervisor + active flag
-- as first-class columns. Lets /team/stats stop reading Mails on every
-- request and gives Step 8's sampler a stable agent roster to stratify
-- against.
CREATE TABLE agents (
  id            BIGSERIAL PRIMARY KEY,
  team_id       TEXT NOT NULL,
  agent_name    TEXT NOT NULL,           -- canonical name
  agent_email   TEXT,
  supervisor    TEXT,                    -- canonical supervisor name (nullable)
  is_active     BOOLEAN NOT NULL DEFAULT true,
  aliases       TEXT[] DEFAULT '{}',     -- raw form-response names that resolve to this canonical row
  timezone      TEXT,                    -- IANA tz; used by Phase F.1's trimmer if we go per-agent local
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (team_id, agent_name)
);
CREATE INDEX agents_team_active ON agents (team_id) WHERE is_active;

-- Append-only event log for any non-trivial state change in the
-- pipeline. JSONL audit log on Railway disk feeds this on Phase E
-- backfill, then Phase D dual-writes new events here as they happen.
-- Powers Step 7's cost dashboard without needing a second migration.
CREATE TABLE audit_log (
  id            BIGSERIAL PRIMARY KEY,
  ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  team_id       TEXT,                    -- null for system events not scoped to a team
  actor         TEXT,                    -- evaluator_email, 'system', 'webhook', etc.
  action        TEXT NOT NULL,           -- 'score' | 'edit' | 'approve' | 'lookup' | 'trim' | 'sample' | ...
  resource_kind TEXT,                    -- 'evaluation' | 'agent' | 'sheet' | ...
  resource_id   TEXT,                    -- call_id, agent_name, etc.
  detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
  request_id    TEXT                     -- correlates to the FastAPI audit middleware request_id
);
CREATE INDEX audit_log_ts          ON audit_log (ts DESC);
CREATE INDEX audit_log_team_action ON audit_log (team_id, action, ts DESC);
CREATE INDEX audit_log_resource    ON audit_log (resource_kind, resource_id);
```

Deferred to their own steps (listed for context):

| Table | Phase | Notes |
|---|---|---|
| `sampling_state` | Step 8 | Per-agent stratified sampler bookkeeping. The reason Step 9 must land before Step 8. |
| `sop_chunks` | Step 6 | Embedded SOP fragments per team for RAG retrieval. ChromaDB-or-pgvector decision deferred. |

---

## Migration phases

Same shape as PhaseTwo (A–F). Each phase ends with a verification gate;
nothing destructive happens until reads are stable on PG.

### Phase A — Schema + Postgres provisioning

- Reuse the existing Mass Notifications Railway Postgres; create a new
  `qa_evaluations` schema and put all three tables (`evaluations`,
  `agents`, `audit_log`) inside it.
- Author `database/migrations/004_qa_evaluations_schema.sql`. Follow
  the numbering pattern from Mass Notifications (`001/002/003_…sql`).
  Wraps `CREATE SCHEMA IF NOT EXISTS qa_evaluations` + the three
  `CREATE TABLE` blocks + their indexes.
- Add `DATABASE_URL` env var on Railway + locally (same connection
  string Mass Notifications already uses — no second URL).
- One-shot script `qa-automation/scripts/migrate_qa_schema.py` that
  runs the migration file against the configured DB. Idempotent —
  uses `CREATE TABLE IF NOT EXISTS` so it can be re-run safely.

**Gate:** psql connects, `\dt qa_evaluations.*` shows all three
tables with their indexes, an `INSERT` followed by `SELECT` round-trips
on each.

### Phase B — PostgresProvider implementation

- New `backend/services/postgres_provider.py` implementing the
  `DataProvider` interface — methods `get_agent_history`,
  `get_all_history`, `get_datapoint(call_id)`, etc.
- New `backend/services/db_pool.py` — asyncpg pool with the standard
  lifespan hookup in `backend/main.py`.
- Mirror the existing `SheetsProvider` behaviors:
  - Date/time filters via SQL instead of pandas
  - Active-only filter via the Mails sheet (Phase E may migrate this
    to a PG `agents` table; Phase B can read it from Sheets still)
- Unit tests against a docker-compose Postgres or `testcontainers`.

**Gate:** the existing `get_provider(team_id)` returns either provider
behind a feature flag (`QA_PROVIDER=postgres|sheets`, default sheets).
Test suite green on both.

### Phase C — Backfill from Sheets (+ Dialpad + Mails + JSONL)

Four backfills land in this phase. All idempotent, all support
`--dry-run` and `--apply`. Same transaction-per-team pattern as
Phase 2's migration scripts.

**C.1 — `agents` from Mails (both teams)**

- `qa-automation/scripts/backfill_agents.py` reads each team's Mails
  sheet, normalizes canonical names, populates `agents`.
- Drives Phase D's "active filter" lookup. Stops `/team/stats`
  reading the Mails sheet on every request.

**C.2 — `evaluations` from Analyst_History (both teams)**

- `qa-automation/scripts/backfill_evaluations.py` walks each team's
  `Analyst_History` and inserts rows into `evaluations`.
- Reuses the row parser in `history_service._parse_row` (it already
  knows how to read the canonical layout for any N).
- For MS, also backfills `Analyst_History_legacy` rows with
  `source='migrated_legacy'` so pre-Phase-E history is queryable.
- `approved_at` = AH `COL_TIMESTAMP`; `call_started_at` left NULL
  (filled by C.3).
- Tag forward-going migrated rows with `source='migrated'`.

**C.3 — `call_started_at` from Dialpad (both teams)**

- `qa-automation/scripts/backfill_call_started_at.py` iterates rows
  with NULL `call_started_at`, looks up each `call_id` via
  `dialpad_client.get_call_details`, writes `date_started` back to
  the row.
- Honors Dialpad's existing disk cache + 5 req/sec throttle
  (`dialpad_client._SEMAPHORE`). Resumable — re-run picks up only
  still-NULL rows.
- Best-effort. Calls older than Dialpad's retention drop NULL and
  stay NULL (analytics queries already handle the
  `NULLS LAST` ordering).

**C.4 — `audit_log` from JSONL**

- `qa-automation/scripts/backfill_audit_log.py` parses the existing
  JSONL audit file(s) on Railway disk, inserts rows.
- `team_id`, `actor`, `action`, `resource_*` extracted from each
  JSONL entry; everything else lands in `detail` jsonb.
- One-time — Phase D's middleware change starts writing directly
  to the table, so JSONL fades.

**Gate:**
- `evaluations` row counts match Sheets for both teams.
- Random-sample 10 rows per team and diff field-by-field.
- `agents` count matches Mails non-blank rows per team.
- ≥80% of `call_started_at` populated for the last 180 days (older
  Dialpad calls may have aged out and that's OK).
- `audit_log` row count ≥ JSONL line count (some lines collapse
  into one event).

### Phase D — Dual-write at Stage 4 (PG primary) + audit-log writes

This is the bridge phase — analogous to Phase 2's Phase-C transitional
bridge, with the same "deploy in either order" intent.

- `sheets_service.finalize_to_analyst_history` becomes
  `finalize_evaluation`:
  1. INSERT INTO `qa_evaluations.evaluations` (primary, transactional)
     - `call_started_at` populated from `dialpad_client.get_call_details`
       (already fetched at Stage 1 for caller_name + caller_phone)
     - `approved_at` = `datetime.now(timezone.utc)`
  2. INSERT INTO `qa_evaluations.audit_log`
     (`action='approve'`, `resource_kind='evaluation'`,
      `resource_id=call_id`)
  3. APPEND to `Analyst_History` (best-effort; logs on failure but
     doesn't roll back)
- Audit middleware (`backend/middleware/audit.py`) gets a second
  sink: write to `qa_evaluations.audit_log` in addition to the JSONL
  file. JSONL stays for one cycle as a safety net; deleted in Phase F.
- Stage 5 (Apps Script doPost) unchanged — it reads from AH as today.
  The AH write must succeed for the email to dispatch, so failures
  here are still surfaced.
- New JSONL log line per approval: `{ "pg": "ok", "sheets": "ok" }`
  or with a failure side flagged. Cross-check parity for a week.

**Gate:** every approval over a one-week window produces matching PG +
AH rows + `audit_log` rows. Any divergence is investigated before
Phase E.

### Phase E — Switch reads to PG

- Default `QA_PROVIDER=postgres`. Flag stays as the rollback path.
- All read endpoints (`/team/stats`, `/agents/{name}/history`,
  `/datapoints/{call_id}`, `/datapoints?bin=...`) go to PG.
- `Analyst_History` becomes write-mostly; it's still appended to (for
  Apps Script email source + human visibility) but no analytic reads
  hit it.
- Dashboard + DataPoint pages keep working unchanged — they consume
  JSON shapes the provider produces, not provider internals.

**Gate:** spot-check the same agent/team/date range on PG-backed
dashboards vs Sheets-backed (toggle the flag) for one week. No
discrepancies before Phase F.

### Phase F — Visibility window + cleanup

This is the user-visible "we moved off Sheets as primary" phase. Three
sub-deliverables, can land independently:

**F.1 — 180-day trim (time-based Apps Script trigger)**

- New `qa-automation/src/Trimmer.js` (shared across teams).
  `trimAnalystHistory()` reads `Analyst_History`, removes rows with
  `COL_TIMESTAMP < now() - 180 days`, leaves the header + last 6
  months. Logs deleted-row count to Apps Script's Logger.
- Time-based trigger registered per-team: weekly, Sunday 03:00 in the
  team's timezone.
- Idempotent — safe to invoke manually from a new "QA Automation" →
  "Trim history (older than 180 days)" menu item.

**F.2 — Sheets custom menu for historic DB queries**

- `qa-automation/src/HistoryQuery.js` adds menu items under "QA
  Automation":
  - "Query history by agent…" — prompts for name + date range, hits
    `GET /api/{team_id}/history/query?agent=…&from=…&to=…`, renders
    results in a modal HtmlService dialog.
  - "Query history by call ID…" — single-eval lookup.
- New backend route `routes/history_query.py` exposes
  `GET /api/{team_id}/history/query` (PG-backed, paginated, capped at
  500 rows per response).
- Same per-team API key auth as the other endpoints. Stored in
  Apps Script Script Properties (same pattern Mass Notifications uses
  for its Postgres credentials — see `mass-notifications/src/Database.js`).

**F.3 — Decommission code paths**

After F.1 + F.2 are live for ≥2 weeks:

- Delete `SheetsProvider` (move to git history).
- Delete the `QA_PROVIDER` flag — PG is the only provider.
- Delete `data_provider.py`'s abstract base if no consumer outside
  `PostgresProvider` (likely).
- Decision point: do we keep writing to `Analyst_History` at all?
  - **Yes** — preserves human visibility + Apps Script email source.
    Stage 4 stays dual-write.
  - **No** — Apps Script reads from PG instead; AH stops being
    written; we lose the 6-month spreadsheet view. **Not
    recommended** — visibility is the whole point of keeping Sheets
    around.

---

## Sheets-as-visibility-window contract

After Phase F, the contract between the system and `Analyst_History`
is:

- **Backend writes**: still appends new rows at Stage 4 (so Apps
  Script's email source row exists). Best-effort — a Sheets write
  failure no longer blocks the approval (PG insert is the
  transactional primary).
- **Apps Script reads**: doPost reads the AH row using
  `historyRowNumber` from the payload (unchanged from Phase 2).
- **Apps Script writes (Trimmer)**: weekly trigger deletes rows older
  than 180 days.
- **Apps Script reads (HistoryQuery)**: new menu queries the backend
  for anything older than 180 days.
- **Sheet permissions**: unchanged — managers can still view + filter
  + sort the rolling 6-month window in the sheet UI.
- **No analytics or dashboard read paths touch Sheets.**

---

## Coordination with Step 8 (webhook sampler)

Step 8 was always going to need PG. Once Phase 3 lands, Step 8 is a
much smaller change:

- New `sampling_state` table with per-agent counters + last-sampled
  timestamps.
- Dialpad webhook handler decides "sample this call?" by querying
  `sampling_state`, then either drops it or hands it to the scoring
  pipeline as if a manager had uploaded it.
- Sampler decisions audited to PG.
- No frontend change — sampled calls show up in the team dashboard
  alongside manually-scored ones, marked `source='auto'`.

The big win: managers stop having to pick + upload calls. They open
the dashboard, see overnight evals, click into the ones that need
coaching, hit Approve. Step 5 → Step 9 → Step 8 in that order.

---

## Decisions (locked in 2026-05-14)

1. **DB:** reuse the existing Mass Notifications Railway Postgres;
   isolate via a `qa_evaluations` schema. Keeps all data centralized
   under one connection string; the two domains never join so schema
   isolation is sufficient blast radius.

2. **History scope:** backfill both teams' `Analyst_History` AND MS's
   `Analyst_History_legacy`. Older rows land with
   `source='migrated_legacy'` and are queryable via the same
   `PostgresProvider`.

3. **Audit log:** in scope for Phase 3 (not deferred to Step 7).
   Schema lands in Phase A; JSONL is backfilled in C.4; the audit
   middleware dual-writes in Phase D. Step 7's cost dashboard becomes
   a `SELECT … GROUP BY actor, action, ts::date` query on top.

4. **Agents:** in scope for Phase 3. Schema lands in Phase A;
   populated from Mails in C.1; reads switch in Phase E (Mails
   becomes a write-side editing surface, PG is the read source).
   Phase F.1's trimmer can use `agents.timezone` to fire per-agent if
   we ever go that granular.

5. **Time columns:** store both
   - `call_started_at` — Dialpad's call-start time, source of truth
     for analytics ("how did this agent do in March?")
   - `approved_at` — when the analyst hit Approve & Send, source of
     truth for audit ("who scored what, and when?")

   New rows from Phase D onward populate both. Phase C.3 backfills
   `call_started_at` for existing rows via
   `dialpad_client.get_call_details` (subject to Dialpad's retention
   window; older rows stay NULL).

---

## Risks + how we mitigate

| Risk | Likelihood | Mitigation |
|---|---|---|
| Dual-write desync during Phase D | Medium | Daily diff script that flags PG rows missing in AH or vice versa. Run for the whole Phase D window. |
| Backfill drops rows silently | Low | Phase C gate requires row count parity per team. Random-sample 10 rows for field-level diff. |
| `QA_PROVIDER` flag forgotten in prod | Low | Phase E starts by setting it explicitly in Railway env. CI smoke that asserts `current_provider == postgres` after Phase E lands. |
| 180-day trimmer deletes too aggressively | Medium | First run is `--dry-run` mode: logs what would delete, doesn't touch. Second run is real. Trigger fires weekly so failures recover automatically. |
| Sheets custom menu hits expired API key | Low | Same Script-Properties pattern Mass Notifications uses; documented refresh procedure. |
| PG outage blocks approvals during Phase E+ | Medium | Stage 4 PG insert wrapped in a circuit-breaker — on failure, the approval still writes to AH (so the email dispatches) and the eval is queued for PG retry. Sheets remains a viable fallback through Phase E. |

---

## Estimated effort

Loose t-shirt-style sizing. Phase 2 took ~3 weeks elapsed across
multiple sessions; Phase 3 is smaller scope (no rubric changes, no
frontend overhaul).

| Phase | Effort | Notes |
|---|---|---|
| A — Schema + provisioning (3 tables) | 1 day | SQL + migration script + Railway schema setup |
| B — PostgresProvider + audit sink | 3-4 days | Mirror SheetsProvider methods + middleware dual-write + tests |
| C.1 — `agents` backfill | 0.5 day | Walk Mails per team, normalize aliases |
| C.2 — `evaluations` backfill | 1 day | Reuse `_parse_row`; cover legacy MS tab |
| C.3 — `call_started_at` Dialpad backfill | 1 day | Throttle + cache + resume; will run hours wall-clock |
| C.4 — `audit_log` from JSONL | 0.5 day | Line-by-line parse + insert |
| D — Dual-write bridge | 2 days | `finalize_evaluation` + audit middleware sink + JSONL parity log |
| E — Read switch + verification | 1 week elapsed | Mostly soaking time |
| F.1 — Trimmer | 0.5 day | Apps Script + trigger registration |
| F.2 — History query menu | 1-2 days | New route + HtmlService modal |
| F.3 — Decommission | 0.5 day | Delete dead code + JSONL audit file + flag |

Call it **~2.5 working weeks of active dev**, plus **1 week soak** in
Phase E. Total elapsed ~3.5 weeks. The agents + audit_log additions
add ~1 day each over the original estimate.

---

## Decisions to add to PhaseOne log on completion

(Same convention as PhaseTwo — capture once Phase 3 lands.)

- Postgres lives in the Mass Notifications database with a
  `qa_evaluations` schema (no new Railway DB stood up)
- Three tables ship in Phase A: `evaluations`, `agents`, `audit_log`
- Unified `evaluations` table with `team_id` + jsonb section payloads
  (rather than per-team tables)
- Both `call_started_at` (Dialpad) and `approved_at` (analyst approval
  time) stored on every eval row; analytics default to
  `call_started_at`
- MS `Analyst_History_legacy` is backfilled into `evaluations` with
  `source='migrated_legacy'`
- `audit_log` table dual-written from Phase D; JSONL audit file
  decommissioned in Phase F.3
- `agents` table populated from Mails in Phase C.1; `/team/stats`
  reads from PG starting in Phase E
- Stage 4 stays dual-write indefinitely so AH remains a 6-month
  human-readable view + Apps Script email source
- `Analyst_History` trimmed weekly by per-team Apps Script trigger
  (Sunday 03:00 team-local time)
- `SheetsProvider` deleted post-cutover; `data_provider.py` abstract
  base retained only if a second non-PG provider is ever needed

---

## What to read before starting Phase A

1. **This doc top to bottom.** §Schema is the load-bearing part.
2. **`PhaseOne.md` §Step 8 and §Step 9.** Frames the coupling.
3. **`mass-notifications/src/Database.js`** — reference implementation
   for the Sheets-side custom menu in Phase F.2. The JDBC connection
   handling is what we'll mirror.
4. **`database/migrations/003_qa_scoring_schema.sql`** — the existing
   QA schema stub from Phase 1's deferred Postgres work. Worth seeing
   if any of it survives or gets thrown out.
5. **`backend/services/history_service.py`** — `SheetsProvider` is
   our template for `PostgresProvider`. Especially `_parse_row`,
   which Phase C's backfill reuses.
