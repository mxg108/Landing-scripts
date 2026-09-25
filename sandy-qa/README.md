# ⏸ PAUSED — auto-scoring is OFF (since 2026-09-24)

> **Owner instruction (Max Pérez, 2026-09-24): "Turn off all auto-scoring
> pipelines. Nothing moves until Diana or Todd take over this project."**
>
> What is off (one switch — the `qa_settings` row `autoscore_paused`,
> migration `0022`, read by `src/lib/pause.ts` on every ticker step):
> - the nightly Member Support disposition sweep (no calls are selected or
>   scored automatically),
> - the hourly Sofia/Retell sweep,
> - the one-shot cron jobs: EOM progression assessments, **HR-bonus GAS
>   dispatch on the 1st**, the daily agent digest.
>
> What still runs: the EOD Google-Sheet report, the supervisor pulls, the
> queue pump (only moves jobs a human submitted), the console "Score Call".
> The hourly `cron_runs` note carries `"paused": {...}` while the pause is on.
> Not touched: the laptop crons (`shadow_sync` every 30 min, parity nightly)
> and the Railway service — see `references/RailwayRetirement.md`.
>
> **To resume** (new owner): `sandy.py db migrate <app-id> "DELETE FROM
> qa_settings WHERE key='autoscore_paused'"` — no deploy. The first night
> back sweeps only *yesterday*; days missed during the pause stay unscored
> unless re-armed (`references/CronContinuation.md` §6.4). The September HR
> bonus (`qa_cron_jobs` key `2026-09`) will need a manual `INSERT` if the
> pause outlives Oct 1.

# Sandy App — qa-scoring

Landing's call-quality platform on Sandy: two-stage AI scoring (Gemini
annotate → Claude judge via the AI Gateway), analyst review, team and
agent analytics, the coaching loop, the Member Support EOD call report,
and — since v0.77 — the Supervisor Deliverables cycle (end-of-shift,
weekly, bi-weekly tracker, monthly) that the Member Support Direction index
requires.

Live: https://qa-scoring.sandy.hellolanding.tech · Design docs: `references/`
(one spec per feature; each carries its ladder and open items).

## Surfaces

| Path | What | Gate |
|---|---|---|
| `/dashboard/{team}` · `/dashboard/{team}/agent/{name}` | team + agent analytics, drill-downs | any SSO user |
| `/score/{team}` · `/lookup/{team}` · `/scorecard/{team}/{id}` | scoring console, call lookup, scorecard editor | admin \| qa |
| `/coaching/{team}` (+ `/session/{id}`) | coaching queue, sessions, tags | coach (admin \| qa \| manager) |
| `/supervisor/{team}` (+ `/report/{id}`, `/tracker/{date}`) | supervisor deliverables hub, report editor, Tuesday tracker | coach |
| `/admin` | roles + access requests | admin |
| `/api/v1/mcp` | MCP server over D1 | service token |

Cron (2-slot cap): hourly `7 * * * *` → qa-cron-ticker continuation (sweeps,
EOD report, supervisor pulls, queue pump); daily `37 9 * * *` → maintenance.

---

# Handover (written 2026-09-24 for Diana / Todd)

Read in this order: this section → **Pending work** → **Known issues and
roadblocks** → `references/CronContinuation.md` (how the cron/ticker
machinery works today) → the design doc of whatever you touch next.

## 1. Where everything lives

