# Agent Dashboard coaching loop — spec

*Design doc, 2026-08-13. Companion to PortManifest.md, SofiaRetellSpec.md,
LandingOpsCommandCenter.md (chiclet system), and the Railway
ScorecardActionsDesign (tampering doctrine / §4.3 override receipts).
Doc-first per working agreement. Ladder prefix **CL** (coaching loop) —
deliberately not C (Aria spec) or R (Sofia spec).*

***SIGNED OFF 2026-08-16 — owner answers recorded inline in §11.*** *Deltas
from sign-off: agent self-view is IN (v1, forward-compatible with a future
agent self-dashboard — §4.5); coaching queue gets a dedicated
`/coaching/{team}` page, not a console card (§6); binary sections get the
rolling pass-rate line in v1 (§5); `coaching_confirmed` ships as the first
Sandy-side **T2 chiclet** of the Command Center design (§6.5); CL0 was
re-diagnosed and re-fixed (v2 — §2.1).*

## 0. Goal

Close the coaching loop on the per-agent dashboard
(`/dashboard/{team}/agent/{name}`). Today we **observe** (overall trend) and
**receipt** (override/S7 write a pending `qa_coachings` row nobody ever
completes). This slice adds the rest of the cycle:

1. **See in detail** — per-section score *progression* (not just averages),
   color-coded, alongside the overall trend.
2. **Coach** — a coaching builder on the agent dashboard: pick the calls to
   review in the 1:1, record agent commitments, set a deadline.
3. **Track** — coaching sessions timestamped on the progression charts (red
   vertical dashed line) so overall + per-section response to coaching is
   visible.
4. **Verify** — a coaching queue (mirror of the Human Review queue): once the
   deadline passes, the agent's supervisor confirms whether commitments were
   met.
5. **Report** — the EOM one-pager renders the month's coaching + commitment
   outcomes, and the monthly AI assessment weighs them.
6. **Learn** — an insights layer: deterministic D1 fact extraction around
   every coaching (pre/post deltas, repeat opportunities, team-level areas of
   opportunity), narrated in natural language by Claude for trainers and
   supervisors.

Design principle carried from the review queue: **the queue is a predicate,
not a table** — state lives on the coaching row; queues are computed at read
time. No new crons (the 2-cron cap is already spent), no AI in the app
(workflow does inference), Sandy-born rows in Railway-parity tables take
high-range ids.

## 1. What exists today (audit of the live code)

| Surface | Today | Gap for this slice |
|---|---|---|
| `pages/dashboard.html` | Overall trend (Chart.js line, goal-85 dashed plugin, click→datapoint) + **Per-Section Averages bar** + one-pager card + AI-assessment card | no per-section *time series*; no coaching surfaces; AI card dead (below) |
| `GET /api/{t}/agents/{n}/history` (`records.ts::fetchRecords`) | records **already carry per-section content** (`sections[history_id] = {score, confidence, reasoning}`) | F1 is UI-only — zero API change |
| `GET /api/{t}/agents/{n}/progression` (`teamApi.ts:478`) | **503 stub** — "ports with the scoring-workflow slice (AI generation)" | F6 builds the real thing |
| D1 `qa_coachings` (0001) | full session model already ported: `conducted_by_role/email`, `status pending\|completed\|cancelled`, `action_plan`, **`action_plan_deadline`**, `coaching_summary`, `agent_attitude`, `scheduled_at`, `completed_at/by`; CHECK: completed ⇒ summary+at+by; partial index `(team_id, action_plan_deadline) WHERE status='pending'` | no structured commitments; no post-deadline outcome; no UI anywhere |
| D1 `qa_coaching_evaluations` | junction w/ `opportunities_snapshot`, `per_eval_note`, UNIQUE (coaching, eval) | exactly the F2 call-picker target |
| `scoring.ts::coachingReceipt` (:807) | §4.3 override **always** and S7 edit-of-finalized create a `pending` receipt (high-range id, junction row w/ opportunities snapshot) — no deadline, and **no completion flow exists on Sandy** | the builder must be able to upgrade these receipts |
| Review queue (`reviewQueue` + `index.html` §0.3 card) | predicate `human_review_required_at IS NOT NULL AND human_review_completed_at IS NULL`; exits stamp `completed_at`; card w/ origin pills | the template F4 mirrors |
| `lib/onepager.ts` | renders **persisted** `qa_assessments` only ("a page view must never spend a Gemini call"); assessment generation never ported | F5 adds coaching block; F6 restores generation |
| `lib/rbac.ts` | `manager` role **RESERVED** — "per-team semantics land when manager capabilities are defined; today it maps to viewer" | this is that moment: supervisors confirm commitments |
| `qa_score_audit` | `action` CHECK admits only `scored\|denied\|approved\|evaluation_orphaned\|rescored\|overridden` — SQLite can't widen a CHECK without a table rebuild | coaching lifecycle self-audits on its own rows + `qa_events` (free-typed) instead |
| `wrangler.toml` crons | `7 * * * *` (queue pump + Retell sweep) + `37 9 * * *` (maintenance + digest) — **cap reached** | EOM generation rides the daily cron (day==1 branch) |
| `workflows/qa-scoring-pipeline.js` | generic executor, but audio-shaped and **one-active-run**, serialized by `qa_score_queue` | insights get their **own** workflow (own serialization domain) |
| `scripts/shadow_sync.py` | wipe order range-scopes children but **`qa_coachings` / `qa_assessments` / `qa_assessment_sections` wipes were unscoped** | **CL0 — live incident, see §2** |

