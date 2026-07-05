# Cutover Design — Postgres-Owned Scoring, Sheets as Projection

> Wave2Plan Phase 7a groundwork. **The contract (owner directive, 2026-07-04):** once a team
> flips, Sheets writes are *projections of the DB row* — finalized evaluations land in
> Analyst_History straight from Postgres, the sheet ARRAYFORMULA never scores anything again, and
> **data never flows Sheets → Postgres**. One direction: DB → Sheets.

| | |
|---|---|
| **Status** | Design for review — no code in this PR |
| **Scope** | The write-path flip (scoring + projection). Read-path flip (dashboards → DB) is 7a's second half, staged separately |
| **Mechanism** | Per-team truth flag; MS flips first, Sales stays pre-cutover until its rubric/pipeline migration |
| **Last updated** | 2026-07-04 |

---

## 1. Current state (post-#80) — and the flows that must die

| Stage | Sheets effect | Postgres effect (dual-write) | Direction violations post-cutover |
|---|---|---|---|
| 1 draft | FR-AI row | `INSERT state='draft'` + sections | — |
| 1.5–2 approve | FR-AI edits → copy to score destination tab | *(deferred to one recorder)* | Stage 2 **reads the FR-AI row** to build the destination row |
| 3 readback | Poll destination ARRAYFORMULA → FR-AI col F | `overall_score` ← **sheet value** | ⚠️ the readback IS a Sheets→Postgres flow — dies entirely |
| 4 finalize | Analyst_History append (agent_email via **Mails tab lookup**) | `state='finalized'` | ⚠️ Mails lookup is a second, quieter Sheets→Postgres flow (agent_email) |
| 5 email | Apps Script keyed on the **Analyst_History row** | — | — (survives unchanged: it reads the projection) |

Two identity facts shape the design: the GAS email pipeline (Stage 5) is keyed on Analyst_History,
not the destination tab — so retiring the destination write doesn't break email. And dashboards /
team_stats read Analyst_History via history_service — so a faithful Analyst_History projection
keeps every existing read surface working before the read-path flip.

## 2. Target state (team flipped)

```
score → Stage 1: INSERT draft (DB)            → project FR-AI row        (DB → sheet)
approve → Stage 2: UPDATE sections/evaluator  → compute_overall_score()  (engine, §3.6)
                   stamp formula_version + rubric_version (get_active_versions)
                   §3.14 trigger check → auto-finalize OR pause flagged_human_review
          Stage 3: DELETED (no readback — the engine already produced the number)
          Stage 4: state='finalized' (DB)     → project Analyst_History row (DB → sheet)
                                              → trigger Apps Script (unchanged)
```

- **Postgres row first, projections second.** A projection failure is retryable (the DB row is
  truth); a DB failure is a hard request error — §7.3 Phase C.
- **agent_email** comes from `qa.agents` (B3 identity resolution), never the Mails tab. Until B3
  lands, a flipped team projects a blank col B exactly like today's Stage-4-miss behavior; the
  Mails *tab* becomes an import source for `qa.agents` (one direction: sheet → one-time import
  script → DB — an explicit, operator-run exception, not a runtime flow).
- **Destination tab / weights tab:** dead for scoring. Optionally keep a metadata projection into
  Form Responses 1 during a transition month for analyst muscle memory, but nothing reads it.
- **Score number:** `NUMERIC(5,1)` from the engine — dashboards show one decimal where the sheet
  showed integers. Cosmetic, but agents will notice; socialize with the formula change.

## 3. The refactor — `sheets_projection.py`

New module with one job and one rule: **render sheet rows from a DB evaluation; never read cell
values to construct DB state.**

```python
async def project_evaluation(evaluation_id, conn, config) -> ProjectionReport
    # loads the row + sections, renders FR-AI + Analyst_History payloads,
    # batch-writes them; idempotent on dialpad_link (same overwrite key as today)
def build_fr_ai_row(evaluation, sections, config) -> list[str]
def build_history_row(evaluation, sections, config) -> list[str]
```

- The row-rendering logic moves out of `write_draft_to_fr_ai` / `finalize_to_analyst_history`
  nearly verbatim — the change is the *input* (DB row, not scorecard-in-flight or sheet reads).
