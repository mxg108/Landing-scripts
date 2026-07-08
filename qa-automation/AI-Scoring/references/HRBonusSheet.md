# HR Bonus Sheet — Production Spec

> Production specification for the HR-facing Agent Bonus workbook. The layout was frozen by the
> mockup exporter (`scripts/export_hr_bonus_sheet.py`, PR #93) and **approved by HR on
> 2026-07-07**. This spec turns that mockup into the monthly production surface: a FastAPI
> endpoint that computes everything from Postgres, rendered into the workbook by a small GAS
> suite on a monthly trigger.

| | |
|---|---|
| **Status** | v1 — owner decisions locked 2026-07-07 |
| **Consumer** | Landing HR (bonus input), read-only |
| **Team scope** | Member Support only at launch; Sales is a config-driven extension (§8) |
| **Workbook** | the Sheet at env `GOOGLE_SHEETS_QA_BONUS_ID` |
| **Cadence** | 1st of each month, exporting the *prior* month |
| **Depends on** | qa.* read path (evaluations finalized by the engine); PR #94's `qa`-schema fixes |

**Owner decisions (2026-07-07):**

1. **Data flow: backend endpoint + GAS render.** The backend computes every number; GAS does
   zero math — it fetches JSON and writes cells. DB credentials never leave Railway; all
   aggregation logic is pytest-testable.
2. **Top-up rule dropped.** The mockup's top-up-to-20 padding (prior-month rows pulled in below
   the floor) does not ship. A monthly run exports exactly the target month's evaluations;
   sparse agents show fewer rows.
3. **Member Support only at launch.** The Sales section subset needs an HR/Sales decision that
   hasn't been made (§8).

---

## 1. Purpose & scope

**In scope**
- The workbook layout (frozen by the approved mockup, deltas in §2a).
- The backend endpoint: route, auth, data semantics, response contract (§4–§5).
- The GAS suite: rendering, trigger, failure handling, config (§6).
- The `hr_export` team-config block and its invariants (§3, §7).

**Out of scope / non-goals**
- Bonus math itself — HR computes bonuses from these numbers; this surface only reports.
- Historical backfill of pre-cutover months (the endpoint accepts any month; runs against
  months that predate the qa.* seed simply return what the backfill has landed).
- Sales at launch (§8), styling beyond the mockup (frozen header row only).

---

## 2. Workbook layout (frozen 2026-07-07)

### Summary tab — one per month

- **Tab title:** month label `"June 2026"` (`%B %Y`), inserted at index 0 (newest month first).
- **Columns:** `Agent Name | Agent Email | Monthly Avg |` the 9 HR-visible sections (§3 order).
- One row per agent that has ≥1 finalized evaluation in the month, sorted case-insensitively
  by agent name.
- **Monthly Avg** = mean of the agent's per-evaluation overall scores, 1 decimal.
- **Numeric sections** = mean of that section's scores across the agent's month, 2 decimals.
- **Binary sections** = `%Yes` as an integer percent (`"85%"`); N/A excluded from the
  denominator (`Yes / (Yes + No)`).
- Cell is **blank** (never `0`, never `NaN`) when the agent has no data for that column.
- Header row frozen.

### Detail tab — one per agent

- **Tab title:** the agent's name (roster `name` from `qa.agents`).
- **Columns:** `Period | Date | Overall Score |` the 9 sections (§3 order) `| Evaluator | Dialpad Link`.
- One row per finalized evaluation in the target month, **newest first**.
- `Period` = `YYYY-MM` of the evaluation (kept for layout continuity with the approved mockup —
  with the top-up rule dropped it always equals the target month).
- `Date` = call time, `MM/DD/YYYY HH:MM`, rendered in project-local time (§4).
- Section cells: numeric scores as digits; binary as `Yes` / `No` / `N/A`.
- `Evaluator` = `evaluator_email`; `Dialpad Link` = the evaluation's `dialpad_link`.
- Header row frozen.

### Re-run semantics

Idempotent: an existing tab is **cleared and rewritten in place** (never deleted — preserves
tab ids and any external links). Re-running a month is always safe.

### 2a. Deltas from the approved mockup

