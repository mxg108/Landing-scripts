# Read-Path Flip — Dashboards & Analytics from Postgres

> Design for roadmap item 4: every read surface (dashboards, drill-downs,
> datapoint pages, progression) sources from `qa.*` instead of the
> Analyst_History sheet. Unblocked 2026-07-12 by Backfill B0–B4 (1,985
> historic evaluations imported, enriched, identity-resolved, verified —
> BackfillPlan §7.4 all green). Writes are NOT in scope: Stage-1 FR-AI
> drafts, the Analyst_History projection (GAS email source), and
> Score_Audit keep working exactly as today.

| | |
|---|---|
| **Status** | v1 draft — 2026-07-12, from the frontend/backend read-path review |
| **Owner constraint honored** | flip only AFTER backfill (done) — flipping earlier would have blanked history |
| **Rollback** | flag flip back (reads only — no data is produced by this path) |
| **Depends on** | PR #94 `PostgresProvider`; backfill complete; `qa.agent_stat_points` seed (F1, this doc) |

---

## 1. The two read paths (finding)

The frontend consumes **two structurally different read paths**; only one
flips via the provider factory.

**Path 1 — provider-based.** `/agents`, `/agents/{name}/history`,
`/agents/{name}/progression`, `/datapoints`, `/datapoints/{call_id}` go
through the `DataProvider` ABC. `PostgresProvider` already implements the
full contract with tested `EvaluationRecord` parity (sections keyed by
history_id, naming bridges, YN_DISPLAY strings, tz-aware UTC) and serves
the Mails-shaped roster from `qa.agents`. Flip = factory wiring + pool
lifecycle (§5 F3).

**Path 2 — the `/team` analytics trio.** `/team/stats`, `/team/evals`,
`/team/long_form` bypass the provider: they call
`provider._ws.get_all_values()` (a gspread-only attribute) and feed raw
rows to `load_and_clean` + nine pandas `compute_*` functions. No factory
swap can flip these — they need a Postgres **row-source** (§3 W1).

**Keystone decision:** do NOT rewrite the nine battle-tested pandas
computations (EWMA, SPC, modified-Z outliers, roster, sections, binary,
supervisor, distribution, long-form) in SQL. Flip their *row source* and
keep the math byte-identical, provable by a golden-parity harness (§5
F2). Individual aggregations move into SQL later only where performance
says so (§5 F5) — correctness first, optimization second.

---

## 2. Frontend windows inventory (what the pages actually need)

From the full frontend review (2026-07-12). Endpoints → consumers:

| Endpoint | Consumers | Window semantics |
|---|---|---|
| `/team/stats` | KPI row, month chiclets, SPC, distribution, EWMA chart, roster, outliers, section analysis, binary tiles, supervisor tab | `days`/`date_from..date_to` (≤730d); **chiclets+SPC bucket by call date in `America/Los_Angeles`, independent of the days filter** |
| `/team/evals` | month drill-down page, Recent-Evals seed | single `year_month` bucket (call-date TZ bucketing); returns both call clock and `eval_approved_at` |
| `/team/long_form` | predictive-analytics groundwork (July 2026 Local-AI cutover consumer — do not clean up) | `days`/range; `coverage_regime` envelope |
| `/team/sections`, `/team/mails` | section metadata, supervisor dropdown | config / roster — no history read |
| `/agents/{name}/history` | agent page tiles + trend + section bars (client-side aggregation), EWMA drill-down, datapoint mini-history (`days=90` hardcoded) | `days` OR range |
| `/agents/{name}/progression` | AI assessment card | `days` only |
| `/datapoints` (`bin`/`agent`) | distribution drill-down | `days` only (custom range deliberately not honored today — preserve) |
| `/datapoints/{call_id}` | datapoint page | single eval by eval_id |
| `/events/stream` (SSE) | toasts, Recent-Evals live buffer | unaffected by the flip (event bus, not storage) |

Client-side-only aggregations (agent-page tiles/means, `/team/evals`
mean±std, last-5 trend arrows, Recent-Evals ring buffer + today count)
need **no server work** — they ride on the rows above.

---

## 3. Postgres windows to create

- **W1 — history row-source** (the `load_and_clean` equivalent; new
  `services/team_source.py`). Two queries per request, pandas assembles
  the same DataFrame the sheet produced:
  1. finalized evaluations ⋈ `qa.agents` (LEFT JOIN on `agent_id`):
     `COALESCE(a.canonical_name, e.agent_name_raw)` as agent, call clock,
     `approved_at`, `overall_score`, `evaluator_email`, `a.active` →
     `is_active` (NULL agent → inactive — departed-agent parity),
     `a.supervisor_email` → supervisor, eval_id (§4), dialpad_link;
  2. `qa.evaluation_sections` long for those evals (numeric_score /
     binary display) — pandas pivots to the per-section columns.
- **W2 — monthly buckets view** (`qa.v_monthly_scores`): month label via
  `call clock AT TIME ZONE <bucket_tz>` → count/mean/std per team.
  Feeds chiclets + SPC + `/team/evals` month filter. Also the biggest
  perf win: today every chiclet render loads the entire sheet.