- `sheets_service.py` keeps: auth/plumbing helpers, Score_Audit append, `trigger_apps_script`,
  and the pre-cutover stage functions (Sales still runs them until its own flip).
- Deleted at MS flip, removed at repo level once Sales flips: `read_score_and_writeback`,
  `write_to_score_destination`'s FR-AI read, `_lookup_agent_email` as a runtime dependency.
- Projection failures: logged + queued for retry (a nightly `reproject` sweep re-renders any
  evaluation whose `finalized_at` > last successful projection — cheap, idempotent).

## 4. The truth flag

`public.teams.scoring_owner TEXT NOT NULL DEFAULT 'sheets'` (`'sheets' | 'postgres'`) — migration
013. Operational config on the team row per SQLMigration §6; flipping is a one-line UPDATE, no
deploy, and rollback is the same line backwards. `get_team_config` surfaces it (DB read with
JSON/env fallback for local dev).

Branch points keyed on the flag:

| Site | `sheets` (today) | `postgres` |
|---|---|---|
| approve endpoint | Stages 1.5–4 sheet flow + `record_approval` (swallowed) | DB transition + engine compute + projections; **DB errors raise** |
| eval_store failure mode | log + swallow (§7.3 A/B) | raise (§7.3 C) |
| Stage 3 | poll ARRAYFORMULA | skipped |
| overall_score provenance | sheet readback, versions NULL | engine, versions stamped |

## 5. Prerequisites and sequencing

1. **4e — §3.14 human-review trigger (BLOCKING).** Post-cutover Stage 2 must decide
   auto-finalize vs. pause (`scoring_status='flagged_human_review'`) from the v3
   `human_review_triggers` (ratings ≤ 2). Small PR, ships before the flip.
2. **4c — `qa.agent_stat_points` seed on finalize (non-blocking).** Wanted before the read-path
   flip (7a dashboards), not before the write flip.
3. **Migration 013** — `scoring_owner` column (+ optionally the §3.4.3 CHECK tightening deferred
   from 006's comments, per team, once flipped).
4. **Flip MS.** Sales flips only after its 15-section rubric/pipeline migration (task #5) — the
   flag makes the asymmetry a non-event.
5. Backfill (Phases B/5-6) stays last per owner sequencing; cutover does not depend on it. The
   ε-sweep and bonus endpoint (7b) want the backfill, so July bonus math runs on
   cutover-forward rows until it lands.

## 6. Verification gate + rollback

**There is no numeric parity gate against the sheet — by design.** The sheet still runs the
legacy twelfths formula; v3 deltas are the point (BackfillPlan §2). Correctness rests on the
already-shipped armor: 96 golden fixtures across both teams proving the engine reproduces real
history exactly under the archived formulas, plus the §3.19.3 archive cross-checks.

The pre-flip gate is therefore *mechanical*, one week of shadow on MS:

- At approve, compute the engine score alongside the readback (no persistence changes) and log
  `engine=X sheet=Y formula_version=Z` — verifies compute + version resolution + trigger checks
  fire cleanly on live traffic, not that X == Y.
- Zero swallowed dual-write failures over the window (the §7.3 flip makes any residual failure a
  user-facing error).

**Rollback:** flip the flag back. The ARRAYFORMULA and weights tab are left intact until Sales
also flips + one full bonus cycle passes; reverting restores the legacy pipeline wholesale. Rows
scored by the engine during the window keep their stamped versions (reproducible forever) — no
data cleanup needed on rollback.

## 7. PR slicing

1. 4e trigger + migration 013 (`scoring_owner`) + shadow logging — mergeable without behavior
   change (flag stays `sheets` everywhere).
2. `sheets_projection.py` + approve-endpoint branch on the flag + §7.3 Phase C error mode —
   inert until the flag flips.
3. Operator flip (MS) after the shadow week: `UPDATE public.teams SET scoring_owner='postgres'
   WHERE id='member_support';` + monitor.
4. Cleanup PR after both teams flip: delete Stage 3, destination write, Mails runtime lookup.