| Mockup behavior | Production |
|---|---|
| Top-up-to-20 from prior months (unbounded) | **Dropped** — target month only (owner decision 2) |
| `EXCLUDED_AGENTS` hardcoded in the script | `hr_export.excluded_agents` in team config (§3) |
| Reads an Analyst_History-shaped CSV | Backend queries `qa.evaluations` / `qa.evaluation_sections` |
| Section list hardcoded as CSV column names | `hr_export.sections` in team config, keyed by section id (§3) |
| Manual invocation | Monthly GAS trigger + manual re-run menu (§6) |

---

## 3. HR-visible sections (`hr_export` config block)

The export list lives in the team config JSON — **not** in code — as a new `hr_export` block:

```json
"hr_export": {
  "sections": [
    {"id": "greeting",          "hr_label": "Greeting"},
    {"id": "caller_id",         "hr_label": "Caller ID"},
    {"id": "purpose",           "hr_label": "Purpose of the call"},
    {"id": "matching",          "hr_label": "Matching the moment"},
    {"id": "process_adherence", "hr_label": "Process Adherence"},
    {"id": "call_resolution",   "hr_label": "Call Resolution"},
    {"id": "comms",             "hr_label": "Communication"},
    {"id": "efficiency",        "hr_label": "Efficiency & Call Handling"},
    {"id": "cri",               "hr_label": "CRI"}
  ],
  "excluded_agents": ["maximiliano perez", "maximiliano.perez"]
}
```

- `id` references the rubric section id; score type (numeric vs binary aggregation) is derived
  from the rubric — never restated here.
- **`human_review_required` is internal-only and must never be exported.** The exclusion is
  structural (absent from the list), and additionally enforced: a Pydantic validator on
  `hr_export` rejects any section whose id is in the team's internal-only set, with a unit
  test asserting the invariant on every shipped config. This is the "Documentation / HRR never
  leaves the building" guarantee from the mockup, made mechanical.
- `excluded_agents` is matched case-insensitively against the raw agent name (mockup parity);
  applied **in the backend**, so no consumer of the endpoint can see excluded agents.

---

## 4. Data semantics

All numbers are computed by the backend from Postgres. Definitions:

- **Population:** `qa.evaluations` with `team_id = 'member_support'`, `state = 'finalized'` —
  the same membership rule as Analyst_History (Stage 4 writes on finalize) and the same rule
  the `PostgresProvider` read path uses (PR #94).
- **Evaluation timestamp:** `COALESCE(call_connected_at, created_at)` — call time, matching
  the col-C semantics of the sheet data HR approved.
- **Month bucketing:** project-local time (`America/Los_Angeles`), consistent with the
  dashboard analytics `BUCKET_TZ`. A call at 03:33 UTC on June 1 is a **May** call.
- **Section values:** from `qa.evaluation_sections` by section id; numeric → `numeric_score`,
  binary → `binary_value` (`Y`/`N`/`NA` rendered `Yes`/`No`/`N/A`).
- **Aggregates:** as defined in §2 (means, %Yes with NA-free denominator, blank-when-empty).
- Rounding happens **once, at the endpoint** — GAS writes strings/numbers verbatim.

---

## 5. Backend endpoint

```
GET /hr-bonus/{team_id}/{month}          month = YYYY-MM
```

- **Auth:** existing API-key middleware; a dedicated read-only reporting key for the GAS suite
  (same key-role machinery as the other GAS↔backend calls). Requests land in
  `qa.api_audit_log` like every other endpoint.
- **Validation:** unknown `team_id` → 404; malformed month → 422; a team without an
  `hr_export` block → 404 (this is how Sales stays dark until §8 is decided).
- **Response** (shape the GAS renderer consumes 1:1):

```json
{
  "team_id": "member_support",
  "month": "2026-06",
  "month_label": "June 2026",
  "generated_at": "2026-07-01T13:00:00Z",
  "section_labels": ["Greeting", "Caller ID", "…"],
  "agents": [
    {
      "name": "Jane Doe",
      "email": "jane@landing.com",
      "monthly_avg": 87.5,
      "section_summaries": ["4.20", "85%", "…"],
      "evaluations": [
        {
          "period": "2026-06",
          "date": "06/28/2026 14:05",
          "overall_score": 91.0,
          "sections": ["4", "Yes", "…"],
          "evaluator": "boss@landing.com",
          "dialpad_link": "https://dialpad.com/…"
        }
      ]
    }
  ]
}
```

- `section_summaries` / `sections` are **positionally aligned** with `section_labels` and
  pre-rendered as display strings (blank for no-data) so the GAS layer stays math-free.
- Agents sorted case-insensitively; evaluations newest-first — the renderer never sorts.

Implementation: `backend/services/hr_bonus_service.py` (pure query + aggregation, unit-tested)
+ a thin route in `backend/routes/`.

---

## 6. GAS suite

New top-level GAS project `qa-automation/hr-bonus/`, built and pushed through the existing
pipeline (`push.sh` target + `scripts/build_config.py` emitting its `Config.js`). It reuses the
established patterns from `src/` (config-driven, render-only) but is deliberately tiny:

| File | Responsibility |
|---|---|
| `Main.js` | `runMonthlyExport()` — trigger entry; computes prior month, orchestrates; `runExportFor(month)` for manual re-runs (custom menu) |
| `ApiClient.js` | `UrlFetchApp` GET to the backend with the API key from Script Properties; JSON parse + error surfacing |
| `SheetWriter.js` | Summary + detail tab rendering per §2 (clear-in-place, freeze header, index 0 for summary) |
| `Config.js` | generated — backend base URL, team id |

- **Script Properties:** `HR_BONUS_API_KEY` (the reporting key), `HR_BONUS_SPREADSHEET_ID`
  (the workbook — GAS opens by id; the suite is not container-bound so the workbook can be
  swapped without redeploying).
- **Trigger:** time-driven, **1st of each month, 06:00–07:00 America/Los_Angeles**, exporting
  the month that just ended.
- **Failure handling:** any error → `MailApp` alert to the operator with the error and month;
  no partial-write cleanup needed because re-runs are idempotent (§2). No retry loop in v1 —
  the operator re-runs from the menu.
- **Write pacing:** `SpreadsheetApp` batched `setValues` per tab (one write per tab — the
  Sheets-API-quota pacing that shaped the mockup's `_paced` writer doesn't apply to GAS).

---

## 7. Build plan (pytest checkpoints per [[feedback_design_doc_first]])

| # | Deliverable | Checkpoint |
|---|---|---|
| P1 | `hr_export` block in `member_support.json` + Pydantic model + validator | tests: block parses; internal-only rejection fires; shipped MS config exports exactly the 9 §3 sections; `human_review_required` invariant |
| P2 | `hr_bonus_service.py` + route | tests: aggregation math (means/%Yes/blanks/rounding), month bucketing incl. the DST boundary case, finalized-only population, excluded-agents filter; **golden parity test** — service output vs the mockup exporter run on the same fixture data |
| P3 | `hr-bonus/` GAS suite + build pipeline wiring | manual smoke into a copy of the workbook; layout diffed against a mockup-produced tab |
| P4 | Trigger install + first supervised run | 2026-08-01 run for July data, operator watching; alert path verified |

P2's golden parity test is the load-bearing one: it proves the DB-sourced numbers match what
HR approved before the mockup CSV path is retired.

---

## 8. Sales extension (deferred)

Everything is config-driven, so Sales onboarding is: decide the HR-visible subset of the 15
`sales_v2` sections with HR + Sales Management, add the `hr_export` block to `sales.json`, and
point a second trigger (or the same suite with both team ids) at a Sales workbook. No code
changes expected beyond config. Blocked on that section-subset decision — deliberately **not**
made in this spec.

---

## 9. Open questions

None blocking. Two conscious defaults, flagged for visibility:

1. Agent detail tabs accumulate across months (a tab holds only the latest exported month's
   rows after its clear-and-rewrite). If HR wants per-month history preserved inside detail
   tabs, that's a layout change to renegotiate — the approved mockup rewrites in place.
2. `excluded_agents` matches on raw name (mockup parity). If roster names ever collide with an
   excluded name, switch the match to `qa.agents.email`.