Charts quirk to respect (v0.34 finding): charts plot by **call date on a
category axis** (one tick per eval, not a time scale) — coaching markers must
interpolate an index position, not a timestamp.

## 2. CL0 — shadow-sync FK incident (prerequisite; fix applied 2026-08-13)

**The shadow sync has failed on every run since 2026-08-03 12:30** (last
`SHADOW SYNC OK` in `scripts/shadow_sync.log`; 150+ consecutive FAILs, plus
Aug-8/9 401s from the token expiry). Root cause: the wipe batch runs under
`PRAGMA defer_foreign_keys = true`; children (`qa_coaching_evaluations`) are
range-scoped so the 9 Sandy-born receipt junction rows survive, but the
parent `qa_coachings` wipe was **unscoped** — at commit the deferred FK check
fails and **D1 rolls the entire batch back**:

> "D1 DB was reset and rolled back to its last known good state … FOREIGN KEY
> constraint failed"

Consequences while it failed: the Railway mirror (evals, review-queue rows,
`eval_approved` SSE toasts, cc incremental) is **stale since Aug 3**; nightly
parity has been comparing against a frozen frame. No data was lost — every
failed batch rolled back atomically.

**Fix (in working tree, this session):** `WIPE_ORDER` now range-scopes
`qa_coachings` (`id <`), `qa_assessments` (`id <`), and
`qa_assessment_sections` (`assessment_id <`) — the sync owns the Railway
range *of every table it wipes*, which is the #174 doctrine this drifted
from. The first coaching receipt (2026-08-03, v0.14 override era) is what
armed the bug; CL4's Sandy-born assessments would have re-armed it — hence
scoping all three now.

**Operator follow-ups:** (1) commit the fix; (2) run the sync manually or
let the :00/:30 cron catch up (~4.5 min; expect a large `+events published`
burst — harmless, 7-day prune); (3) confirm `SHADOW SYNC OK` and that the
finalized-eval count moves past 2505; (4) expect the next nightly parity to
go green again.

### 2.1 CL0 v2 (2026-08-16) — the v1 fix was necessary but not sufficient

The sync kept failing after v1: **`qa_agents` was also wiped unscoped**, and
surviving Sandy-born rows (evals, coachings — and CL4's future assessments)
hold `agent_id` FKs into it. The re-import restores the same agent ids, but
in a *later transaction* — the wipe batch's deferred-FK check fails at its
own commit. Two further problems hid underneath:

- **The sofia roster row has no Railway counterpart** — an agent wipe would
  have destroyed it (and Jackson's supervisor routing for the R4 sweep) with
  nothing to re-import. The FK rollback was accidentally *protecting* sofia.
- **Roster id collision:** migration 0005 seeded sofia at AUTOINCREMENT
  id 184; Railway's max agent id is 183 — the next Railway hire would have
  collided. **Remediated 2026-08-16:** sofia re-id'd to 10000000 (Sandy-born
  range) with all referencing rows re-pointed (30 evals; verified zero
  orphans). Doctrine going forward: **provider-team roster seeds INSERT with
  explicit high-range ids** (§10).

**Fix v2:** `qa_agents` leaves `WIPE_ORDER` and `RESYNC` entirely; a new
`sync_agents_upsert` step upserts the PG roster by id (`ON CONFLICT(id) DO
UPDATE` — the cc_calls pattern, explicitly not `INSERT OR REPLACE`), never
deletes; departures propagate as `active=0` updates. With agents stable,
every surviving Sandy row's FK web (teams / agents / rubric / formula /
cc_calls — none wiped) is intact at commit.

## 3. Schema — migration `0007_coaching_loop.sql`

House pattern: author via the generator script, validate by applying
0001→0007 to in-memory sqlite before touching live D1.