| Piece | Where | Notes |
|---|---|---|
| App (Cloudflare Worker, SSR React, D1) | Sandy app `qa-scoring`, id `a2cc5b5a-df29-4ae7-9dbb-e270052015e7`, D1 `15a839da-…` | Deploy = `sandy.py push` + `publish` (see `~/.claude/commands/references/build-and-deploy.md`). Last live: **v0.79** (pause) on top of v0.78. |
| Workflows (durable, minutes-long jobs) | `qa-scoring-pipeline` (`25dec973-…`, v0.4: Dialpad audio → Gemini annotate → Claude judge → callback) · `qa-cron-ticker` (`ab006dfa-…`, v0.1: the bounded-step loop) · `qa-insights` (assessments, digests) | Source in `workflows/`. One active run per workflow (platform rule). Keep status `ready` — the Monday dormancy sweep deletes `in_draft` workflows 28 days after the last push. |
| Secrets | Sandy Dashboard only (names ≤ 20 chars) | `DIALPAD_API_KEY`, `RETELL_API_KEY`, `PULPO_MCP_URL/TOKEN`, `GAS_WEBAPP_URL_*`, `GSHEETS_SA_JSON`, `SLACK_*`, `SNOWFLAKE_MCP_TOKEN`; workflow secrets `GEMINI_API_KEY`, `DIALPAD_API_KEY`; org-injected `AI_GATEWAY_TOKEN`. Gemini bills to **our** key; Claude bills to Engineering's AI Gateway. |
| Migrations | `migrations/0001…0022` | Applied with `sandy.py db migrate <id> - < file`. The D1 query endpoint is read-only: every data fix is a migration file or an inline `db migrate`. |
| Tests | `node tests/*.test.mjs` (node:sqlite behind a D1 shim, real migration chain) | No `npm test`; run each file. `tests/routes/scoring.js` is the harness stub for the scoring route. |
| Emails / HR workbook | Google Apps Script under `qa-automation/` (`./push.sh <team>`) | Sandy pushes payloads to GAS ("payload mode"); GAS cannot call Sandy (SSO wall). clasp gotchas in `qa-automation/teams/*/README.md`. |
| Old system | Railway `qa-automation/AI-Scoring` (FastAPI + Postgres) | Still up; analysts may still score there. Retirement runbook: `references/RailwayRetirement.md` (proposal, unsigned). |
| Laptop-side | `crontab`: `scripts/shadow_sync.sh` every 30 min (Railway PG → D1 mirror), `parity/nightly.sh` 09:30 | Both need Max's laptop awake and a valid `sandy.py` token (10-day TTL). Neither scores calls. |
| Monitoring | `references/NightWatch.md` (overnight watcher runbook, `scripts/night_poll.py`) | Laptop-only (device token). |
| Open PRs | #211 (cron continuation + cost doc), #212 (daily digest), #213 (this pause + the live-first supervisor code) | Stacked; merge in that order. `main` is behind production by ~15 versions. |

## 2. Pending work (owner-named, 2026-09-24)

### 2.1 Pulpo / SOP retrieval broke — QA evals stopped pulling SOPs

**Symptom (live D1, Member Support, Sandy-born evals):** SOP grounding was
100% of evals every week through the week of Sep 14. From **2026-09-22** the
judge prompt carries `sop_skipped_reason = "no_hits_in_team_scope"` on
**121 of 176 evals (69%)**. Process Adherence is scored "exclusively from
the retrieved SOP documents" since migration 0020 (2026-09-17), so an eval
with no SOP now has nothing to score that section against.

**What it is:** not a Pulpo outage. `no_hits_in_team_scope` is raised by
`src/lib/sopRetrieval.ts` (`applyTagScope`) when every Pulpo hit is
filtered out by the team's tag whitelist (`teams.retrieval_config`,
`references/RetrievalScope.md` v2, migration 0013). That scoping was added
on 2026-08-31 because Sofia's engineering estate (`system:sofia` docs) had
leaked into MS/Sales retrieval and cost −5.8 points per contaminated eval.
The likely trigger for the Sep 22 break is a change on the Pulpo side
(document tags re-canonicalized, MS SOPs re-tagged or moved), a tightened
whitelist, or the 0020 rubric change producing queries the tagged corpus
does not match. Nobody has confirmed which.