- **W3 — `qa.v_history_long`**: evaluations × sections — `/team/long_form`
  is natively this shape; W1's query 2 is a parameterized slice of it.
- **W4 — `qa.agent_stat_points` seed** (the CutoverDesign 4c gap —
  CONFIRMED unimplemented: schema+indexes exist, zero writers, zero rows):
  - finalize-time writer: one EWMA/SPC point per finalize, computed by
    `team_stats.py` math (§9.2 — λ pinned per point);
  - backfill replay over all ~2,100 evaluations ordered by
    `approved_at` (SQLMigration §7.1 — EWMA is sequential);
  - **semantics wrinkle (decided):** stat points are the *all-time
    incremental* series for CC sparklines/PDF assessments; the
    dashboard's *window-scoped* EWMA stays computed over W1 rows. They
    are different numbers by design — neither impersonates the other.
- **W5 — roster** — already done: `PostgresProvider._get_mails_sheet()`
  serves `qa.agents` in Mails shape; `/team/mails`, supervisor dropdown,
  and `/lookup/scoring-permission` ride it after F3.
- **W6 — indexed eval-id lookup** for `/datapoints/{call_id}`: replaces
  today's linear scan over 365 days of history with an indexed
  `WHERE dialpad_entry_point_call_id = $1 OR dialpad_call_id = $1`
  (B2 populated both on every enrichable row) with dialpad_link-suffix
  fallback for pre-B2-junk rows. Ships in F5 (perf, not correctness).

---

## 4. Parity traps (each becomes a golden-parity test)

1. **Naive vs tz-aware timestamps.** `load_and_clean` deliberately emits
   naive bucket-TZ-comparable timestamps; the provider path emits aware
   UTC. W1 must preserve the naive semantics or month boundaries shift.
2. **eval_id derivation.** Everywhere (URLs, SSE dedupe, drill-down
   links) eval_id = trailing segment of the dialpad_link *text*.
   Superseded D2 re-eval rows have NULL links — W1 falls back to
   `dialpad_call_metadata.backfill.superseded_dialpad_link`, then to
   `dialpad_entry_point_call_id`. Rows with junk links (26 marked) parse
   to whatever the sheet had — same as today.
3. **Departed agents** (984 MS rows, `agent_id IS NULL`): must degrade
   exactly as Mails-absence does today — inactive, blank supervisor,
   raw name shown. The LEFT JOIN does this naturally; test it anyway.
4. **`active_only` and supervisor filters** must consult `qa.agents`
   with the same canonicalization (accent-stripped matching exists in
   load_and_clean's canonical_map — W1 keeps it in pandas).
5. **`coverage_regime`** stays hardcoded `manager_sample` until the
   Local-AI cutover (predictive-groundwork constraint — do not touch).
6. **Row-drop rules**: load_and_clean drops rows with unparseable
   overall/timestamp/blank agent/test-agent names — W1 filters
   equivalently (mostly moot post-B0, but the excluded_test_agents
   config filter must stay).
7. **`/datapoints` days-only window** (ignores custom range) is a
   frontend contract today — preserve, don't "fix" during the flip.

---

## 5. Slice plan (each PR-able; shadow-tested like the write flip)

| # | Slice | Checkpoint |
|---|---|---|
| F1 | `agent_stat_points` finalize writer + ordered backfill replay script (`backfill_stat_points.py`, B-series run-report conventions) | pytest: EWMA/λ pinning, replay ordering, idempotency (UNIQUE evaluation_id); replay report row-count == finalized evals |
| F2 | W1/W2/W3 row-source (`team_source.py`) feeding unchanged pandas + **golden-parity harness**: same request served from sheet and Postgres, JSON-diffed | parity green on /team/stats, /team/evals, /team/long_form across days/range/supervisor/active_only permutations, both teams |
| F3 | Path-1 factory flip: `get_provider()` returns connected `PostgresProvider` behind `QA_READ_PATH=postgres` env flag; pool lifecycle in main.py lifespan | existing provider-contract tests + route smoke; rollback = env flip |
| F4 | `/team` trio flipped behind the same flag after a shadow window (both sources computed, diffs logged, on live traffic) | zero diffs over the window; operator smoke: roster, chiclets, SPC, drill-downs, datapoint, SSE |
| F5 | Perf + cleanup: W6 indexed datapoint lookup, W2/W3 as real SQL views, delete the `_ws` back-door and sheet-read code from the trio (slice-5-style) | suite green; no gspread import in the /team read path |

Flag semantics: one env var (`QA_READ_PATH`, default `sheets`) — reads
are stateless so a redeploy-free per-team column isn't warranted; flip
and rollback are single Railway env changes.

## 6. Explicitly out of scope

- Any write-path change (FR-AI drafts, Analyst_History projection,
  Score_Audit tab, GAS email flow) — the sheet remains a projection
  target and the email source.
- Retiring the Analyst_History sheet itself (post-flip + one bonus
  cycle, per CutoverDesign §7's tab-retention rule).
- HR bonus endpoint (already reads qa.* — untouched).
- `/lookup` Dialpad passthroughs (no history reads except
  scoring-permission, which F3 covers via W5).