```sql
-- Post-deadline verification stamped on the session (derived from
-- commitment statuses at confirm time; stored for cheap F5/F6 reads).
ALTER TABLE qa_coachings ADD COLUMN outcome TEXT
  CHECK (outcome IS NULL OR outcome IN ('met','partially_met','not_met'));
ALTER TABLE qa_coachings ADD COLUMN outcome_confirmed_by TEXT;
ALTER TABLE qa_coachings ADD COLUMN outcome_confirmed_at TEXT;
ALTER TABLE qa_coachings ADD COLUMN outcome_note TEXT;

-- Structured commitments (F2). Sandy-only table: no Railway counterpart,
-- NOT added to shadow_sync WIPE/RESYNC — plain AUTOINCREMENT ids are fine.
-- FK targets Sandy-born coachings only (API refuses Railway-born, §4).
CREATE TABLE qa_coaching_commitments (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    coaching_id        INTEGER NOT NULL REFERENCES qa_coachings(id) ON DELETE CASCADE,
    commitment         TEXT NOT NULL CHECK (length(trim(commitment)) > 0),
    section_id         TEXT,     -- optional rubric link; feeds F6 per-section analysis
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','met','partially_met','not_met','waived')),
    confirmed_by       TEXT,
    confirmed_at       TEXT,
    confirmation_note  TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (status = 'open' OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL))
);
CREATE INDEX idx_coaching_commitments_coaching
    ON qa_coaching_commitments (coaching_id);

-- Persisted insight narratives (F6, scopes coaching|team). Sandy-only.
-- Agent-window progression assessments do NOT land here — they reuse
-- qa_assessments/qa_assessment_sections (Railway-parity shape the
-- one-pager + dashboard already read), at high-range ids.
CREATE TABLE qa_coaching_insights (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scope               TEXT NOT NULL CHECK (scope IN ('coaching','team')),
    team_id             TEXT NOT NULL REFERENCES teams(id),
    agent_id            INTEGER REFERENCES qa_agents(id),
    coaching_id         INTEGER REFERENCES qa_coachings(id),
    window_start        TEXT,
    window_end          TEXT,
    facts               TEXT NOT NULL CHECK (json_valid(facts)),
    narrative           TEXT NOT NULL,
    models_used         TEXT NOT NULL CHECK (json_valid(models_used)),
    estimated_cost_usd  REAL,
    generated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    is_current          INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1))
);
CREATE INDEX idx_coaching_insights_current
    ON qa_coaching_insights (team_id, scope, generated_at DESC)
    WHERE is_current = 1;

-- F4 queue predicate index: conducted sessions awaiting confirmation.
CREATE INDEX idx_coachings_confirm_queue
    ON qa_coachings (team_id, action_plan_deadline)
    WHERE status = 'completed' AND outcome IS NULL;

-- §6.5 chiclet type widening: cc_chiclets.type CHECK is closed and SQLite
-- can't ALTER a CHECK — but BOTH chiclet tables are EMPTY in D1 (verified
-- 2026-08-16; cc_* incremental sync carries calls/holds/webhooks only, so
-- D1 chiclets are Sandy-owned from here on). Rebuild in place: recreate
-- cc_chiclets with 'coaching' added to the type enum (all other columns
-- byte-identical to 0001), recreate cc_chiclet_events against it
-- (event_type enum unchanged), re-create the 0001 indexes. No data copy.

-- CL1 build finding: qa_coaching_evaluations rebuilds WITHOUT the
-- evaluation_id FK (coaching_id FK + UNIQUE + index kept, rows copied).
-- Why: the builder links Sandy-born coachings to RAILWAY-born evals, and
-- a cross-ownership FK cannot survive the sync's wipe-then-reimport-in-
-- separate-transactions model — the linked eval is deleted at the wipe
-- commit (deferred-FK failure, the exact CL0 bug class) no matter how the
-- junction wipe is scoped. Dropping the FK + scoping the junction wipe by
-- COACHING_ID (shadow_sync.py change: Railway junctions wipe+reimport
-- with their coachings; Sandy junctions always survive) makes links to
-- either origin sync-safe. Reads LEFT JOIN evals (a Railway-side delete
-- leaves the snapshot columns); deleteEvaluation keeps its explicit
-- unlink; app code owns integrity, as everywhere else in the high range.
```

**Session lifecycle** (uses the existing status enum + CHECK unchanged):

```
pending    created/planned (builder or override/S7 receipt) — editable
   │  POST /conduct  (coaching_summary + agent_attitude required — the 1:1 happened)
   ▼
completed  documented; completed_at = the red-line timestamp (F3)
   │  deadline passes → appears in coaching queue (F4)
   │  POST /confirm  (per-commitment verdicts → derived outcome stamped)
   ▼
completed + outcome                    (cancelled exits at any pre-confirm point)
```

Sandy-born id doctrine: `qa_coachings` inserts keep the existing
`MAX(id)+1 >= 10_000_000` allocation (`coachingReceipt` pattern). Railway-born
coachings (6 exist in D1) render read-only with an origin pill; every
mutating endpoint answers the house 409 ("act on Railway") below the base.