**To do:** (1) run the retrieval probe from `RetrievalScope.md` (top hits +
their tags for 5 recent misses) and compare against `retrieval_config`;
(2) fix tags on the Pulpo side or widen the whitelist; (3) decide the open
rubric question — Process Adherence when *no* SOP step applies: `NA` vs a
guardrail line (`references/NightWatch.md` "PA sensor",
open since 2026-09-17); (4) re-score or accept the 09-22→09-24 cohort. Earlier
retrieval incidents and their fixes: 60/min quota burst → trigger-time
retrieval (v0.64); 500-char query cap; `list_documents_by_tag` silently
truncating at 50 docs (replaced by hit-tag scoping). Pulpo's own open
items: `qa-automation/AI-Scoring/references/PulpoConnection.md` P5/P6; the
*Unit Issues — Pests* label has no SOP coverage.

### 2.2 Score drift since the Railway → Sandy migration; the harsher Claude judge; the calibration loop

**Numbers (Member Support, finalized evals, live D1):**

| Period | Pipeline | Avg overall |
|---|---|---|
| Jul 2026 (Railway) | Gemini 2.5 Flash single-stage judge | 77–84 (AI), 80.8 (manual) |
| Aug 2026 (Sandy) | Gemini annotate → **Claude judge** | 56.3 |
| Aug 2026 (Sandy, same weeks) | Gemini judge (`SCORING_MODEL_PROVIDER=gemini` or Plan-B single-stage) | 73.5 / 69.2 |
| Sep 2026 (Sandy) | Claude judge | 56.1 |
| Sep 2026 (Sandy, same weeks) | Gemini judge / Plan-B | 73.2 / 71.8 |

The judge in `src/routes/scoring.ts` is pinned to **`claude-sonnet-5`**
(the owner refers to it as "the harsher Opus judge"; no Opus judge
configuration exists in the repo — Opus is used for progression analysis
only, `TwoStageScoringDesign.md` §10.2). On the same rubric and formula the
Claude judge scores ~17–20 points below the Gemini judge. The 2026-08-31
decomposition (`references/RetrievalScope.md` §Why) ranks the causes:
(1) judge model swap — largest; (2) Sofia SOP leakage — fixed by tag
scoping; (3) selection change — random nightly sweep vs analyst-picked
calls. Two calibration tweaks shipped since: v0.65 scoring anchors and
team identity in the judge prompt; migration 0020 (Process Adherence
scoped to SOPs). Neither closed the gap.

