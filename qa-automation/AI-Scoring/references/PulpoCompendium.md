# PulpoCompendium — weekly SOP snapshot → Google Doc + T2 ack chiclet

> The one-off Pulpo SOP Compendium (2026-07-30: 114 docs, full contents,
> clickable index, delivered into a user-owned Google Doc) becomes a
> weekly institution: every Monday 05:00 America/Mexico_City the backend
> re-snapshots the corpus, refreshes the Doc, and surfaces a **T2
> chiclet on the QA dashboard** with a change résumé that a manager must
> acknowledge. The chiclet is written to the REAL Command Center ledger
> (`command_center.chiclets`) — this feature is the first live writer of
> the CC chiclet data model, so the future CC alert rail inherits its
> history unchanged.

| | |
|---|---|
| **Status** | v1.0 — 2026-07-31, §6 open questions PENDING owner |
| **First consumer** | MS managers on `/dashboard/member_support` |
| **Artifact** | the `PULPO_COMPENDIUM_DOC_ID` Google Doc (user-owned; SA is editor — service accounts have zero Drive quota and can NEVER create files, only edit) |
| **Pipeline** | `scripts/pulpo_compendium_export.py` (landed with this doc): extract → diff → build → deliver, all stages pytest-covered where pure |
| **Chiclet semantics** | LandingOpsCommandCenter.md §4 (T2 = ack-required, amber), §6 SSE contract; ack gate mirrors ScorecardActionsDesign §4.3a (`acknowledged: true` or 422) |
| **Related** | PulpoConnection.md (provider seam, quota citizenship), `disposition_pull.py` (lifespan-task pattern), `event_bus.py` (QA SSE) |

## 1. What exists today (verified in-repo 2026-07-31)

- **Pipeline**: `scripts/pulpo_compendium_export.py` — full-corpus
  enumeration (trending-90d ∪ tag transitive closure ∪ semantic sweep
  looped until twice-dry; ~465 rate units on the benchmark token ≈ ¼ of
  its daily quota), raw `get_document` payloads (created_at, owner,
  review cadence — fields the neutral RagProvider types deliberately
  drop; the script uses `PulpoProvider.raw_tool_call`), HTML build
  (`<a name>` anchors — the ONE form Drive's importer converts to real
  bookmarks; `id=` variants convert to dead links), delivery via Drive
  `files.update` + HTML conversion, and a docx-export verification that
  every internal link resolves. `diff_corpora`/`diff_resume` produce
  the change résumé.
- **QA dashboard SSE**: `event_bus.py` (in-memory, per-team fanout) →
  `GET /api/{team_id}/events/stream` (`routes/events.py`) → inline JS in
  `frontend/team_dashboard.html` (`openEventStream()`, per-event-name
  listeners). ONE event exists today (`eval_approved`); the toast stack
  is hardcoded to its shape.
- **CC chiclets**: schema only. `command_center.chiclets` +
  `chiclet_events` (migration 005, constraint-tested) with a `type`
  CHECK that does NOT include a compendium type. No chiclet runtime, no
  `cc_event_bus`, no ack endpoint, no CC frontend exists anywhere.
  NOTE: the `command_center` Python package does not import in prod
  (Railway root is `qa-automation/AI-Scoring`), but the TABLES are
  reachable — `backend/services/cc_context.py` already reads
  `command_center.*` through the shared pool. We follow that pattern.
- **Scheduling**: `disposition_pull.run_periodic_pull` — env-gated
  asyncio task spawned in the FastAPI lifespan. Fixed-interval only;
  no clock-aligned cron exists yet. `America/Mexico_City` (no DST) is
  already the house TZ constant in three services.

## 2. Principles

1. **The Doc is the artifact; the chiclet is the receipt.** The weekly
   job never touches the scoring path and never blocks it — failures
   log and wait for next Monday (non-fatal doctrine, cc_context-style).
2. **Write the real ledger.** Chiclet rows go to
   `command_center.chiclets` (type CHECK widened by migration), events
   to `chiclet_events`, SSE payloads follow the LandingOpsCommandCenter
   §6 contract verbatim — published on the QA team bus only until the
   designed `/cc/{team}/events` stream exists, at which point the
   publish site moves and history is already in place.
