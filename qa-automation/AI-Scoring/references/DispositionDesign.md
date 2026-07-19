# Call Dispositions — persistence, ingestion, and the scoring-prompt primer

> Owner mandate (2026-07-19): the agent-selected end-of-call disposition
> must stop being dropped and become a primer for scoring-prompt RAG.
> Sequenced after July packaging (R2/R3, PR #118), before the Sandy
> re-platform. First question asked by the owner: should moment data be
> stripped as noise at all, or persisted for analysis and prompting?

| | |
|---|---|
| **Status** | v1 draft — 2026-07-19, evidence-gathering complete |
| **Registry** | LateStageDesign Tier 3 row → this doc |
| **Related** | [[project_rag_coach_cards]] (scoring prompt = RAG consumer #1); August webhook automation (LateStageDesign Tier 3) |

---

## 1. Evidence — where disposition data actually lives (probed 2026-07-19)

| Source | What it carries | Verdict |
|---|---|---|
| `GET /transcripts/{id}` "moment" lines | **Occurrence markers only**: `content` holds the moment TYPE name (`call_disposition`, `call_purpose_category`, `ai_csat_reboot`, …), `moment_type` is None, `name` is the AGENT. No values. | already fetched per scored call; proves *that* a disposition was set + when — never *what* |
| `GET /call/{id}` | direction/duration/recordings/MOS/state… | **no disposition/CSAT/label keys** across 12 probed scored calls |
| `command_center.calls.raw_call_details` | empty on recent rows | no free lunch |
| Webhook **call events** | documented `call_dispositions` field in the event payload | the clean source — arrives with the **August webhook work** (`webhook_events` table is live but empty today) |
| **Stats API** (async batch export) | per-call-center exports including dispositions | available TODAY: backfill + interim forward pull |

**Bonus finding — the current filter is rotted.** `FILTERED_MOMENT_TYPES`
matches `moment_type` / `name`, but current payloads put the type in
`content` — so marker lines sail PAST the filter and reach the scoring
prompt as `[<AgentName>] at <timestamp>` lines (~10–40 per call of pure
noise, plus the agent's name repeated). Whatever else this design does,
fixing that is an immediate prompt-hygiene win.

## 2. The strip-vs-persist question (owner's framing)

Strip-as-noise conflated two concerns; they get different answers:

- **Prompt**: the markers carry no values, so un-filtering them buys the
  model nothing — keep the scoring prompt lean. Exceptions once VALUES
  exist (§5): the disposition and call-purpose values are context the
  model should see. Verbatim marker spam stays out.
- **Persistence**: keep EVERYTHING. Markers are ~40 tiny rows per call we
  already hold in memory at Stage 1 — persisting them to
  `dialpad_call_metadata.moments` (typed, timestamped) costs nothing and
  unlocks analysis the sheet era never had: *did the agent set a
  disposition* (compliance %), moment-density vs score, AI-CSAT
  occurrence vs `csat_score` (a column waiting empty since 006).

**Principle going forward: nothing Dialpad hands us gets discarded;
filtering is a PROMPT decision, not a storage decision.**

## 3. Schema (migration 016)

- `qa.evaluations.dialpad_disposition TEXT` — the value the scoring/RAG/
  one-pager surface consumes. NULL = never captured.
- `command_center.calls.dialpad_disposition TEXT` — the ops-side copy
  (CC dashboards, ratio queries), stamped by webhook ingestion.
- No enum CHECK: disposition labels are Dialpad-admin-configured and
  will drift; a CHECK would turn every ops edit into a migration.
- Marker list → existing `dialpad_call_metadata` JSONB (`moments` key),
  no schema change.

## 4. Ingestion — three phases

- **P1 (now): Stage-1 marker persistence + filter fix.** Correct the
  moment parse for the current payload shape (type in `content`), keep
  markers out of the prompt, persist all of them to
  `dialpad_call_metadata.moments`.
- **P2 (now): Stats-API disposition puller.** Operator-run/nightly
  script: initiate a stats export per call center + date range, join per
  call, fill `dialpad_disposition` on both tables. Doubles as the HISTORIC
  BACKFILL (answering the re-fetch-vs-forward-only question: the Stats
  API makes backfill batch-cheap — no per-call re-fetch under rate
  limits).
- **P3 (August): webhook ingestion.** The call-event `call_dispositions`
  field stamps both tables at event time — synchronous availability,
  which is what makes the prompt primer reliable (§5).

## 5. Scoring-prompt primer (the RAG rework)

Timing is the crux: scoring runs minutes-to-hours after the call.
Stats-API pulls are batch/next-day, so under P2 the disposition often
does NOT exist yet at scoring time — injection would silently cover only
late-scored calls. Therefore:

- The prompt primer ships gated on P3 (webhooks), when the value is
  present at scoring time deterministically.
- Injection shape: a `Call context` block ahead of the transcript —
  disposition + call purpose (when captured) — priming section relevance
  (e.g. disposition "Lockout resolved" primes Process Adherence and Call
  Resolution expectations) and, in the SopRag cascade, keying retrieval
  toward the disposition's SOP family before Stage-A embedding search.
- Until P3, the persisted value serves analytics, the one-pager, and
  coaching views — no silent partial prompt coverage.

## 6. Open questions (owner/ops)

1. Is disposition selection REQUIRED for MS agents in the Dialpad admin
   config, and what is the configured label list? (Drives how much
   compliance signal the marker data carries, and the primer's wording.)
2. Verify the Stats export's per-call join key + disposition field names
   on a real export before building P2 (docs are thin; one manual export
   from the admin UI settles it).
3. Should `csat_score` ride along? The AI-CSAT marker exists per call and
   the column has waited since 006 — same Stats/webhook sources; near-zero
   marginal cost inside P2/P3.

## 7. Slices

| # | Slice | Checkpoint |
|---|---|---|
| D1 | filter fix + marker persistence (`dialpad_client` parse, Stage-1 metadata write) | pytest on the parse against the REAL payload shape; prompt contains zero marker lines; metadata carries the full set |
| D2 | migration 016 + Stats-API puller (`scripts/pull_dispositions.py`, B-series conventions) + backfill run | dry-run join coverage report; spot-check vs Dialpad UI |
| D3 | webhook stamp (folds into the August webhook slice) | event → both columns, idempotent |
| D4 | prompt primer + SopRag keying (gated on D3) | prompt-build pytest; before/after scoring comparison on a sampled week |