**Calibration loop (scoped, partially modeled, not built):**
`references/AriaIntegrationSpec.md` §3 frames weekly calibration as a
PID loop shared with Aria (Jackson's ticket grader): a weekly stratified
human sample (n=18, the *sensor*), setpoint A* = 90 ± 3, P/I terms that
adjust sample size and the human-review gate fraction (fail-safe direction
is always "more human review"), an integral threshold that fires a Claude
meta-analysis drafting a rubric version bump, and a D-term drift alarm at
ΔA ≤ −10. Tables are designed (`qa_calibration_items / _reviews /
_state`), per-section confidence markers are already persisted on every
eval for it, and the `/calibration` review page and Monday sampler are
ladder item C4. Blockers: C2 needs Engineering's `sast_` service token;
§5 open questions (rubric v1 weights, PID parameters, v0 backfill).
**Decision the new owner must make first:** is the Claude-judge level the
new honest baseline (re-anchor the HR-bonus thresholds and coaching
triggers to it), or should the loop pull scores back toward the Railway
era? The HR bonus already consumes these scores.

### 2.3 The Gemini annotator dependency and the local-annotator proposal

Stage A of every eval is `gemini-2.5-flash` listening to the audio and
producing the annotated transcript (turns, emotion, pace, holds — the
judge scores from this alone; audio is the source of truth for Spanish
calls, owner rule 2026-07-19). This is a **single-model dependency**: every
Gemini 3.x model rejected audio on our key and 2.5 Pro was closed to new
accounts (`TwoStageScoringDesign.md` §10.4, commit `b6584e4`); newer Flash
models cost 2.5–5× more; Google lists no shutdown date for 2.5 Flash as of
2026-09-16 but sets it on its own schedule (`references/CostOptimization.md`
§0.4). Gemini quota bursts (429) already cost evals twice (Sep 6, mitigated
by `fetchWithBackoff` + transient requeue in workflow v0.4).

**Proposal (owner, repeatedly since the July "Local AI" roadmap):** run the
annotator on a Landing-hosted model — **Gemma 4** (the `SQLMigration.md`
§8.3a cascade names *Qwen2-Audio + Gemma 4*, with audio-dependent sections
re-routed to Gemini when confidence comes back LOW; spec stub
`landing-ai/LandGPT.md`) or **OpenAI Whisper** for the transcription leg.
Landing's Engineering team should be able to work around the
audio-listening requirement (self-hosted inference is already on the
Command Center roadmap as Phase 5, `LandingOpsCommandCenter.md`). Two
cautions for whoever builds it: Whisper transcribes but does not produce
the emotion/pace/hold annotations the judge relies on (a second pass or
prosody features would be needed), and any swap must go through the
score-diff harness (30 calls, both annotators, same judge) plus a
bilingual fidelity spot check before it scores anyone's bonus. The
workflow already treats the annotator as a payload-driven leg
(`workflows/qa-scoring-pipeline.js`), so a new provider is a workflow
change, not an app change.

## 3. Known issues and roadblocks (chronological) and how each was worked around

Full detail per item lives in the cited doc/commit. Dates are 2026.

| When | Roadblock | Impact | Workaround / resolution | Where |
|---|---|---|---|---|
| Feb–Mar | GAS `onFormSubmit` read a row before its ARRAYFORMULAs filled | Wrong/missing agent email on scorecards | Resolve email from the Mails sheet; wait for formulas | `d5d9501`, `52fce75` |
| Apr 07 | Apps Script column explosion (ARRAYFORMULA × `getLastColumn`) | Approve flow slow/broken | Explicit bounded column reads | `284a965` |
| Apr 28 | `push.sh` staged `rootDir:"./src"` → clasp said "up to date" and pushed nothing | Deploys silently no-op'd | `push.sh` rewrites `rootDir`; never `clasp push` from `teams/<team>/` | `c140c14`, `qa-automation/teams/sales/README.md` |
| May 19–20 | Railway edge outage (GCP blocked the account); Hobby-plan deploys off; Watch Paths skip commits outside `AI-Scoring/` | Merged PR never deployed | `.deploy-trigger` bump; Pro plan; "Deploy Latest Commit" | `d965ffb`, `57bcb5e` |
| Jun 19 / Jul 19 | Dialpad transcripts unreliable for Spanish | Scores built on bad text | **Audio is the source of truth**; prompt wording branches on eval language | `ad4de4f`, `08600cc`, `DispositionDesign.md` |
| Jul 12 | 19 backfill rows have no call clock | Never importable | Marked `import_blocked` forever | `database/BackfillPlan.md` §7 |
| Jul 20 | Stats export `call_id` is the entry-point id, not the leg id | 0 grounding stamps | Match all three id columns; persist entry-point + master ids | `2e1274e`, `DispositionDesign.md` §9 |
| Jul 20 | "Form Responses AI" tab hit the 965-row grid limit | Sheets 400 blocked all scoring | Draft write removed; Sheets kept only as a projection | `fdc0a5f` |
| Jul | **Dialpad webhooks cannot send custom headers**; Sandy's SSO wall needs `X-App-Service-Token` | No webhook ingress on Sandy (Railway's is the only one) | 30-min stats-pull loop, then the nightly sweep; Engineering ask #1 | `SandyMigration.md` §5.6, `RailwayRetirement.md` B7 |
| Jul 24 | Pulpo rejects queries > 500 chars | Retrieval errors | Queries clamped | `7c3cf0b` |
| Jul 26 | Gemini 3.x rejects audio; 2.5 Pro closed | Single annotator model | `gemini-2.5-flash` only, fail fast on 400 | `b6584e4`, `TwoStageScoringDesign.md` §10.4 |
| Jul 26–27 | Annotator thinking ate the output budget (62.9k of 65.5k tokens); "uh uh…" loops | Truncated annotations | Thinking budget 4096, constrained JSON, temp-bump retry, last-turn salvage | `4b54101`, `0711883` |
| Jul 27–28 | Anthropic SDK refused non-streaming 65k `max_tokens`; ~1/29 judge replies had a trailing comma | Judge failures | Always stream; structured output; trailing-comma salvage | `b34848a`, `20633d0` |
| Jul 29 | `command_center/` outside Railway's root dir; sync Gemini/Sheets calls pinned the event loop | Webhook 404s; pages froze up to 3 min | Move package; `client.aio` + `to_thread` | `96ccf18`, `99a7585` |
| Jul 29 | Sandy constraints: SSO wall, personal 10-day push token, secret names ≤ 20 chars, 2-cron cap, outbound allow-list | Re-platforming rules | Rename map, payload-mode GAS, accepted deltas | `SandyMigration.md` §3/§5/§7 |
| Aug 03 | Shadow sync's wipe-and-reimport deleted the first Sandy-scored evals | Data loss | Sandy-born ids start at 10,000,000; wipes range-scoped | `249f2da` |
| Aug 03 | One active run per workflow (409) | Only 1 of 3 batch calls scored | D1 `qa_score_queue`, CAS-claimed drain | `ded2aa6` (v0.20) |
| Aug 03–16 | Shadow sync failed every run (unscoped parent wipes vs deferred FKs; token 401s) | Mirror stale two weeks | Range-scoped parents; upserts; explicit high-range ids for every Sandy-born row | `0dc6d7a`, `CoachingLoopSpec.md` §2.1 |
| Aug 10 | Rescore with a coaching receipt hit an FK failure; callback wedged the queue | Stale scorecards | Rescore updates in place; failures terminal | `0f0bbb7` (v0.33) |
| Aug 19 | `sandy.py` had no socket timeout; a sync hung 25 h while cron stacked runs | Mirror shredded (102 vs 515 July evals) | pid lock, 20-min kill, socket timeouts | `45508d0`, `CoachingTagsSpec.md` §1.3 |
| Aug 20 | Callback re-drain loses the platform-slot race (run still "active" during its own callback) | Queue advanced once an hour | `waitUntil` re-drains at +6 s/+15 s (≈30 s cap) | `b777c37`, `79b27d9` |
| Aug 20–30 | Assessment section ids collided with Postgres | Sync UNIQUE failures | Explicit high-range child ids | `RailwayRetirement.md` §8 |
| Aug 30 | Analysts still scoring on Railway spend on Max's **personal Anthropic key** | Credit leak (~90 evals) | Four stop options proposed, unsigned | `RailwayRetirement.md` §2 |
| Aug 31 | MS Overall 79.8 → 57.8 across the migration | Score drift (see §2.2) | Tag-scoped retrieval (0013); anchors (v0.65); PA scope (0020) | `RetrievalScope.md` |
| Aug 31 – Sep 02 | Intermittent platform 5xx on workflow triggers; nine chain breaks in one night (13 h drain) | Stalled drains, burned attempts | 5xx = silent requeue; inline backoff | `6972363` (v0.61), v0.72 |
| Sep 01 | Enqueue burst of ~58 Pulpo lookups vs 60/min | 14 of 62 evals without SOP | SOP retrieval at trigger time | `172ee76` (v0.64) |
| Sep 01 | Railway's Sep 1 HR run computed August from the ~90 leak evals | Wrong HR input | HR export ported to Sandy, push to GAS | `e177819` (v0.63) |
| Sep 04–08 | Dialpad Stats request ids expire after ~1 h; single-day exports come back hourly; a 429 failed the whole `Promise.all` | EOD report errored three mornings | Re-initiate on 400/404; two-day ranges; 429 = not ready | v0.69–v0.73, `ShiftReport.md` §10.2 |
| Sep 06 | Workers "Illegal invocation" (`fetch` called as a method) | Sheets writes failed | Detached wrapper; stubs enforce `this` | `8364cac` (v0.70) |
| Sep 06 | Gemini 429 bursts killed 5 nightly jobs | Missing evals | `fetchWithBackoff`, transient requeue, cost stamps | `2dab440` (v0.72, wf v0.4) |
| Sep 11–16 | **Sandy cron outage**: scheduler skipped ticks (20-h hole Sep 14), then its fix bounded dispatch time and silently cut every tick doing real work; dormancy sweep threatens `in_draft` workflows | Sweeps for 09-11/13/14/15 lost; EOD stuck | Instant-ack cron + `qa-cron-ticker` bounded steps; sweep as a state machine | `CronContinuation.md`, PR #211 |
| Sep 17 | Process Adherence scores leaked into holds/pacing/tone | Double-counting | Migration 0020 scopes PA to retrieved SOPs | `d7bb571` |
| Sep 18 | Laptop watcher died on sleep | Monitoring gaps | AC power + `caffeinate` | `NightWatch.md` |
| Sep 22 → | Retrieval returns `no_hits_in_team_scope` on 69% of MS evals | No SOP grounding (see §2.1) | **Open** | this README |
| Sep 24 | Owner pause | — | `qa_settings.autoscore_paused` (0022, v0.79) | top of this file, PR #213 |