3. **Ack is a duty gate, §4.3a style.** `acknowledged: true` in the
   request or 422. One manager ack resolves the chiclet team-wide
   (chiclets are team-scoped rows; CC doctrine §4).
4. **Quota citizenship.** Benchmark token, Monday 05:00 (quietest
   window), the script's existing pacing. Steady-state cost is known:
   ~465 units against 2,000/day.
5. **Env-gated, off by default** (the `CC_STATS_PULL_INTERVAL_MIN`
   pattern): unset `PULPO_COMPENDIUM_WEEKLY` = feature absent. Flip is
   a Railway env change, no deploy.

## 3. Design

### 3.1 Migration 019

- `command_center.chiclets`: widen `chiclets_type_check` to add
  **`compendium_update`** (the 018 pattern — drop + re-add CHECK).
- **`qa.pulpo_compendium_snapshots`**: `id IDENTITY, taken_at
  TIMESTAMPTZ NOT NULL DEFAULT NOW(), doc_count INT NOT NULL, doc_url
  TEXT NOT NULL, summary JSONB NOT NULL` — `summary` is the lean
  per-doc census `[{id, title, updated_at, open_flag_count, status,
  tags}]` (~40 KB/week, permanent retention), the diff base for the
  next run AND the missed-run watermark. The full corpus JSON is not
  stored server-side: the Google Doc is the artifact.

### 3.2 `backend/services/pulpo_compendium.py` (stages move in-repo)

The script's stage functions (`extract_corpus`, `merged_entries`,
`diff_corpora`, `diff_resume`, `build_html`, `deliver_html`) move here;
`scripts/pulpo_compendium_export.py` becomes a thin CLI over the same
module (single source of truth; existing tests re-point). New:
`snapshot_summary(corpus)` (census for the table) and
`run_snapshot(pool)` orchestrating: extract → load last summary → diff
→ build → deliver (verify) → insert snapshot row → chiclet write +
SSE publish per team. Delivery-verification failure aborts BEFORE the
snapshot insert, so the next successful run diffs against the last
GOOD delivery.

### 3.3 Weekly scheduler — `run_weekly()` lifespan task

- Clock-aligned, not interval: sleep until next Monday 05:00
  `America/Mexico_City` (zoneinfo; no-DST TZ makes the arithmetic
  boring — still computed via zoneinfo, never a fixed offset).
- **Missed-run catch-up**: on startup, if the latest
  `pulpo_compendium_snapshots.taken_at` is > 8 days old (or the table
  is empty and the env gate is on), run once immediately — Railway
  restarts and deploy timing must not silently skip a week.
- Spawned/cancelled in `main.py` lifespan exactly like the disposition
  pull; gated on `PULPO_COMPENDIUM_WEEKLY=1` AND
  `PULPO_COMPENDIUM_DOC_ID` present.

### 3.4 Chiclet write + SSE

Per team in `PULPO_COMPENDIUM_NOTIFY_TEAMS` (comma list, default
`member_support`):

- INSERT `command_center.chiclets`: `type='compendium_update'`,
  `tier='T2'`, `status='active'`, `summary=<diff_resume string>`,
  `data={doc_url, diff:{added,removed,updated,flag_changes,total_docs},
  stats:{rate_units, fetched}}`.
- INSERT `chiclet_events` (`event_type='created'`, payload = SSE data).
- `get_event_bus().publish(team_id, "chiclet_created", {"id", "tier": 2,
  "type": "compendium_update", "chiclet": {"title": "Pulpo Compendium
  updated", "summary": <résumé>, "doc_url": …, "created_at": …}})` —
  the §6 CC contract shape (integer tier in SSE, `T2` string in SQL,
  matching the doc's own convention).
- First-ever run (no previous snapshot): résumé reads
  `"first snapshot: N docs"` — no fake diff against an empty corpus.

### 3.5 Routes — `backend/routes/chiclets.py` (mounted `/api/{team_id}`, team auth)

- `GET /chiclets?status=active` → team's chiclets (dashboard boot
  re-render; SSE alone is not durable — the bus drops on full queues).
