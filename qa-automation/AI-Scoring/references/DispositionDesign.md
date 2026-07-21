# Command Center v1 — webhook ingestion, dispositions, holds & AI-CSAT grounding

> v2 (2026-07-19) — reframed per owner directive: this is the **first
> live version of the Command Center project**, not merely a disposition
> column. The webhook stream becomes a continuously-documented source of
> per-call truth; `command_center.calls` grows a column whenever a new
> event field earns one (cheap now, by design); and the /score pipeline
> grounds Gemini with that truth — dispositions first, hold intervals
> and AI-CSAT alongside. Prompt-level RAG proper lands later this month
> after the Sandy re-platform (unless re-specified); until then the
> prompt gets EMPHASIZED plain injection.

| | |
|---|---|
| **Status** | v2.1 — MERGED 2026-07-19 (PR #120 + owner amendment: Spanish-call audio SOT). Build starts at C0 |
| **Supersedes** | v1's phase gating (P2 Stats-interim / P3-August webhooks). Webhooks are NOW — they are this project |
| **Related** | LandingOpsCommandCenter.md (CC phases; this implements the ingestion base), SQLMigration.md §4 (webhook_events / calls / the hold_intervals scrapping clause), [[project_rag_coach_cards]] |

---

## 1. Evidence base (all probed 2026-07-19)

- **Transcript "moments" are occurrence markers** — type name in
  `content`, no values, `name` = the agent. The current
  `FILTERED_MOMENT_TYPES` filter is rotted for this payload shape and
  leaks `[<AgentName>] at <ts>` noise into every scoring prompt (D1).
- **`GET /call/{id}` carries no disposition/CSAT/label keys** (12 scored
  calls probed). Webhook **call events** carry `call_dispositions`; the
  **Stats API** exports them batch-wise.
- **Dispositions — List** (MS call center `4716644561813504`, office
  `4839600547790848`; targets: Member Support Line `ERT` + Flight X):
  9 categories × ~50 subdispositions, admin-editable. Policy: selection
  REQUIRED 100% — but an agent pulled into a new call from the
  disposition-select screen leaves that call undispositioned. Absence is
  expected-normal, not an error.
- **One-day Stats export (2026-07-15, 229 calls)**: join key `call_id`
  (same id-space as our `dialpad_call_id`); disposition format
  **`Category~Subdisposition`**, bare `Category` when the agent stopped
  at level 1; **83% coverage** (38 empty — the back-to-back edge +
  outbound); 43 distinct labels that day; also carries `date_queued`,
  operator identity/email, per-agent `timezone`, `salesforce_activity_id`,
  `note` — fields worth columns later, logged here per the
  document-as-we-go rule. Sales onboards after the MS roadmap is robust.

## 2. Principles

1. **Nothing Dialpad hands us gets discarded.** Filtering is a prompt
   decision, never a storage decision. Markers →
   `dialpad_call_metadata.moments`; webhook payloads → `webhook_events`
   (append-only, already schema'd §4.1); per-call truths → columns.
2. **`command_center.calls` is the per-call truth surface** the scoring
   pipeline reads. New webhook fields that prove useful get columns as
   they appear — the table is young; column adds are cheap NOW.
3. **Ground the model with facts it otherwise guesses.** Disposition
   (what the agent said the call WAS), hold intervals (Gemini
   demonstrably hallucinates holds today), AI-CSAT. Emphasized plain
   injection now; full RAG keying post-Sandy.

## 3. Schema (migration 016)

**`command_center.calls`** gains:
- `disposition_category TEXT`, `disposition TEXT` — split from the
  `Category~Sub` form; bare-category selections leave `disposition`
  NULL. No enum CHECK (admin-editable labels drift).
- `ai_csat NUMERIC(3,1)` — Dialpad Ai CSAT. **Distinct from the
  user-survey CSAT** (which has a `survey_id` and stays out of scope);
  do NOT conflate with `qa.evaluations.csat_score`.
- `disposition_source TEXT` — `webhook` / `stats_pull` (provenance for
  the backfill-vs-live seam).

**`qa.evaluations`** gains `dialpad_disposition_category TEXT`,
`dialpad_disposition TEXT`, `ai_csat NUMERIC(3,1)` — stamped at Stage 1
from the CC match (§5) so the eval row is self-contained for analytics/
one-pagers (same reproducibility instinct as formula/rubric stamps).

**`command_center.hold_intervals` RETURNS.** Scrapped in SQLMigration
§4.4 with an explicit clause — *"bring it back the day a real query
needs the per-cycle detail"* — and that day is here: the consumer is
scoring-prompt grounding (per-cycle timing, not just the
`total_hold_seconds` rollup, because the prompt says WHERE in the call
holds happened). Shape: `id, team_id, dialpad_call_id, call_id FK→
calls, started_at, ended_at, seconds, ended_by ('connected'|'hangup')`.
Derivation rule (§4.1, tested): **no `unhold` event exists — a hold
cycle ends at the next `connected` or `hangup`.** Rows materialize at
ingest time; `total_hold_seconds` stays as the rollup.

## 4. Webhook ingestion (the CC v1 core)

- FastAPI receiver on the existing app (Railway). Verifies Dialpad's
  JWT signature (secret in env), appends to `webhook_events` verbatim,
  then folds into `calls` (upsert by `(team_id, dialpad_call_id)`):
  state transitions, `call_dispositions` → the two columns +
  `disposition_source='webhook'`, AI-CSAT when present, hold-cycle
  materialization into `hold_intervals`.
- **Sandy portability**: the receiver is one route + pure fold
  functions; at re-platform the only Dialpad-side change is updating the
  subscription URL (one API call). Keep the fold logic free of
  FastAPI/Railway specifics.
- Subscription setup (operator, one-time): create the event subscription
  for the MS call center; document id + secret in `.env`.
- **Event documentation discipline**: every observed `event_kind`/field
  we don't yet consume gets a line in this doc's appendix as it
  appears — the constantly-documented catalog the owner asked for.

## 5. /score integration — the grounding block

At Stage 1, after the transcript fetch:

1. **Match**: `command_center.calls` by
   `dialpad_entry_point_call_id OR dialpad_call_id OR
   dialpad_master_call_id` (in that order — entry-point id is what the
   eval usually carries; indexes exist: `uq_calls_team_call_id`, 014's
   entry-point index pattern extends to CC in 016).
2. **Pull**: disposition pair, `ai_csat`, `total_hold_seconds`, and the
   call's `hold_intervals` rows.
3. **Inject** a `Call context (verified system data)` block ahead of the
   transcript, EMPHASIZED:
   - *"The agent classified this call as: Access & Entry — Smart-lock
     failure. Score the call within reason FOR that disposition — the
     expectations of each section apply as they pertain to this call
     type."*
   - *"Verified hold record: 2 holds (1:42 at 03:15, 0:58 at 11:02).
     Do NOT infer holds beyond this record."* — or *"Verified: no holds
     occurred on this call."* (kills the hallucinated-hold class)
   - Absence path: *"No disposition was captured for this call
     (back-to-back handling) — score on transcript evidence alone
     OR audio content if call is in Spanish."*
4. Stamp the three `qa.evaluations` columns in the same Stage-1 write.

**Language rule (owner, 2026-07-19):** for Spanish calls the source of
truth is the AUDIO, not the transcript (Dialpad transcription is
English-biased). Every grounding-block sentence that references
"transcript evidence" must carry the audio alternative — C3's prompt
builder branches the wording on the eval's `language`, it does not
hardcode transcript-first phrasing.

This is deliberately plain-injection: the RAG rework (disposition keying
into SopRag retrieval, coach-cards corpus) follows the Sandy re-platform
later this month per the owner's sequencing.

## 6. AI-CSAT surfacing

`ai_csat` rides the same webhook/stats sources into both tables. Frontend:
the `/scorecard` route's stale X/5 badge (not SOT for anything) is
replaced by AI-CSAT — clearly labeled as Dialpad Ai's estimate, not a
member survey. (Frontend swap is its own small slice, C5.)

## 7. Historic backfill — Stats API

`scripts/pull_dispositions.py` (B-series conventions): initiate a Stats
export per call center + date range, poll, download, join on
`dialpad_call_id`, fill both tables' columns with
`disposition_source='stats_pull'` for rows the webhook era predates.
Batch-cheap; the 2026-07-15 sample proves fields + join. Also serves as
the catch-up sweep if the receiver ever drops events.

## 8. Slices

| # | Slice | Checkpoint |
|---|---|---|
| C0 | D1 from v1: moment-parse fix + marker persistence | prompt has zero marker lines; `dialpad_call_metadata.moments` populated |
| C1 | migration 016 (CC columns, qa.evaluations columns, hold_intervals) | runner up/down clean on prod |
| C2 | webhook receiver + fold (events→calls/holds/disposition/ai_csat) + subscription setup | replayed synthetic event fixtures → exact rows; pytest on fold + hold-cycle derivation (incl. reconnect flush); signature verify |
| C3 | /score triple-key match + grounding block + eval-row stamps | prompt-build pytest (disposition present / absent / holds / no-holds); shadow week: log-only compare of scored calls with vs without context |
| C4 | Stats-API backfill puller | coverage report; spot-check vs Dialpad UI |
| C5 | AI-CSAT frontend swap on /scorecard | stale X/5 gone; labeled Ai estimate |

## 9. Appendix — webhook event/field catalog (living)

| Field / event | Seen | Consumed by | Notes |
|---|---|---|---|
| states: ringing, connected, hold, hangup, recording, call_transcription, recap_summary | §4.1 (design-era) | calls fold, hold_intervals | no `unhold` — cycle ends on next connected/hangup |
| `call_dispositions` | docs (Call Events) | disposition columns | verify exact payload shape on first live event; the C2 extractor accepts string / list-of-strings (last wins) / dict with name/value |
| `event_timestamp` / `date_updated` | C2 synthetic (verify live) | webhook_events.event_timestamp | explicit event clock beats lifecycle dates — `date_connected` on a post-hold `connected` event may still carry the ORIGINAL connect time; fold clamps inverted cycles to zero-length |
| `ai_csat` | C2 synthetic (verify live) | ai_csat columns | extractor accepts number / numeric string / {score: n} |
| agent-status events (`target.type == 'user'`, no call_id) | C2 synthetic | — (appended verbatim, not folded) | fold is call-events-only in C2; agent-status columns when a consumer appears |
| Stats export: `date_queued`, operator identity, per-agent `timezone`, `salesforce_activity_id`, `note` | 07-15 export | — (candidates) | columns when a consumer appears |
| Stats request: `stat_type: "dispositions"` + `is_today` | owner curl 2026-07-20 | disposition_pull loop | INTERIM ingestion (webhook blocked on sub cap + Railway root dir): in-app 30-min `is_today` loop creates calls rows (seen_via='stats_pull', migration 017) so C3 grounds backfill-first; those rows carry NO hold truth — grounding branches on seen_via. Export timestamps are naive in the row's `timezone`. Team constraint: score only calls the last pull has covered |
| Stats export `call_id` id-space | live probe 2026-07-20 | triple-key match, eval fill | CORRECTION to §1's "same id-space as our dialpad_call_id": the dispositions export keys by the **ENTRY-POINT id** (scored agent leg 6517394311946240 absent; its entry-point 6750284618604544 present with disposition). Consequences shipped: the CC match tries every id against every id column; Stage 1 persists `dialpad_entry_point_call_id`/`master` on the eval; the eval fill also joins via `dialpad_link` (historic rows carry the entry-point id only there) |
