# ScorecardActionsDesign — formal lifecycle actions on the scorecard/datapoint pair

**Status:** v1.2 — doctrine settled, S1 build started (design-doc-first).
**Date:** 2026-07-24 (v1/v1.1) → 2026-07-25 (v1.2)
**Prompted by:** "Getting some formal methods directly into the
`/scorecard/<team>/<call_id>_<agent>` route — re-score, delete, edit — plus a
way of overriding the auto score only from this scorecard page." v1.1 added
the escalation ladder and the ≤50 auto-rescore; v1.2 inverts the coaching
gate (override *creates* the coaching), adds the acknowledgment protocol,
and writes down the tampering doctrine that frames all of it.

---

## 0. Doctrine — zero-touch is the goal; manual touch is designed tampering

The owner's framing (2026-07-25), which every section below serves:

> Ceteris paribus and all things working good, there should be **NO manual
> interaction at all** — manual interaction is tampering, and if tampering
> occurs, it needs to be a **designed, auditable hole** in the
> auto-pipeline.

Consequences:

1. **The auto-pipeline is the authority.** Score → finalize → project →
   email, zero analyst touch (this is already the clean-call reality via
   `_postgres_post_stage1`). Every manual doorway in this doc exists as a
   *safeguard* against three failure classes: AI hallucination, missing or
   outdated SOPs, and pipeline errors — not as a routine workflow.
2. **Every manual doorway leaves a double receipt**: a `score_audit` row
   (the machine-side ledger: `approved` / `rescored` / `overridden` /
   `evaluation_orphaned`) AND — for score-changing interventions — a
   `qa.coachings` record (the human-side ledger: who told the agent, when;
   §4.3a). Tampering without a receipt is a bug, not a feature.
3. **The Human-Review pipeline is authoritative today, informative
   tomorrow.** Today a fired §3.14 trigger *blocks* finalize
   (`flagged_human_review` holds the row in draft). At some point it flips
   to non-blocking: flagged evals finalize and ship like any other, the
   flag becomes an annotation feeding a review queue, and post-hoc
   corrections go through the override doorway. The flip is designed now
   as team config (`human_review: {mode: authoritative | informative}`,
   default `authoritative`) so it lands as a config change, not a
   redesign: in informative mode `_postgres_post_stage1` finalizes flagged
   rows too, keeps `human_review_required_at` as the queue marker
   (`required_at IS NOT NULL AND completed_at IS NULL`), and never sets
   the blocking `scoring_status`.
4. **Steady-state metric:** the audit ledger should show ~100%
   `scored`-only traffic. Manual-action rows are signal — each one is
   evidence of one of the three failure classes and should be traceable to
   a fix (a re-score, an SOP escalation, a pipeline bug).

## 1. Where we actually stand

The scorecard page (`main.py` → `frontend/scorecard.html`) drives three
routes in `backend/routes/scoring.py`, all keyed off the **in-memory
`_jobs` dict** (`_job_key(team_id, job_id)`, `job_id =
<dialpad_call_id>_<agent_name>`, spaces → underscores):

| Action | Route | State today |
|---|---|---|
| Edit + approve | `POST /score/{job_id}/approve` | **Exists — the default behavior.** Analyst edits ride the approval payload; `record_approval` rewrites sections (DELETE + re-INSERT), flips `source` to `ai_reviewed` when anything changed, then `stamp_and_finalize` computes the engine score. |
| Human-review hold | (same route, `resolving_review`) | A fired §3.14 trigger holds the row at `scoring_status='flagged_human_review'`; the manager's approval IS the resolution. Authoritative mode — see §0.3. |
| Re-score | — | No route. But `record_draft_evaluation` already upserts on the dedupe key and resets lifecycle columns (tests: `test_rescore_updates_row_and_replaces_sections`) — Stage 1 was built rescore-shaped; §4.2 adds the trigger paths, not the write. |
| Delete | — | Doesn't exist anywhere in code. The 2026-07-24 manual delete of eval 2377 (0.0-score outlier) was raw SQL; the schema reserved `score_audit.action='evaluation_orphaned'` for exactly this path but no route writes it. |
| Override auto score | — | Doesn't exist. `overall_score` is engine-owned (`stamp_and_finalize`); `record_approval` is always called with `overall_score_raw=None` post-cutover. |