- `POST /chiclets/{id}/ack` body `{acknowledged: true, evaluator_email}`
  → 422 unless `acknowledged is True` (§4.3a gate verbatim); UPDATE
  `status='resolved', resolved_at=NOW(), resolved_by=<email>` (the 005
  resolved-pair CHECK enforces completeness); INSERT `chiclet_events`
  (`'resolved'`); publish `chiclet_resolved` `{"id", "resolved_by":
  "manual_ack"}`. Idempotent: acking an already-resolved chiclet
  returns 200 with current state. 404 on team mismatch.

### 3.6 Dashboard UI (`frontend/team_dashboard.html` only)

- Generalize `showToast` to also accept a system shape
  `{title, summary, href}` (existing eval shape untouched).
- New SSE listeners: `chiclet_created` → render pinned card + toast
  ("Pulpo Compendium updated" + résumé); `chiclet_resolved` → remove
  card (any manager's ack clears every open dashboard).
- Pinned **T2 card**: amber left-border (CC §4 tier vocabulary), fixed
  above the toast container; title, résumé, `[Open compendium]`
  (doc_url, new tab), `[Acknowledge]` → POST ack with
  `evaluator_email` prompted once and cached in
  `sessionStorage['qa_evaluator_email']` (scorecard-page convention).
  NOT auto-dismissed — it outlives reloads via the boot fetch.
- Boot: after `openEventStream()`, `GET /chiclets?status=active` →
  render pending cards.

### 3.7 Env (+ `.env.example`, Railway)

```
PULPO_COMPENDIUM_WEEKLY=1          # gate; unset = feature off
PULPO_COMPENDIUM_DOC_ID=…          # existing user-owned Doc (SA editor)
PULPO_COMPENDIUM_NOTIFY_TEAMS=member_support
PULPO_MCP_BENCHMARK_TOKEN=…        # already provisioned
```

## 4. Failure semantics

| Failure | Behavior |
|---|---|
| extract/deliver error (incl. link-verification) | log loudly; NO snapshot row, NO chiclet; next Monday (or restart catch-up) retries |
| chiclet insert fails after delivery | log; Doc is fresh, receipt missing — next run's diff is unaffected (snapshot row committed) |
| SSE publish fails / queue full | non-fatal; the chiclet ROW is the truth, boot fetch renders it |
| backend down at Monday 05:00 | §3.3 catch-up on next startup |

## 5. Rollout

`unset` (today) → set the three env vars in Railway → next Monday runs
live; or set locally + run the CLI once (`--corpus` reuse for dry
runs). Watch-paths note: all changes live under
`qa-automation/AI-Scoring/`, so merges auto-deploy.

## 6. Open questions (owner)

1. **Notify teams** — `member_support` only at launch, Sales later?
2. **Ack scope** — one manager's ack resolves team-wide (CC doctrine;
   recommended). Alternative (per-manager acks) needs a table 005
   doesn't have.
3. **Ledger** — writing `command_center.chiclets` via CHECK-widening
   (recommended: first real writer of the scoped CC data model) vs. a
   QA-side table (rejected: duplicate vocabulary, orphaned history when
   the CC rail ships).
4. **Catch-up window** — run-on-startup when the last snapshot is > 8
   days old (recommended) vs. strictly-Monday-only.
5. **Ack identity** — prompted + cached `evaluator_email` (scorecard
   convention) vs. no identity (plain click). §4.3a spirit says named.

## 7. Build plan (each slice one PR, pytest-gated)

| # | Slice | Checkpoint |
|---|---|---|
| C1 | Migration 019 (+down) | integration tests: widened type CHECK accepts `compendium_update`, snapshots-table constraints |
| C2 | Stages → `backend/services/pulpo_compendium.py`; script becomes thin CLI | existing `test_pulpo_compendium.py` re-pointed and green; CLI smoke (`--corpus … --skip-deliver`) |
| C3 | `run_weekly` scheduler + `run_snapshot` wiring (env-gated) | unit: next-Monday-05:00 computation, catch-up predicate; job test with mocked provider + Drive |
| C4 | Chiclet write + routes + SSE publish | route tests: ack gate 422, resolved-pair invariant, team scoping, idempotent re-ack, `chiclet_created`/`chiclet_resolved` on the bus |
| C5 | Dashboard card + generalized toast + boot fetch | manual smoke on `/dashboard/member_support`; SSE contract pinned by C4 tests |

C1–C2 are risk-free (no runtime change); C3 is inert until the env
gate flips; C4–C5 land the visible feature.