## 4. Standing constraints to design around

- **Sandy platform:** 2 cron schedules max, fixed minute; a cron dispatch
  must be acknowledged within seconds (bound unpublished — CC5 asks
  Engineering); `waitUntil` ≈ 30 s; one active run per workflow; secret
  names ≤ 20 chars, Dashboard-only; workflows need `allowed_applications`
  by app *name*; `event-log`/`cron-schedules` are engineer-only;
  `in_draft` workflows are deleted 28 days after the last push; the push
  token is a personal 10-day device token (no cloud routine can hold it).
- **Dialpad:** webhooks cannot carry custom headers (no Sandy ingress);
  Stats export ids expire in ~1 h and results are cached by parameters;
  single-day exports are hourly; 429 under parallel polling; export
  `call_id` = entry-point id.
- **Gemini:** only 2.5 Flash listens to audio on our key; thinking counts
  against output tokens; 429 bursts; retirement date unannounced.
- **Claude via the AI Gateway:** `/v1/messages` passthrough only (batches
  and `cache_control` passthrough unverified — `CostOptimization.md` §4);
  streaming required at judge `max_tokens`; ~1/29 replies need JSON salvage.
- **Pulpo:** 60/min and 2,000/day per token; 500-char query cap; tags are
  the only scoping mechanism; corpus curated outside this team.
