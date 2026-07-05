# Cutover Design — Postgres-Owned Scoring, Sheets as Projection

> Wave2Plan Phase 7a groundwork. **The contract (owner directive, 2026-07-04):** once a team
> flips, Sheets writes are *projections of the DB row* — finalized evaluations land in
> Analyst_History straight from Postgres, the sheet ARRAYFORMULA never scores anything again, and
> **data never flows Sheets → Postgres**. One direction: DB → Sheets.

| | |
|---|---|
| **Status** | Design for review — no code in this PR |
| **Scope** | Write-path flip (scoring + projection) + GAS email-pipeline refactor + the lookup-route UX that serves as the first testing surface. Read-path flip (dashboards → DB) is 7a's second half, staged separately |
| **Teams** | **Both.** Sales Mgmt confirmed ready for the new formula (2026-07-04) — MS flips first only because Sales has engineering prerequisites (§6), not sign-off ones |
| **Last updated** | 2026-07-04 |

---

## 1. Current state (post-#80) — and the flows that must die

| Stage | Sheets effect | Postgres effect (dual-write) | Direction violations post-cutover |
|---|---|---|---|
| 1 draft | FR-AI row | `INSERT state='draft'` + sections | — |
| 1.5–2 approve | FR-AI edits → copy to score destination tab | *(deferred to one recorder)* | Stage 2 **reads the FR-AI row** to build the destination row |
| 3 readback | Poll destination ARRAYFORMULA → FR-AI col F | `overall_score` ← **sheet value** | ⚠️ the readback IS a Sheets→Postgres flow — dies entirely |
| 4 finalize | Analyst_History append (agent_email via **Mails tab lookup**) | `state='finalized'` | ⚠️ Mails lookup is a second, quieter Sheets→Postgres flow (agent_email) |
| 5 email | GAS pipeline keyed on the **Analyst_History row** | — | — (survives as a projection consumer; needs the §3b refactor to stay correct) |

Downstream consumers audited:

- **GAS email pipeline** (`qa-automation/src/` + per-team `Config.js`): reads *only* the
  Analyst_History row (`AnalystHistory.js`/`QAEntry.js` via `CONFIG.HISTORY_LAYOUT` +
  `NUMERIC_CATEGORIES`/`BINARY_CATEGORIES`). A faithful Analyst_History projection keeps it alive.
  `parseFloat` on the overall cell → engine decimals (87.5) render fine. **Found stale:**
  `teams/member_support/Config.js` still carries `documentation` keys/labels from before Phase 1a
  (layout width unchanged, so it renders — with the old section name in Gmail). Regeneration is
  part of this work (§3b).
- **Dashboards / team_stats** read Analyst_History via history_service — unaffected until the
  read-path flip.
- **Lookup → Score pipeline** (`/lookup/{team}` → `POST /score` → poll → "Open editor"): the
  first surface where cutover behavior becomes operator-visible; spec in §5.

## 2. Target state (team flipped)

```
score  → Stage 1: INSERT draft (DB) → project FR-AI row (DB → sheet)
         §3.14 trigger check on the draft (v3: process/resolution rating ≤ 2):
           ├─ CLEAN    → auto-pipeline: compute_overall_score() + stamp versions
           │             → state='finalized' → project Analyst_History → GAS email
           │             (no analyst touch; scorecard becomes read-only)
           └─ FLAGGED  → scoring_status='flagged_human_review', stays draft;
                         supervisor opens the editor, scores human_review_required,
                         approves → same compute/stamp/finalize/project path
Stage 3: DELETED (the engine already produced the number)
```

- **Auto-finalize is the headline behavior change**: clean calls complete end-to-end with zero
  analyst interaction. `/approve` becomes the *flagged-call resolution* endpoint (and the path
  for any pre-finalize edit); once finalized, the scorecard is immutable from the UI.
- **Postgres row first, projections second.** A projection failure is retryable (the DB row is
  truth); a DB failure is a hard request error — §7.3 Phase C.
