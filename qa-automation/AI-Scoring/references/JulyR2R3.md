# July R2/R3 — EOM CSV Exports & Assessment Persistence

> Design for roadmap item 5 (tasks #12/#13 of the MS Director's July
> rollout): R2 = end-of-month CSV exports with the sampling counter;
> R3 = `qa.assessments` persistence (the Wave-2 write-ban is lifted per
> the rollout directive) + the one-pager's CSV→DB source swap. Both ride
> the completed read-path flip: every data read here sources `qa.*`
> through the proven `team_source` row-source — no sheet reads.

| | |
|---|---|
| **Status** | v1 — 2026-07-19 |
| **Depends on** | ReadPathFlip complete (#108–#116); migration 011 (`qa.assessments`, shipped empty); `scripts/export_onepager.py` mockup (PR #93-era); identity repair (#114) — `qa.assessments.agent_id` is NOT NULL |
| **Out of scope** | coaching/tags UI (LateStageDesign Tier 3); webhook sampler (August); Sales onboarding to exports (config-gated, same code) |

---

## 1. R2 — EOM CSV exports + sampling counter

One operator-run script, B-series conventions:

```
python3 scripts/export_eom_csvs.py --team member_support --month 2026-07 \
    [--target 20] [--out-dir ../../database/eom_exports]
```

**Source:** `team_source.fetch_history_frame` filtered to the month via
`_months_in_bucket_tz` — the EXACT bucket-TZ semantics of the dashboard
chiclets/drill-down, so the CSV row count always equals the month
drill-down count. No new query shapes.

**Outputs** (UTF-8-BOM CSVs so Excel opens them clean, under
`<out-dir>/<team>/<YYYY-MM>/`):

1. `team_summary.csv` — one row per agent:
   `agent, supervisor, evals, sampling_target, pct_of_target, mean,
   std, min, max, <numeric section means...>, <yn section pcts...>`.
   Sorted by mean desc (roster convention). Includes ONLY active-roster
   agents by default (`--include-departed` to widen) — matches the
   dashboard's default lens.
2. `agent_<slug>.csv` — one per agent, one row per eval:
   `call_date (bucket TZ), overall_score, <per-section scores...>,
   evaluator_email, dialpad_link`.
3. `run_report.json` — counts, month, target, source row totals
   (staging-report convention).

**Sampling counter** = `evals` vs `--target` (default 20; the rollout
band is 20–25/agent/month). A CLI arg, not config: the target is July
rollout policy, likely obsolete by the August webhook sampler —
config-schema churn isn't warranted.

**Destination:** local files (gitignored dir). The Director receives
files; no Sheet/email automation in R2.

## 2. R3 — qa.assessments persistence

**Writer location:** `progression_service.get_progression` — persist
every FRESH generation (cache hits don't re-persist; identical windows
re-served from memory don't duplicate rows). This is what migration 011
was designed for: append-only rows, the `is_current` flip on the prior
(agent, window) row being the only permitted mutation. Dashboard cards
and EOM runs share one code path and one audit trail, and
`estimated_cost_usd` accrues per real Gemini call.

**Never persisted:** the two placeholder results (no-evaluations, JSON
parse failure). They are error text, not AI output — persisting them
would violate the §Q4.a "pure unadulterated AI output" rule and flip
`is_current` off a valid predecessor in favor of garbage.

**Identity:** resolve `agent_id` from `qa.agents` (active roster,
case-insensitive name/canonical match — the Stage-4 semantics). No
match → generate as today but SKIP persistence with a log line
(departed/unknown agents keep working dashboards; nothing silently
breaks). Post-#114/#115 the roster is current, so misses should be rare.

**Stamps:** `rubric_version`/`formula_version` from
`score_compute.get_active_versions` (same source finalize uses);
`models_used` mirrors the eval-row shape
(`{"text": {"provider": "gemini", "model": <progression_model>}}`);
`estimated_cost_usd` from response usage metadata when available, NULL
otherwise. `qa.assessment_sections` snapshots section_name/number at
generation time (migration 011 Q3.a).

**Failure posture:** persistence failures are logged and swallowed — the
dashboard card must render even if the DB write fails (same §7.3 spirit
as the old dual-write). The EOM script, by contrast, treats a persist
failure as a hard error (its whole point is the durable row).

## 3. One-pager DB swap

`export_onepager.py` drops `--csv` for `--team/--month/--agent`:

- Rows from `fetch_history_frame` (same month filter as R2) — the
  layout's existing per-section/trend/sparkline logic keeps working on
  the frame's columns.
- The reserved AI-Assessment slot fills from the CURRENT
  `qa.assessments` row for (agent, month window) — generated via the
  R3 writer if absent (`--no-generate` to skip, e.g. for cost-free
  re-renders).
- Month window = calendar month in bucket TZ (`range_start_at`/
  `range_end_at` stamped accordingly; `time_range_days` = days in
  month) — distinct from the dashboard's rolling `days=30`, disjoint by
  design, distinguished naturally by the window columns.

## 4. Slices

| # | Slice | Checkpoint |
|---|---|---|
| R3a | assessment writer (`assessment_store.py`: resolve identity, insert + sections, is_current flip, stamps) wired into `get_progression` | pytest: fresh-generation persists, cache-hit doesn't, placeholders never persist, unknown agent skips, is_current uniqueness |
| R2 | `export_eom_csvs.py` + pure summarization helpers | pytest on summarize-from-frame (synthetic frames); prod dry-run row counts == month drill-down |
| R3b | one-pager swap + EOM assessment generation | render MS 2026-06 + 2026-07 against prod read-only; layout unchanged vs mockup |

Single PR; slices are commits.