Audit: **no new `qa_score_audit` action values** (CHECK is closed and SQLite
can't widen it in place). The coaching row is its own audit trail
(`created_at/completed_by/outcome_confirmed_by…`); lifecycle transitions also
publish `qa_events` rows (`coaching_logged`, `coaching_confirmed` — the type
column is free-text) for optional SSE toasts. The existing override/S7 audit
notes (`coaching={id}`) already link receipts.

## 4. API surface — new module `src/routes/coaching.ts`

Wired from `teamApi.ts` like `scoring.ts` (dynamic import per route).

| Route | Method | Gate | Behavior |
|---|---|---|---|
| `/api/{t}/agents/{n}/coachings?days=` | GET | coach **or self** | sessions + commitments + linked evals (per-eval notes, opportunities snapshots) + per-session response stats (§8 facts, scope=coaching, no AI) — powers the dashboard card and chart markers. **Self responses are redacted** (§4.5) |
| `/api/{t}/coachings` | POST | coach | create `pending`: `{agent_name, eval_ids[], per_eval_notes{}, commitments[{text, section_id?}], deadline, scheduled_at?, conducted_by_role?}`; deadline required; high-range id; junction rows snapshot each eval's `opportunities` |
| `/api/{t}/coachings/{id}` | PATCH | coach | pending-only edits (incl. upgrading an override/S7 receipt with calls/commitments/deadline); Sandy-born only |
| `/api/{t}/coachings/{id}/conduct` | POST | coach | `{coaching_summary, agent_attitude}` → `completed`, `completed_by` = Access email (satisfies the 0001 CHECK) |
| `/api/{t}/coachings/{id}/cancel` | POST | coach | `cancelled` (pre-confirm only) |
| `/api/{t}/coaching-queue` | GET | coach | due = `status='completed' AND outcome IS NULL AND action_plan_deadline <= today(LA)`; response also carries an `upcoming` group (deadline in future) for visibility |
| `/api/{t}/coachings/{id}/confirm` | POST | coach | `{commitments:[{id, status, note?}], outcome_note?}` — every commitment leaves `open`; outcome derived (all met→`met`; any met/partial→`partially_met`; else `not_met`; `waived` excluded from the vote) and stamped with confirmer + timestamp |

**RBAC — `manager` role activates.** `resolveAccess` gains a `coach`
capability: `admin | qa | (manager AND (team_id IS NULL OR team_id = t))`.
`privileged` (scorecard actions, lookup, scoring) is **unchanged** — managers
get coaching surfaces only. `whoami` adds `can_coach`; pages render coaching
UI off it (API is the real wall, as always). Supervisors are granted
`manager` rows via the existing `/admin` UI — no code, no auto-grant from
`supervisor_email` (roles stay explicit and revocable).

Evaluator identity on every mutation: body value → `accessEmail(request)`
fallback (the #175/v0.31 house pattern).

### 4.5 Agent self-view (owner answer §11.1 — IN for v1, forward-compatible)

Agents need a place to see their own commitments. New rbac helper
`selfAgentFor(request, db, teamId)` → the active `qa_agents` row whose
`email` matches the CF-Access identity (null otherwise). The coachings GET
allows `coach || self(agent n)`; **self responses redact supervisor-side
judgment**: `agent_attitude`, `per_eval_note`, `confirmation_note`, and
`outcome_note` are stripped — the agent sees dates, linked calls,
commitment texts + statuses, deadline, and outcome. Mutations stay
coach-only.

**Forward-compat contract** (the future agent self-dashboard — own QA
average + std, delegated tasks, richer widgets — is out of scope this
slice, but nothing may block it): every agent-scoped read goes through
capability-keyed gates (`coach | self`) rather than page-level walls, so a
future `/me` page can compose the same APIs; `whoami` gains `self_agent`
(the caller's roster row, when any) next to `can_coach`; redaction lives in
ONE response-shaping function (`redactForSelf`) so new fields default to
the redacted path; and the existing `/history` + summary-stat computations
stay reusable client-side (the self-dashboard's QA avg/std come from the
same endpoint the coach dashboard already uses, gated `self` the same way).
On the current dashboard, a self-visiting agent gets: charts + markers
(redacted tooltips) + a read-only "My commitments" variant of the coaching
card (§5).

## 5. Agent dashboard UI (`pages/dashboard.html`)

**F1 — Per-Section Progression card** (new, between Overall Trend and the
existing averages bar, which stays — it answers "where does the agent sit",
the new chart answers "where are they heading"):

- Chart.js multi-line, x = the same per-eval category axis, y = 1–5.
- One line per **numeric** section (`_numericSectionKeys`, same filter as the
  bar chart), distinct hue per section, legend chips toggle lines. Missing/NA
  = gap (`spanGaps: false`). Point radius small (3) to keep 6+ lines legible;
  tooltip = section name + score + reasoning first line; click → datapoint.
- Band thresholds stay the house 1–5 scale (≥4.25 green / ≥3.5 amber / red)
  for the *y-axis gridline tint only* — line color = section identity, so
  color-coding reads as "which section" first, "how good" via position.
- YN/binary sections plot as **rolling pass-rate lines** (owner answer
  §11.9 — v1, not deferred): trailing-5 applicable evals (NA excluded;
  fewer than 3 applicable → no point), `Y/(Y+N)` as a percentage,
  **dashed** line style to distinguish the derived series from raw numeric
  ones. *CL2 build amendment:* NOT on a second y-axis of the numeric chart
  — dual-axis is the canonical chart anti-pattern — but as a **second
  stacked panel** in the same card (own 0–100% axis, shared x categories,
  own legend). Each panel assigns its hues from slot 1 of the validated
  categorical order, so neither panel ever cycles. Tooltip shows the
  window ("4/5 passed, last 5 applicable").

**F3 — coaching markers** on BOTH the overall and section charts: a shared
`coachingMarkers` Chart.js plugin (same afterDraw pattern as the existing
`goalLine`). For each session with `completed_at` inside the window: red
(`--red #D9534F`) vertical **dashed** line at interpolated index (first eval
with `ts >= completed_at`, drawn at `index − 0.5`; after the last eval →
right edge), label "Coaching MM/DD", hover/tooltip = commitments count +
outcome badge (met green / partially amber / not_met red / unconfirmed gray
"due MM/DD"). Sessions land from the same `/coachings` fetch the card uses;
a 403 (non-coach viewer) silently renders plain charts.

Markers and the coaching card key off the coachings fetch: `can_coach` gets
the full card; a **self-visiting agent** (`whoami.self_agent` matches the
page agent) gets markers plus a read-only **"My commitments"** card —
commitment texts, statuses, deadline, outcome; no builder, no attitude, no
internal notes (§4.5 redaction). Anyone else gets plain charts (the 403 is
swallowed).

**F2 — Coaching card** (full version renders only when `whoami.can_coach`):

- Session list: status pill (pending / conducted / due / met / partially /
  not met / cancelled), origin pill (sandy/railway — Railway rows read-only),
  deadline, commitments with status glyphs, linked calls (→ datapoint), and
  the per-session **response readout**: evals since `completed_at`, overall
  avg before → after, per-coached-section before → after (from §8 facts —
  pure D1, always free).
- **New coaching session** builder: multi-select dropdown of the current
  window's evaluations (date · score · opportunities snippet — from the
  already-loaded history records), optional per-call note, commitments
  repeater (text + optional section dropdown from `/team/sections`), deadline
  date, optional scheduled-at. Pending override/S7 receipts offer "Upgrade to
  full session" (PATCH) instead of creating a duplicate.
- Conduct flow: "Mark conducted" → summary textarea + attitude select
  (existing enum: receptive/engaged/neutral/defensive/dismissive/mixed).

## 6. Coaching queue (F4) — dedicated `/coaching/{team}` page

Owner answer §11.2: **a dedicated page, not a console card** — the coaching
queue must never be conflated with the human-review call queue. New
`pages/coaching.html` served at `/coaching/{team}`, gated `can_coach` (403 →
the §RBAC denial page with the request-access flow, `page: "coaching"` added
to `qa_access_requests`' enum use). The scoring console (`/score/{team}`)
stays exactly as it is — gate untouched (`privileged`), review-queue card
untouched. Nav: greeting-page card + a header link rendered when
`whoami.can_coach`.

Page layout (console visual language — cards, DM Mono, queue-table CSS):

1. **Due for confirmation** (the queue proper): predicate rows, ordered by
   deadline.

   | Agent | Conducted | Deadline | Overdue by | Commitments | |
   |---|---|---|---|---|---|
   | name → agent dashboard | date | date | Nd | 3 open | **Confirm** |

   Confirm expands inline: the facts panel (post-coaching evals n, overall
   before→after, coached sections before→after — §8 scope=coaching) + one
   met/partially/not-met/waived selector + note per commitment + outcome
   note → POST `/confirm`.
2. **Upcoming** — conducted sessions whose deadline hasn't arrived (muted).
3. **All sessions** — filterable list (status, agent), origin pills;
   Railway-born rows read-only and never queue-eligible.

Sofia note: coaching surfaces are team-generic and render for `sofia` too,
but the expected consumers are the human teams — "coaching" Sofia happens in
Retell prompt builds (rubric-hardening R3-tail), not 1:1s.

### 6.5 `coaching_confirmed` → the first Sandy-side T2 chiclet (owner §11.10)

Owner decision: the confirmation toast ships as the **first implementation
of the LandingOpsCommandCenter chiclet system on Sandy** (D1 `cc_chiclets`
is empty and Sandy-owned — the cc incremental sync carries calls/holds/
webhooks only — so Sandy takes over the table cleanly).

**Write path** (inside the `/confirm` handler, same transaction group):

- `cc_chiclets` INSERT: `type='coaching'` (enum widened in 0007 — §3),
  `tier='T2'` (CC §2: important, needs attention soon, not interruptive),
  `status='active'`, `agent_name`, `summary` = "Coaching outcome: {met|
  partially met|not met} — {agent}, {n} commitments", `data` JSON
  `{coaching_id, outcome, commitments: {met, partially_met, not_met,
  waived}, deadline, confirmed_by}`. `source_event_id` stays NULL (QA
  pipeline source, not a Dialpad webhook — mirrors the qa_outlier type).
- `cc_chiclet_events` INSERT `event_type='created'`.
- `qa_events` INSERT `type='chiclet_created'`, payload = the CC §6 SSE
  shape (`{id, tier: 2, type: "coaching", chiclet: {...}}`) — the qa_events
  D1-bus is Sandy's SSE transport, so the event name + payload match the CC
  protocol the future ported CC page will consume unchanged.

**Render path (first chiclet rail):** `team_dashboard.html` gains a minimal
**alert-rail strip** above the toasts — active `cc_chiclets` for the team
(initial fetch via new `GET /api/{t}/chiclets?status=active`, live inserts
via the existing SSE listener picking up `chiclet_created`). Each chiclet:
amber T2 left border per the CC border-state system, QA source stripe,
summary line, actions **"View coaching"** (→ `/coaching/{team}`) and
**"Agent dashboard"**, and **Acknowledge** → `POST
/api/{t}/chiclets/{id}/resolve` (`resolved_by` = Access email; emits
`cc_chiclet_events` 'resolved' + `qa_events` `chiclet_resolved`). Resolution
is manual-ack only (CC qa_outlier semantics — no day-boundary auto-resolve:
an outcome isn't a today-counter). Tier is fixed at T2 (no escalation path
in v1).

This deliberately implements a thin vertical slice of CC Zone B: real
chiclet rows, real event log, protocol-correct SSE — so the eventual CC
port inherits working data instead of a parallel toast hack.

## 7. EOM one-pager (F5) — `lib/onepager.ts`

New **Coaching & commitments** block between the section table and the AI
assessment, for sessions whose `completed_at` *or* `action_plan_deadline`
falls in the month:

- per session: conducted date + by-role, deadline, outcome badge, commitments
  with status glyphs (✓ met / ◐ partially / ✗ not met / – waived / ○ open),
  and the response readout (before → after on coached sections).
- month with zero sessions renders nothing (the block, like the assessment,
  is additive — print layout unchanged otherwise).

The month **AI assessment** becomes coaching-aware via §8: its prompt carries
the month's coaching facts, so `overall_assessment` (and section
`coaching_tip`s) explicitly reckon with commitments met or not met. No
`qa_assessments` schema change — Sandy-born assessments just take high-range
ids (the CL0 fix already protects them from the sync).

## 8. Insights layer (F6) — facts from D1, narrative from Claude

**Two strict layers** (owner requirement: factual grounding first):

**Layer 1 — `src/lib/coachingFacts.ts`** (pure D1 aggregation, no AI; reused
by the dashboard response readout, queue facts panel, one-pager block, and
every prompt below). Three scopes:

- `agent(team, agent, window)` — overall + per-section series stats and
  trend deltas; coachings in window w/ commitments + outcomes; per-coaching
  pre/post deltas.
- `coaching(id)` — linked evals' opportunities snapshots; coached sections
  (commitment `section_id`s ∪ sections scoring low on linked evals);
  before = linked evals + prior window, after = `completed_at → deadline`
  (commitment-verdict window) and `completed_at → now` (progression window);
  commitment statuses.
- `team(team, window)` — most-coached sections; least-improved coached
  sections; repeat opportunities (sections coached ≥2× still below band);
  coverage (agents with/without sessions); queue hygiene (overdue
  confirmations, receipts never conducted); commitment met-rate. This is the
  "areas of opportunity we need to tackle most" ranking — computed, then
  narrated.

**Layer 2 — workflow `qa-insights` v0.1** (new; text-only, tiny — no npm
deps, plain fetch). Separate from `qa-scoring-pipeline` so its one-active-run
never contends with the scoring queue. Payload (prompts built app-side, house
split):

```
{ mode: "progression" | "coaching" | "team" | "eom_batch",
  model: { provider: "anthropic", model: "claude-sonnet-5", max_tokens },
  items: [ { ref, system, prompt } ],   // one per agent; eom_batch = many
  callback_url, callback_token }
```

Claude via the AI Gateway anthropic passthrough (`cf-aig-authorization`, no
Bearer — same as the judge leg; billing = engineering's central account, per
the 2026-07-17 doctrine). One `step.do` per item; single HMAC callback with
all results; app-side `insightsCallback` persists:

- `progression` / `eom_batch` → `qa_assessments` + `qa_assessment_sections`
  (high-range ids, `is_current` flip on prior rows for the same
  agent/window) — the shape `onepager.ts` and the dashboard already read.
  Response schema = the Railway progression contract
  (`overall_assessment`, per-section `{trend, summary, coaching_tip}`),
  enforced via tool-style JSON instruction + app-side validation.
- `coaching` / `team` → `qa_coaching_insights` (facts JSON stored beside the
  narrative — the reader can always check the numbers the prose cites).

**Endpoints & surfaces:**

- `POST /api/{t}/agents/{n}/progression` → builds facts, triggers a
  `progression` run, returns `{job_id}`; `GET` (existing dashboard route)
  serves the persisted current assessment when fresh (≤1h), else 202 +
  job status. `dashboard.html`'s AI card goes async (trigger → poll →
  render) — a page-contract change from Railway's synchronous endpoint,
  required by the platform's no-AI-in-apps rule. Cache semantics (1h,
  sessionStorage) unchanged.