- **agent_email** comes from `qa.agents` (B3 identity resolution), never the Mails tab. Until B3
  lands, a flipped team projects a blank col B (same as today's Stage-4-miss behavior); the Mails
  *tab* demotes to a one-time operator-run import into `qa.agents` — an explicit exception, not a
  runtime flow.
- **Destination tab / weights tab:** dead for scoring; nothing reads them post-flip.
- **Score number:** `NUMERIC(5,1)` from the engine — one decimal where the sheet showed integers.
  Cosmetic but visible; socialize together with the formula change.

## 3. The refactor

### 3a. `sheets_projection.py` (backend)

New module with one job and one rule: **render sheet rows from a DB evaluation; never read cell
values to construct DB state.**

```python
async def project_evaluation(evaluation_id, conn, config) -> ProjectionReport
    # loads the row + sections, renders FR-AI + Analyst_History payloads,
    # batch-writes them; idempotent on dialpad_link (same overwrite key as today)
def build_fr_ai_row(evaluation, sections, config) -> list[str]
def build_history_row(evaluation, sections, config) -> list[str]
```

- Row-rendering moves out of `write_draft_to_fr_ai` / `finalize_to_analyst_history` nearly
  verbatim — the change is the *input* (DB row, not scorecard-in-flight or sheet reads).
- `sheets_service.py` keeps: auth/plumbing, Score_Audit append, `trigger_apps_script`, and the
  pre-cutover stage functions until both teams flip.
- Deleted at repo level once both teams flip: `read_score_and_writeback`,
  `write_to_score_destination`'s FR-AI read, `_lookup_agent_email` as a runtime dependency.
- Projection failures: logged + retried by a nightly `reproject` sweep (re-render any evaluation
  whose `finalized_at` > last successful projection — cheap, idempotent).

### 3b. GAS email pipeline (same PR as 3a — owner requirement)

`qa-automation/src/` renders Gmail from Analyst_History + generated `Config.js`
(`scripts/build_config.py` → `teams/<team>/Config.js` → `./push.sh` → clasp). The projection
keeps the *data* flowing; this keeps the *rendering* correct:

- **Regenerate `teams/member_support/Config.js`** — it predates Phase 1a and still renders
  "Documentation" where the row now carries Human Review Required (ids/labels/rubric questions
  stale; layout positions unchanged). Verify `build_config.py` output against the current nested
  config, commit alongside.
- **Sales regen lands with its flip**: 19 → 15 sections changes `N_SECTIONS` and therefore every
  derived `HISTORY_LAYOUT` constant + both category arrays — `Config.js`, the Analyst_History /
  FR-AI tab layouts, and backend `HistoryLayout` all move together in the Sales-flip PR (§6).
- Audit `src/` renderers for assumptions beyond `CONFIG` (none found so far: overall score is
  `parseFloat`-safe for decimals; sections iterate the generated arrays).
- **Owner's share:** updating the GAS deployments (`./push.sh` per team + clasp deploy) after
  each regen merges — code PRs land the sources; the deploy is an operator step.

### 3c. Lookup + scorecard UX (§5 spec)

Frontend (`lookup.html` poll loop + scorecard page) and the job-status payload learn the
trigger outcome — see §5.

## 4. The truth flag

`public.teams.scoring_owner TEXT NOT NULL DEFAULT 'sheets'` (`'sheets' | 'postgres'`) — migration
013. Operational config on the team row per SQLMigration §6; flipping is a one-line UPDATE, no
deploy; rollback is the same line backwards. `get_team_config` surfaces it (DB read with JSON/env
fallback for local dev).

Branch points keyed on the flag:

| Site | `sheets` (today) | `postgres` |
|---|---|---|
| post-Stage-1 | job → `complete`, analyst approves manually | §3.14 trigger check → auto-finalize or pause |
| approve endpoint | Stages 1.5–4 sheet flow + `record_approval` (swallowed) | flagged-call resolution: DB transition + engine compute + projections; **DB errors raise** |
| eval_store failure mode | log + swallow (§7.3 A/B) | raise (§7.3 C) |
| Stage 3 | poll ARRAYFORMULA | skipped |
| overall_score provenance | sheet readback, versions NULL | engine, versions stamped |

## 5. Lookup route — expected behavior (the first testing surface)

Today: `Score Call` → `POST /api/{team}/score` → poll `/score/{job_id}` → on `complete`, the row
shows *"complete · Open editor"* → `/scorecard/{team}/{job_id}` (editable, analyst approves).

**Post-flip** (owner spec, 2026-07-04) — the poll payload gains the trigger outcome and the
button becomes the operator's signal:

| Outcome | Job payload | "Open Editor" button | Click behavior |
|---|---|---|---|
| **Flagged** (§3.14 fired: process/resolution rating ≤ 2) | `status='flagged_human_review'`, `state='draft'` | **Red** | Pop-up warning: *"This call cannot be automatically scored until you review it."* → editor opens **editable**; supervisor scores `human_review_required`, approves → engine finalizes |
| **Clean** (no trigger) | `status='complete'`, `state='finalized'`, `overall_score` present | **Green** (unchanged) | Lands on the **completed scorecard, read-only** — engine score + stamped versions displayed; no edit or approve controls |

API surface changes:
- `/score/{job_id}` response adds `state`, `scoring_status`, `overall_score`, `evaluation_id`
  (the poll loop keys color + link off `scoring_status`).
- Scorecard page gets a read-only mode (render-only when `state='finalized'`), and the pop-up on
  entry when `scoring_status='flagged_human_review'`.
- Pre-flip teams keep today's behavior verbatim — the frontend branches on fields that simply
  aren't present until the team flips.

This surface exercises every cutover component on demand — Stage-1 dual-write, trigger check,
engine compute, version stamping, projection, GAS email — one call at a time, which is exactly
where testing starts before the flip is announced to anyone else.

## 6. Prerequisites and sequencing

1. **4e — §3.14 human-review trigger (BLOCKING, both teams).** The auto-finalize-vs-pause
   decision is the heart of §2/§5. Ships first, inert behind the flag.
2. **Migration 013** — `scoring_owner` column.
3. **4c — `qa.agent_stat_points` seed on finalize (non-blocking).** Wanted before the read-path
   flip, not before the write flip.
4. **MS flip** after the §7 shadow week. MS prerequisites are already met (v3 live, Config.js
   regen in 3b).
5. **Sales flip** — sign-off is no longer the gate (Sales Mgmt ready, 2026-07-04); the remaining
   prerequisites are engineering, in one coordinated PR + operator step:
   - `sales.json` regen to the nested 15-section `sales_v2` shape (task #5; needs descriptor
     prose from `QA_Scoring_Guide.pdf` + the §3a/§3b mapping vs the archived 19-section rubric)
   - ship `sales_v2` rubric + formula (3d ceremony: CLI + restart)
   - Analyst_History / FR-AI tab restructure 19 → 15 sections + `Config.js` regen (§3b) + GAS
     deploy (owner)
   - Sales fixtures/prompts sanity pass (scoring prompt now drives 15 sections)
6. Backfill (Phases 5/6) stays last per owner sequencing; cutover does not depend on it. The
   ε-sweep and bonus endpoint (7b) want the backfill, so July bonus math runs on cutover-forward
   rows until it lands.

## 7. Verification gate + rollback

**There is no numeric parity gate against the sheet — by design.** The sheet still runs the
legacy formulas (twelfths / uniform-fifths); v3 and sales_v2 deltas are the point (BackfillPlan
§2/§2a). Correctness rests on the shipped armor: 96 golden fixtures across both teams proving
the engine reproduces real history exactly under the archived formulas, plus the §3.19.3 archive
cross-checks.

The pre-flip gate is *mechanical*, one week of shadow per team:

- At approve, compute the engine score alongside the readback (no persistence changes) and log
  `engine=X sheet=Y formula_version=Z trigger_fired=B` — verifies compute + version resolution +
  trigger checks fire cleanly on live traffic, not that X == Y.
- Zero swallowed dual-write failures over the window (the §7.3 flip makes any residual failure a
  user-facing error).
- Post-flip smoke: operator scores a known-clean and a known-flagged call via lookup (§5) and
  checks button color, read-only scorecard, DB row, Analyst_History projection, and the Gmail
  render.

**Rollback:** flip the flag back. The ARRAYFORMULA and weights tabs are left intact until both
teams have flipped + one full bonus cycle passes; reverting restores the legacy pipeline
wholesale. Rows scored by the engine during the window keep their stamped versions (reproducible
forever) — no data cleanup on rollback.

## 8. PR slicing

1. **4e trigger + migration 013 + shadow logging** — mergeable without behavior change (flag
   stays `sheets` everywhere).
2. **`sheets_projection.py` + approve/auto-finalize branch on the flag + §7.3 Phase C error mode
   + GAS `Config.js` regen (MS) + lookup/scorecard UX (§5)** — inert until the flag flips;
   owner deploys GAS after merge.
3. **Operator flip (MS)** after the shadow week:
   `UPDATE public.teams SET scoring_owner='postgres' WHERE id='member_support';` + §7 smoke.
4. **Sales-flip PR** — the §6.5 coordinated bundle (config regen, rubric/formula ship, tab
   restructure, Config.js regen), then the same operator flip + smoke.
5. **Cleanup PR** after both teams flip: delete Stage 3, destination write, Mails runtime
   lookup, pre-cutover stage branches.