Two structural facts shape the design:

1. **`_jobs` is process-lifetime.** A server restart 404s every old
   scorecard URL, while the same evaluation stays reachable forever at
   `/datapoint/{team}/{call_id}` via the indexed DB probe
   (`db_provider.get_by_eval_id`, migration 014). The scorecard and
   datapoint pages already render the same record — two views of one
   evaluation, one ephemeral, one durable.
2. **Finalized = read-only is enforced twice** (frontend
   `scorecard.html` and the 409 in `approve_scorecard`). Every action
   below punches a *specific, audited* hole in that wall (§0.2) rather
   than removing it.

## 2. Owner decisions (2026-07-24 → 25)

| Question | Decision |
|---|---|
| Doctrine | §0: zero-touch authority; manual = designed, auditable tampering. (v1.2) |
| Re-score semantics | **REPLACE.** Same eval row, downstream computations rebuilt — no supersede chain, no second row. |
| Auto re-score | Fires when a finalize lands `overall_score ≤ 50` — **exactly once per evaluation**; then the score stays. |
| Override semantics | **SUPERSEDE, riding `source='ai_reviewed'`.** Auto score stays reproducible from `evaluation_sections` + `formula_version`. |
| Override ↔ coaching | **Overriding CREATES the coaching**: a `qa.coachings` row + `coaching_evaluations` link, plus an acknowledgment pop-up binding the evaluator to notify the agent their progression was manually modified. Same protocol for human-review resolutions. (v1.2 — inverts v1.1's "coaching must pre-exist" gate) |
| HR pipeline trajectory | Authoritative today; **informative (non-blocking) later** via team-config mode — designed now, flipped by config (§0.3). (v1.2) |
| Delete auth | **Privileged key only.** |
| Edit auth | Team keys keep edit on the scorecard AND gain it on `/datapoints/*` — mirrored surfaces; the datapoint one survives restarts. |
| Override surface | Scorecard page only (not datapoint) — an approval-flow action, not an archaeology action. |
| Email on rescore/override | Default: re-send WITH a cause-specific disclaimer; checkbox opts *out*. |
| Sheets row of a deleted eval | Blank the projected row (tombstone mode) — Sheets is a projection; drift is a bug. |
| Concurrent-edit protection | Not worth it at current traffic; docstring records the optimistic-`updated_at` upgrade path (§3). |

## 3. The enabling seam — DB-backed action resolution (S1)

Every new action resolves its target **from Postgres, not `_jobs`**:

```
eval_store.resolve_evaluation(team_id, job_id) -> EvalRef | None
    # job_id = "<call_id>_<agent_name>" (spaces → "_"), or a bare
    # call_id on the datapoint surface. The call-id prefix is the
    # digits before the first "_"; probe qa.evaluations the way
    # get_by_eval_id does:
    #   WHERE team_id=$1 AND (dialpad_entry_point_call_id=$2
    #                         OR dialpad_call_id=$2)
    # NOTE: both id columns are probed on purpose — the 2026-07-24
    # incident eval was addressed by its dialpad_call_id while everyone
    # believed it was the entry-point id. Do not "simplify" to one column.
```

`_jobs` remains the live-scoring progress channel (SSE polling, status
transitions). Actions consult it only to refuse racing an in-flight job
(§4.2). This makes every action work on a years-old evaluation from the
datapoint page after any number of restarts.

The approve path adopts the seam immediately (S1): on a `_jobs` miss it
reconstructs the approval context from the DB row + sections instead of
404ing. One payload consequence: the in-memory job carried the original
`manager_email`; a reconstructed context doesn't have it, so
`ApprovalRequest` gains an optional `evaluator_email` (frontend sends the
signed-in evaluator; absent → fall back to the row's `evaluator_email`;
neither → 422).

Every mutating action route carries the traffic-scale docstring (§2):

```
Concurrency: last-writer-wins by design. qa.evaluations has no
updated_at column and these are low-traffic manager tools. If Landing
outgrows this (concurrent evaluators on one eval), add updated_at +
an If-Unmodified-Since-style optimistic check here — potential design
choice deliberately deferred, not overlooked.
```

## 4. The actions

### 4.0 The escalation ladder — how the pieces compose

The mutations are not peers; they form a quality-control loop whose end
state escalates *process* problems (outdated or missing SOPs), not just
call scores:

```
finalize lands score ≤ 50
        │
        ▼
auto re-score (once, ever) ─────── score now > 50 → done, email w/ disclaimer
        │ still ≤ 50
        ▼
review queue (blocking today, informative later — §0.3)
        │
        ▼
override (scorecard only) — the evaluator sets the score a human stands
behind; the act CREATES the coaching/1:1 record + notification duty
(§4.3a), and its SOP-gap note is the escalation record for missing or
outdated SOPs
```

An eval that scores badly twice under the same rubric+SOP context is
evidence about the *context*, not just the call — the override's
reasoning and SOP-gap field are where that evidence lands.

### 4.1 Edit — extend, don't rebuild

Approve-with-edits stays the default scorecard behavior, human-review
gate included. Changes:

- **`POST /datapoints/{call_id}/edit`** (team key or privileged): same
  `ApprovalRequest` payload and validation
  (`missing_manual_scores`, section-vs-config defense), routed through
  `record_approval` + `stamp_and_finalize` via §3 resolution. Editing a
  **finalized** eval from this surface is a re-approval — it re-runs the
  engine, re-projects, clears any standing override (the engine
  recomputes `overall_score`), and, being manual tampering with a shipped
  score, triggers the §4.3a acknowledgment protocol exactly like an
  override does.
- The scorecard approve path adopts §3 resolution as fallback when
  `_jobs` misses (S1), instead of 404ing.

### 4.2 Re-score — REPLACE, manual and automatic

**Manual:** `POST /score/{job_id}/rescore`, team key (roster-scoped) or
privileged. Refuse (409) when a `pending`/`scoring` job for the same key
is in flight.

**Automatic:** hooked at the finalize seam (both `_postgres_post_stage1`
and the approve path), after `stamp_and_finalize`:

```
if overall_score <= config.rescore.threshold      # default 50, team JSON
   and eval.auto_rescored_at IS NULL              # once, EVER
   and source == 'ai':                            # never discard human edits
    stamp auto_rescored_at = NOW()
    SUPPRESS this pass's projection + email       # agent must not see a
                                                  # score about to change
    schedule the rescore primitive (same background-task pattern as /score)
```

- **Once means once.** `auto_rescored_at` (migration 018, §6) is stamped
  *before* the re-run is scheduled, so a crash mid-rescore can't loop.
  The second finalize proceeds normally whatever the number: > 50 →
  project + email with the re-score disclaimer; still ≤ 50 → project +
  email AND enter the review queue (§0.3 mode decides blocking vs
  annotation) — the cue for the coaching → override escalation.
- **`source='ai'` only.** A human-approved (`ai_reviewed`) low score is a
  human's judgment of a genuinely bad call — auto-rescoring it would
  discard the analyst's edits. Escalation for those is override directly.
- The auto-rescore is machine-initiated and therefore NOT tampering
  (§0): it books `action='rescored'` with notes `"auto: <old> ≤
  <threshold>"` and creates no coaching record.
- Threshold lives in team JSON (`rescore: {threshold: 50}`) beside the
  stats knobs.

**The rescore primitive** (shared by both triggers):

1. Resolve the eval (§3). Re-download audio by `dialpad_call_id`
   (`download_recording`) — same input, fresh model pass through the
   existing `score_call` pipeline.
2. Reuse the Stage-1 upsert (`record_draft_evaluation` already resets
   lifecycle columns and replaces sections on the same row id — §1).
   Keeping the row id preserves every FK.
3. Delete the row's `agent_stat_points` point and rebuild the agent's
   series (§5) — the old finalized score must not linger in the EWMA
   chain while the row sits in draft.
4. Manual path: fresh job entry, normal review/approve flow. Auto path:
   the auto-flow decides auto-finalize vs. flagged, like any first pass.
5. Audit: `action='rescored'`, notes `"manual: <old>"` / `"auto: <old> ≤
   <threshold>"`.
6. Sheets projection: re-projected on the re-finalize, overwriting the
   history row — REPLACE all the way down.
7. Email: on the re-finalize, dispatch WITH the re-score disclaimer by
   default; manual-rescore UI shows the opt-out checkbox
   (`suppress_email=true`). The GAS template gains the disclaimer block
   (teams overlay + `push.sh` redeploy — Railway watch paths don't cover
   GAS).

### 4.3 Override — `POST /score/{job_id}/override` (SUPERSEDE)

Team key (roster-scoped) or privileged; **scorecard surface only**.
Target must be `finalized`. No precondition beyond that — the coaching
record is not a gate you pass, it's a receipt the action writes (v1.2).

```
{
  overall_score: float,
  reasoning: str,                     # required
  conducted_by_role: 'team_lead' | 'manager' | 'hr' | 'external',
  sop_gap: {note: str,                # optional — the SOP escalation:
            document_id: int?} | null #  outdated/missing SOP evidence;
                                      #  document_id → embeddings.sop_documents
  acknowledged: true,                 # §4.3a — literal true or 422
  suppress_email: bool = false,
  evaluator_email: str
}
```

One transaction:

1. `overall_score = $new`, `source='ai_reviewed'`. `formula_version` and
   sections stay — that pair reproduces the superseded auto score
   forever, so the UI can render "engine would say X" without storing X.
2. **Create the coaching receipt**: a `qa.coachings` row
   (`agent_id`, `team_id`, `conducted_by_role`, `conducted_by_email =
   evaluator_email`, `status='pending'`,
   `action_plan = 'Notify <agent>: score manually overridden <old> →
   <new>'`, `action_plan_deadline = NOW() + interval '3 days'`) + a
   `coaching_evaluations` link (`per_eval_note = reasoning`,
   `opportunities_snapshot` from the row). No schema change — the
   existing tables model this exactly.
3. Stat point deleted + series rebuilt (§5); re-projection to Sheets.
4. Audit: `action='overridden'`, notes
   `"<old> → <new>: <reasoning>" [+ " | SOP gap: <note>"]`.
5. Email: disclaimer dispatch by default. **If the disclaimer email goes
   out, the notification duty is discharged mechanically** — the coaching
   completes in the same transaction (`status='completed'`,
   `coaching_summary='Agent notified via override disclaimer email'`,
   `completed_by = evaluator_email`). If `suppress_email`, the coaching
   stays `pending` with its deadline — and `idx_coachings_pending`
   becomes the accountability queue for evaluators who owe an agent a
   conversation.

**SOP escalation:** `sop_gap` notes land in the audit row now;
aggregating them into an SOP-owner queue (Pulpo-side review of
`document_id`-clustered gaps) is deliberately deferred — filtering
`action='overridden'` in audit search is enough to start.

### 4.3a The acknowledgment protocol (v1.2) — every manual doorway

Any manual mutation of an agent-visible score — **override, human-review
resolution, edit-of-finalized** — exposes the same UI behavior:

1. A pop-up states what is about to happen ("You are manually modifying
   an AI-scored evaluation…") and requires explicit acknowledgment. The
   acknowledgment text binds the evaluator to **at least notify the
   agent that their progression has been manually modified by a human**.
2. `acknowledged: true` rides the request; the route 422s without it.
   The acknowledgment is recorded in the audit notes (`ack:<evaluator>`)
   and embodied in the coaching receipt (§4.3.2) — pending until the
   agent is notified, auto-completed when the disclaimer email
   discharges the duty.
3. Human-review resolutions (approve with `resolving_review`) create the
   same coaching receipt: the §3.14 flag said "a human must look"; the
   receipt records that a human looked AND that the agent knows.

Machine-initiated actions (auto-rescore, auto-finalize) never see this
protocol — it exists precisely at the human/pipeline boundary (§0.2).

### 4.4 Delete — `DELETE /score/{job_id}` (privileged only)

`identity.role == "privileged"` or 403 (+ `denied` audit row, matching
`check_scoring_access`'s pattern). Formalizes the 2026-07-24 manual
procedure, in order, one transaction:

1. Resolve (§3); capture a JSON snapshot of the row + sections + stat
   point into the response (client-side backup — `eval_2377_backup.json`,
   formalized).
2. `DELETE FROM qa.agent_stat_points WHERE evaluation_id=$1` (its FK is
   NO ACTION and blocks the row delete otherwise), remember `agent_id`.
3. `DELETE FROM qa.evaluations WHERE id=$1` — sections, tags, sweeps
   cascade. `coaching_evaluations` is NO ACTION **and stays that way**:
   refuse with 409 listing the coaching rows. Doubly load-bearing in
   v1.2 — coachings are the human-side tampering ledger (§0.2), so
   cascading them would erase the receipts that justify score changes.
4. Rebuild the agent's stat series (§5) — the manual procedure got lucky
   (the deleted point was the latest); the route must not depend on luck.
5. Audit: `action='evaluation_orphaned'` — the reserved action, finally
   written by code. `notes` records the requesting key role + old score.
6. Sheets projection: blank the projected history row via a
   `project_evaluation` tombstone mode.

Both pages get the button; datapoint is the primary home (it outlives
restarts), scorecard mirrors it. UI: type-the-agent-name confirm.

## 5. Stat-series rebuild — one primitive, three callers

`backfill_stat_points.py` already proves the series is deterministic
(DELETE + re-INSERT per agent, approved_at order, id tiebreak). Extract
its core into the service layer:

```
backend/services/stat_points.py
  async def rebuild_agent_series(conn, agent_id, config) -> int
      # DELETE the agent's points; replay finalized evals in
      # approved_at order via compute_point(); INSERT the fresh series.
      # Same transaction as the caller's mutation. Returns row count.
```

Callers: rescore (§4.2.3), override (§4.3.3), delete (§4.4.4). The
script becomes a thin CLI over the same function. Unlike the
finalize-time write, rebuild runs INSIDE the mutating transaction — if
the rebuild fails the whole action rolls back (an action that silently
corrupts the EWMA chain is worse than one that fails loudly; the
non-fatal contract stays finalize-only).

## 6. Migration 018 — audit vocabulary + auto-rescore stamp

```sql
-- 018_scorecard_actions.sql (+ _down.sql)

-- (a) audit action vocabulary — explicit actions beat notes-parsing
ALTER TABLE qa.score_audit DROP CONSTRAINT score_audit_action_check;
ALTER TABLE qa.score_audit ADD CONSTRAINT score_audit_action_check
    CHECK (action IN ('scored','denied','approved',
                      'evaluation_orphaned','rescored','overridden'));
-- same for qa.score_audit_archive

-- (b) the once-ever auto-rescore latch (§4.2). Timestamp, not boolean:
--     "when did the machine take its one retry" is audit signal.
ALTER TABLE qa.evaluations ADD COLUMN auto_rescored_at TIMESTAMPTZ;
```

No other DB change: override rides `ai_reviewed`, rescore reuses the
Stage-1 upsert, the coaching receipt uses `qa.coachings` /
`qa.coaching_evaluations` as-is, and the HR mode flip (§0.3) is team
JSON, not schema.

## 7. Auth matrix

| Action | Team key | Privileged key | Surfaces | Preconditions |
|---|---|---|---|---|
| View | ✓ | ✓ | scorecard, datapoint | — |
| Edit / approve (draft) | ✓ (roster-scoped) | ✓ | scorecard, datapoint | — |
| Edit of finalized | ✓ (roster-scoped) | ✓ | datapoint (primary) | §4.3a acknowledgment |
| Re-score (manual) | ✓ (roster-scoped) | ✓ | scorecard, datapoint | no in-flight job |
| Re-score (auto) | system | system | finalize seam | score ≤ threshold, `auto_rescored_at IS NULL`, `source='ai'` |
| HR resolution | ✓ (roster-scoped) | ✓ | scorecard | flagged eval + §4.3a acknowledgment |
| Override score | ✓ (roster-scoped) | ✓ | **scorecard only** | finalized + §4.3a acknowledgment |
| Delete | ✗ → 403 + audit | ✓ | scorecard, datapoint | no coaching rows (409 otherwise) |

"Roster-scoped" = the existing `check_scoring_access` gate: the target
agent must be in the team's Mails roster for team keys.

## 8. Resolved-question log (v1 → v1.2)

1. **Email on re-finalize:** re-send by default WITH a disclaimer naming
   the cause; checkbox opts out. The disclaimer email doubles as the
   mechanical discharge of the §4.3a notification duty.
2. **Sheets projection of a deleted eval:** blank the row (tombstone).
3. **Concurrent edit protection:** skipped at current traffic; docstring
   on every action route records the upgrade path (§3).
4. **Coaching gate direction (v1.1 → v1.2):** overrides *create* the
   coaching receipt rather than requiring one to pre-exist.
5. **HR pipeline:** authoritative → informative later, via team config;
   designed now, default `authoritative` (§0.3).

## 9. Build order & pytest checkpoints

Slices land in this order; each has its checkpoint before the next
starts (workflow rule):

| Slice | Contents | Checkpoint tests |
|---|---|---|
| S1 | §3 `resolve_evaluation` + approve-path fallback + `ApprovalRequest.evaluator_email` + concurrency docstrings | probe by call_id / entry_point id / miss → None; job-id parsing (agent underscores, bare call_id); restart simulation (empty `_jobs`) approves via fallback; finalized fallback → 409; evaluator_email fallback chain |
| S2 | §5 `rebuild_agent_series` + CLI rewire | golden-series parity vs `backfill_stat_points.py` on fixture history; mid-chain delete rebuild correctness (EWMA/σ recomputed, flags re-judged) |
| S3 | Migration 018 + §4.4 delete route + tombstone projection | 403 + denied-audit for team key; coaching-row 409; cascade verification; `evaluation_orphaned` row written; snapshot in response; Sheets row blanked |
| S4 | §4.2 manual rescore | in-flight 409; same-row-id preservation via Stage-1 upsert; stat point removed while draft; full re-approve round-trip; disclaimer email + opt-out |
| S5 | §4.2 auto rescore + `human_review.mode` config flag (default authoritative) | fires at ≤ threshold on `source='ai'` only; latch stamps before re-run (crash sim → no loop); first-pass email suppressed; second-low-score enters review queue per mode; `ai_reviewed` low score does NOT fire; informative mode finalizes flagged rows with queue marker |
| S6 | §4.3 override + §4.3a acknowledgment protocol (override + HR resolution + edit-of-finalized) + scorecard UI | `acknowledged` 422 gate; coaching receipt created (pending + deadline); disclaimer email auto-completes the coaching; suppress_email leaves it pending; HR resolution creates receipt; `overridden` audit with SOP-gap note; series rebuilt; engine-score reproducibility (recompute from sections == old score) |
| S7 | §4.1 datapoint edit surface + frontend action bars + GAS disclaimer template | datapoint edit round-trip; override control absent on datapoint page; role-gated button rendering; GAS overlay builds via `push.sh` |