- **GAS/clasp:** `rootDir` no-op trap; `clasp deploy -i` drops the web-app
  entry point unless the manifest has a `webapp` block; GAS cannot call
  Sandy (SSO), so Sandy pushes payloads to GAS; HR GAS authenticates with
  the team API key (rotate in lockstep).
- **Data doctrine:** Sandy-born rows in any Railway-parity table own
  explicit high-range ids (≥ 10,000,000; child tables too); the D1 mirror
  is UPDATE-only during shadow; PII stays out of D1 audit tables.

## 5. Open asks to Engineering (unchanged, from `PortManifest.md` §12 / `RailwayRetirement.md` §7)

1. Webhook ingress for Dialpad (or a header-less allow-listed route).
2. `sast_` App Service Token for GAS/HR/Aria callers (blocks Aria C2 and
   the push double-write).
3. Per-team AI-Gateway cost attribution.
4. D1 PII / retention stance.
5. The cron dispatch time bound value, and whether callback requests carry one.
6. Railway billing shutdown once `RailwayRetirement.md` is signed.

## 6. Design docs map

`references/` (Sandy era): `CronContinuation` (cron/ticker, incident) ·
`NightlyScoring` (MS sweep) · `RetrievalScope` (Pulpo scoping, drift
decomposition) · `CostOptimization` (CO0, decisions pending) ·
`AriaIntegrationSpec` (evals API + PID calibration) · `CoachingLoopSpec`,
`CoachingTagsSpec` (coaching, both shipped) · `DailyDigest` (v0.78) ·
`SupervisorDeliverables` (v0.77) · `ShiftReport` (EOD sheet) ·
`SofiaRetellSpec`, `RetellAPI` (Sofia provider) · `SalesHumanReview` ·
`AgentAddition` (roster) · `NightWatch` (runbook) · `RailwayRetirement`
(kill sequence, unsigned) · `PortManifest.md` (port map + sit-down list).
`qa-automation/AI-Scoring/references/` (Railway era): `TwoStageScoringDesign`
(annotate/judge doctrine), `PulpoConnection`, `DispositionDesign`,
`SandyMigration`, `HRBonusSheet`, `ScorecardActionsDesign`,
`LandingOpsCommandCenter` (Phase 5 = local AI), `Phase*/Wave2/Cutover/
ReadPathFlip` (historical). `database/SQLMigration.md` (schema v1.4 incl.
the Qwen2-Audio + Gemma cascade note), `BackfillPlan.md`.