- **EOM**: `runDailyMaintenance` ("37 9 * * *") branches on LA
  day-of-month == 1 → one `eom_batch` run for the closed month: every agent
  with finalized evals in that month **and no current assessment already
  intersecting it** (the idempotency guard that also prevents double-spend
  while Railway's own monthly export still writes assessments during
  shadow — Sandy fills gaps, e.g. sofia, and takes over fully at cutover).
- **Team insights**: a "Team insights" card on `/coaching/{team}`
  (`can_coach`) — the §11.2 page decision moved every coach-facing tool
  there, superseding this section's original console placement. Shows the
  current `team` narrative + fact summary, button to regenerate.
- Concurrency: one-active-run 409s surface as "insights engine busy — retry
  shortly" (no insights queue in v1; revisit only if it actually bites).

Cost note: sonnet-5 narrative per agent ≈ a few cents (facts prompts are
small — no transcripts); EOM batch for both human teams well under $1/mo.
Model pinned, never `dynamic/sandy-workflows` (trial-config parity rule).
Owner confirmed sonnet-5 at sign-off (§11.5 — judge parity).

**Billing doctrine + the "qa-key" residual spend (audited 2026-08-16):**
every Sandy-side Claude call in this spec rides the AI Gateway
(engineering's central account). The residual spend on the owner's
self-provided Anthropic key ("qa-key") is **Railway-side**: the key chain in
`backend/services/llm/anthropic.py` (`ANTHROPIC_API_KEY` → `_LANDING` →
`_PERSONAL`, ≥20 chars wins) resolves to `ANTHROPIC_API_KEY_PERSONAL` — the
only Anthropic var present — and the **Stage-B scoring judge**
(`judge_service.py`, `SCORING_MODEL_PROVIDER=anthropic` since the Claude
flip) burns it on every Railway-scored MS/Sales call. The `progression`
stage defaults to Gemini (no `PROGRESSION_MODEL_PROVIDER` set), so it is
not a spend source. Retirement: the spend ends at cutover with Railway
scoring itself; the interim zero-code fix is Engineering delivering a
Landing-funded key into `ANTHROPIC_API_KEY`/`_LANDING` on the Railway env —
the chain was built so an engineering key silently wins over the personal
one. Added to the sit-down list.

## 9. Rollout ladder (CL0–CL5 — each deployable + verifiable)

- **CL0 — sync fix** *(v1 2026-08-13; v2 + sofia re-id DONE 2026-08-16 —
  §2/§2.1)*: agents upsert-only, parents range-scoped, sofia at 10000000.
  Gate: cron run logs `SHADOW SYNC OK`, finalized count moves past 2505,
  next nightly parity green. Then commit.
- **CL1 — schema + coaching API**: migration 0007 (sqlite-validated, then
  live) incl. the cc_chiclets enum rebuild; `routes/coaching.ts` (all §4
  routes); `rbac.ts` `coach` capability + `selfAgentFor` + `whoami`
  `can_coach`/`self_agent`; `qa_events` publications. Gate: curl the full
  lifecycle on a test agent (create → conduct → deadline passes → queue
  shows → confirm → outcome stamped + chiclet row + `chiclet_created`
  event); Railway-born coaching 409s; manager-role user can coach but not
  score; self GET is redacted.
- **CL2 — dashboard**: per-section progression chart (numeric lines +
  binary pass-rate lines) + coaching markers + coaching card/builder + "My
  commitments" self variant (§5). Gate: real session on a real agent
  renders the colored section lines, a dashed pass-rate line, and the red
  dashed marker at the conduct date; a rostered agent visiting their own
  page sees markers + redacted commitments card; other viewers see clean
  charts and no card.
- **CL3 — `/coaching/{team}` page + chiclet rail**: queue/upcoming/all
  sections with the confirm flow + facts panel (§6); chiclet write path +
  `GET /chiclets` + resolve endpoint + team-dashboard alert-rail strip
  (§6.5). Gate: a supervisor (manager role, not qa) confirms a due session
  end-to-end from the page; the T2 chiclet appears live on the team
  dashboard (SSE, no refresh) and Acknowledge resolves it.
- **CL4 — one-pager + insights engine**: coaching block in `onepager.ts`;
  `qa-insights` workflow v0.1 published + enabled + allowed-app wired;
  `coachingFacts.ts`; progression endpoint live (async contract);
  EOM day==1 branch. Gate: generate a real assessment whose text cites a
  commitment outcome; one-pager renders block + assessment; re-run is
  idempotent (no duplicate assessment).
- **CL5 — team insights**: `team` facts + narrative + console card. Gate:
  narrative's cited numbers hand-checked against SQL; regenerate flips
  `is_current`.

## 10. Constraints honored (checklist)

- **App never calls AI** — inference in `qa-insights` workflow; facts/prompts
  built app-side (the scoring-pipeline design split).
- **2-cron cap** — nothing new; EOM rides daily maintenance, queue is
  read-time.
- **One-active-run per workflow** — new workflow = own slot; EOM batches in
  a single run; on-demand 409 → user-visible retry.
- **Sandy-born high-range ids** in Railway-parity tables (`qa_coachings`,
  `qa_assessments`); Sandy-only tables (`qa_coaching_commitments`,
  `qa_coaching_insights`) stay off the sync's WIPE/RESYNC lists.
- **Roster-seed doctrine (new, from CL0 v2)** — provider-team roster/seed
  rows in Railway-parity tables INSERT with **explicit high-range ids**,
  never AUTOINCREMENT (the sofia-at-184 collision, §2.1). Any new
  Sandy-born row class in a synced table needs its matching range guard in
  `shadow_sync.py` before first write.
- **Shadow doctrine** — sync owns the Railway range only (CL0 restores
  this); mutations refuse Railway-born rows with the house 409.
- **Chiclet tables are Sandy-owned** — D1 `cc_chiclets`/`cc_chiclet_events`
  are empty and outside the cc incremental sync; the 0007 rebuild must not
  be re-pointed at Railway's chiclet tables if the CC slice later changes
  the sync surface.
- **Parity harness** — `historyFrame`/`records` shapes untouched (F1 is
  UI-only; coaching data rides new endpoints). If anything in `records.ts`
  does get touched, run the parity harness before deploy (v0.32 lesson).
- **`qa_score_audit` CHECK is closed** — no new action strings; coaching
  rows self-audit + `qa_events`.
- **D1 mechanics** — chunked `IN` lists (no array binds), ~100KB statement
  cap, migration keyword scan (no literal `ATTACH`), migrations validated on
  in-memory sqlite first.
- **Secrets** — none new: `AI_GATEWAY_TOKEN` is org-injected, callback HMAC
  exists, no per-team keys (20-char name cap stays irrelevant).
- **Spanish-audio SOT** — untouched (no scoring-prompt changes in this
  slice).
- **Email** — no coaching emails in v1; seam noted at conduct/confirm
  (suppress-first culture: coaching conversations already suppress scorecard
  emails deliberately). Slack notifications stay deferred to the designed
  Slack pass (same as access requests).

## 11. Open questions — ✅ ANSWERED at sign-off (owner/Max, 2026-08-16)

1. **Agent self-view: YES** — agents need a place to see their current
   commitments, and this seeds a future **agent self-dashboard** (own QA
   average + std, delegated tasks, more). Full dashboard out of scope this
   slice; **code must be forward-compatible with it** → §4.5 (capability-
   keyed gates, `self_agent` on whoami, single redaction seam).
2. **Queue surface: dedicated `/coaching/{team}` page** — the coaching
   queue must never be mixed up with the human-review call queue → §6.
3. **Manager scope: coaching-only** — as specced; lookup stays
   `privileged`.
4. **Windows: as specced** (verdict `completed_at → deadline`; deltas
   ±30d); more windows may come later — keep them tunable constants.
5. **Insights model: `claude-sonnet-5`** (judge parity). Rider task —
   audit the self-provided "qa-key" Anthropic spend: **done**, findings +
   retirement path in §8; all Anthropic spend must land on Engineering.
6. **EOM scope: assessments without coachings too** — matches current
   Railway behavior.
7. **Existing 9 receipts: leave pending** — acceptable; the coaching arm
   didn't exist when they were written. Upgradeable via the builder.
8. **Red-line timestamp: `completed_at`** (conduct) — confirmed.
9. **Binary pass-rate line: IN for v1** — not deferred → §5 (trailing-5
   applicable, right-hand % axis, dashed).
10. **Toast → chiclet: the `coaching_confirmed` event ships as the first
    Sandy-side T2 chiclet** of the LandingOpsCommandCenter design → §6.5
    (widened `cc_chiclets.type`, protocol-correct `chiclet_created`/
    `chiclet_resolved` SSE, alert-rail strip on the team dashboard).

## 12. File / change map

| File | Change | Ladder |
|---|---|---|
| `scripts/shadow_sync.py` | v1 range guards + v2 agents upsert (**done 08-13/08-16, uncommitted**) | CL0 |
| D1 data fix | sofia roster re-id 184 → 10000000 + FK re-point (**applied 2026-08-16**) | CL0 |
| `migrations/0007_coaching_loop.sql` | §3 (commitments, outcome cols, insights table, queue index, cc_chiclets enum rebuild) | CL1 |
| `src/routes/coaching.ts` | **new** — §4 routes + chiclet list/resolve | CL1/CL3 |
| `src/routes/teamApi.ts` | wire coaching routes; `/coaching/{t}` page route + `coaching` denial page kind; `whoami` `can_coach`/`self_agent` | CL1/CL3 |
| `src/lib/rbac.ts` | `coach` capability (manager activation) + `selfAgentFor` + `redactForSelf` seam | CL1 |
| `pages/dashboard.html` | section-progression chart (numeric + pass-rate lines), markers plugin, coaching card/builder + self variant, async AI card | CL2/CL4 |
| `pages/coaching.html` | **new** — §6 queue/upcoming/all + confirm flow | CL3 |
| `pages/team_dashboard.html` | §6.5 alert-rail strip (chiclet fetch + SSE listeners + Acknowledge) | CL3 |
| `src/lib/coachingFacts.ts` | **new** — §8 layer 1 | CL4 |
| `src/lib/onepager.ts` | coaching block | CL4 |
| `workflows/qa-insights.js` | **new** — §8 layer 2 (publish + enable + allow app) | CL4 |
| `src/routes/scoring.ts` or `insights.ts` | `insightsCallback` persist + progression trigger/poll | CL4 |
| `src/lib/maintenance.ts` | EOM day==1 branch | CL4 |
| Railway env (operator) | Landing-funded key → `ANTHROPIC_API_KEY`/`_LANDING` (§8 qa-key retirement; sit-down ask) | — |
| `references/CoachingLoopSpec.md` | this doc | — |