---

## Change log

> v0.78 : 2026-09-17
- Daily Agent QA Digest (references/DailyDigest.md): the nightly sweep's
  suppressed per-call scorecards are replaced by ONE email per active agent
  per sweep night — tonight-vs-month per-section averages, a Sonnet-written
  trend summary (qa-insights mode `daily_digest`, judge parity), the
  ProgressionCard strip (last 5 + tonight) and the plain month-to-date
  average (HR-bonus window). Migration 0021 (`qa_agent_digests`,
  `qa_cron_jobs.next_at`, MS `nightly_sweep.digest` config); `dailyDigest.ts`
  (facts, prompt, renderer, `daily_digest` cron-job handler); sweep done →
  digest job; `JobStepResult.wait_s` parks a waiting job instead of spinning
  the ticker. GAS gains a generic `{html_email}` sender branch
  (qa-automation/src/Main.js — push `qa-member-support`).
- Tests: `tests/daily_digest.test.mjs`.

> data migration 0020 : 2026-09-17 (no app deploy)
- Judge calibration — Member Support **Process Adherence is scoped
  exclusively to the retrieved SOP documents** (migration
  `0020_process_adherence_sop_scope.sql`, applied to live D1). The section's
  `rubric_question` now carries the scope rule (do not re-score greeting,
  verification, tone, holds, pacing, resolution — their own sections own
  those) and `special_reasoning_instructions` asks the judge to name the SOP
  document + step the score rests on. Call Resolution untouched (scores the
  outcome). Anchors, weights and `rubric_version` (member_support_v2)
  unchanged; picked up by the next enqueue.

> v0.77 : 2026-09-16
- Supervisor Deliverables (references/SupervisorDeliverables.md): migration
  0019 (`qa_sup_reports`, `qa_sup_pulls`, `qa_sup_action_plans`,
  `qa_sup_events`, `provider_config.deliverables`); facts library
  (`supervisorFacts.ts`), renderers (`supervisorRender.ts`), Snowflake
  gateway client (`snowflakeMcp.ts` — MC ticket counts, WFM parity), Slack
  seam (`slack.ts` — post + unattended-mention audit), routes
  (`routes/supervisor.ts`), three pages, "Supervisor" nav button, ticker
  step 3b (weekly Dialpad productivity + CSAT pull). New optional secrets:
  `SLACK_BOT_TOKEN`, `SLACK_USER_TOKEN`, `SNOWFLAKE_MCP_TOKEN`.
- Tests: `tests/supervisor.test.mjs` (period math, abandon rollup, QA
  coverage, coaching activity, plans, pull state machine, tracker,
  renderers).

> v0.76 and earlier
- See git history (`git log --oneline -- sandy-qa`) and the per-feature specs
  in `references/` (CronContinuation, NightlyScoring, ShiftReport,
  CoachingLoopSpec, CoachingTagsSpec, AgentAddition, CostOptimization, …).
