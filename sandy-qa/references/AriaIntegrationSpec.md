# Aria ↔ QA Scoring — integration + PID calibration (spec)

*Design doc 2026-08-12, responding to Jackson's scoping doc (Google Doc
`1zOUlBMbmfLql…`, 2026-08-11) §-by-§, grounded in a pull of Aria v0.189
(app 976cf5b1, aria.sandy.hellolanding.tech, D1 9a30c0dc). Companion to
[SofiaRetellSpec.md](SofiaRetellSpec.md). Doc-first; code follows sign-off.*

## 0. The two lenses (Jackson §2b, agreed framing)

- **QA Scoring asks:** "Is this a good call, full stop?" — Sofia graded 1:1
  on the human-agent rubric, same bar, no AI leniency (Conor & Joao,
  2026-08-11).
- **Aria asks:** "Is this a good call *from an AI agent*?" — human-likeness,
  latency, structural/technical gaps; derived **from our data only**.
- Aria never displays a competing score; our scorecard stays authoritative.

## 1. Jackson's asks vs. what already exists (audit)

| Ask (doc §) | Current state | Gap |
|---|---|---|
| §2a daily auto-submission | **R4 already ships HOURLY auto-pull** (26h window, voicemail/short-call skip, one-attempt idempotency; review gate untouched) — doc predates it by a day | none — exceeds ask |
| §2b 1:1 human rubric | sofia_v0 scores 5 numeric + escalation; flow/tone are §4.1 audio-dependent sections | **rubric v1**: drop flow/tone from the score; keep SOP, Accuracy, Intent (1–5) + Escalation (Y/N/NA); reweight formula |
| §3 real API | server-rendered only; no REST surface for services | **/api/v1/evals** list+detail (below) |
| §3 auth | — | **Inbound App Service Token** (`X-App-Service-Token: sast_…`) — Sandy's sanctioned service auth THROUGH CF Access; identity arrives as `svc-<name>@sandy…`, path-regex-scopable. **Engineering provisions it (Dashboard → Inbound Rules)** — sit-down dependency |
| §4 telemetry | `computeSignalMarkers` already: response gaps p50/p95, interruptions, silences >3s, talk ratio, Retell latency percentiles — persisted in `dialpad_call_metadata` | widen to full per-turn latency series + dead-air total + notable-moment list; STT/TTS confidence **only if Retell exposes it** (transcript_object carries word timings, not confidence — flagged) |
| §4 grounding quotes | reasoning is model narration | prompt+schema change (rubric v1): score ≤3 ⇒ verbatim quote + mm:ss; SOP Adherence cites doc title (we already persist `sop_used` + `pulpo_docs` provenance — surface per-section) |
| §6 progression chart | `/dashboard/sofia/agent/Sofia` exists (EWMA, trends) | JSON series endpoint under the same API for Aria to render (iframe would drag CF Access sessions into their page) |
| §7 schema versioning | rubric versions exist in D1 (`qa_rubric_versions`) but no changelog discipline, no API field | `schema_version` on every API response + `qa_rubric_changelog` (rationale rows, mirroring Aria's `rubric_versions` table) |
| §8 weekly calibration | per-section **confidence markers persisted on every eval** — planted for exactly this | the shared mechanism (below, §3) |

Aria-side facts that shape our build: grades carry `deviation_quote` +
`*_verified` flags (programmatic substring checks — the grounding bar we
should meet, not just meet halfway); `composite_score` is 0–100 "so a
future Max-integration comparison has a shared scale" (their comment);
golden-set `calibration_mode` rides a Monday 11:00 UTC tick; MCP surface
at `/api/v1/mcp` is human-SSO-only by design (not our transport).

## 2. The evals API (Jackson §3)

```
GET /api/v1/evals?since=<iso>&status=finalized&limit=50&cursor=<id>
GET /api/v1/evals/{call_id}
```
- **Finalized only** on the list — Aria never ingests provisional scores.
- Detail = everything we compute, not just what the scorecard renders:
  section scores + reasoning + **confidence markers** + severity, verbatim
  grounding quotes (rubric v1), SOP citations + Pulpo provenance, full
  telemetry object, notable moments, `transcript_display`, call metadata
  (duration/direction/caller/sentiment/disconnection/`agent_version`),
  stable scorecard URL, `schema_version`, `rubric_version`.
- Auth: `X-App-Service-Token` (checked app-side via the `svc-` identity
  prefix), path-scoped to `^/api/v1/.*`. Rotation = normal Sandy token
  rotation. Until Engineering provisions it, the routes 403.

## 3. Weekly calibration as a PID loop (Jackson §8 + Max's framing)

**One build, hosted in the QA app** (we own the review-UI muscle, D1, and
cron infra), consumed by Aria over the same `/api/v1` surface. Both tools
are calibrated **symmetrically** (§9 corrected framing): the same sampler,
the same review UI, the same controller state — per-tool rows.

### 3.1 Plant, sensor, setpoint

- **Plant**: each tool's grading pipeline (prompts + model + rubric).
- **Sensor**: the weekly human calibration sample. Monday (riding our
  existing daily 09:37 cron with a weekday branch — the 2-cron cap holds),
  the sampler draws **n stratified items per tool** (default 18): strata =
  score quintiles × AI-confidence band × origin (auto-pull vs manual),
  **independent of score and of prior review status** — that independence
  is what catches new blind spots AND false positives.
- Reviewer (Max, initially) works a dedicated `/calibration` queue page:
  free-text notes + **0–100 accuracy score** per item ("how well did the
  AI's grade match what a sharp reviewer wanted").
- **Process variable** A(t): weekly mean accuracy per tool,
  **confidence-weighted** — a miss on a section the AI called `high`
  confidence weighs 1.5×, `medium` 1.0×, `low` 0.7×. Miscalibrated
  confidence is worse than honest uncertainty, and this is precisely what
  the persisted confidence markers exist to measure.
- **Setpoint** A\* = 90 (configurable per tool). Error e(t) = A\* − A(t),
  deadband ±3 (inside it, no action; integral decays 0.8×/wk).

### 3.2 Actuators — u(t) = Kp·e + Ki·Σe + Kd·Δe

| Term | Actuator | Semantics |
|---|---|---|
| **P** (+I) | **Sample size** n(t+1) = clamp(n₀ + Kp_n·e + Ki_n·I, 10, 40) | Error grows → more human eyes next week (sensor gain up). Cheap, safe, always-on. |
| **P** (+I) | **Review-gate fraction** g(t+1) = clamp(g₀ − Kp_g·e − Ki_g·I, 0, 0.9) | The §always_human_review relaxation path: sustained accuracy at setpoint lets Sofia evals auto-finalize with only a g-fraction sampled into review; ANY error drives g back toward full review. **Fail-safe direction is always "more human review".** Guarded actuator: enabled only after owner sign-off; until then it logs what it *would* do (shadow mode). |
| **I** threshold | **Meta-analysis trigger**: I ≥ T_a (e.g. 25 error-weeks) fires a workflow — Claude (sonnet per the gateway Opus restriction Aria hit) reasons over the low-scored items + reviewer notes → structured findings → **draft rubric-version bump proposal**. Human approves; an approved bump **discharges the integrator** (I := 0) — the structural fix is the integral's job, and Jackson's "learning = structured input to rubric versioning" is exactly an integrator discharging into a version bump. |
| **D** | **Drift alarm**: ΔA ≤ −10 wk/wk → immediate alert (Sofia digest line + the deferred Slack seam) and auto-tighten g → 0 (tighten-only; D never relaxes anything). Catches step changes — model swap, vendor update, prompt regression — a slow integral would sleep through. |

Anti-windup: I clamped to ±50; frozen while the guarded actuator is
disabled. Bootstrap: first 4 weeks open-loop (n=18, g=0, collect
baseline A) — no control action until the sensor has a signal history.
Few-shot grounding from the accumulated example bank (Jackson's option 3)
stays out of the loop for now — noted as a possible future feed-forward
term, decided separately.

### 3.3 State & storage (migration, QA app D1)

```
qa_calibration_items    (id, tool 'qa'|'aria', item_ref, week, strata JSON,
                         payload JSON [aria items pushed to us], status)
qa_calibration_reviews  (item_id, reviewer_email, notes, accuracy_score 0-100,
                         section_flags JSON, reviewed_at)
qa_calibration_state    (tool, week, A, e, I, n_next, g_next, params JSON,
                         actions JSON [what fired: analysis/alarm/bump])
qa_rubric_changelog     (version, released_at, summary, rationale)   [§7]
```
Aria's weekly ticket sample arrives via `POST /api/v1/calibration/items`
(same sast\_ auth); their trend reads back over `GET
/api/v1/calibration/state?tool=aria`. Trend surfaces: a `/calibration`
page + one line in the Sofia daily digest.

## 4. Build ladder (aligned to Jackson §9a)

- **C1** — rubric v1 re-scope (4 scored sections; flow/tone → telemetry),
  grounding prompts (quote+mm:ss on ≤3, SOP citation), `schema_version`,
  `qa_rubric_changelog`. Smallest, unblocks Aria's schema work.
- **C2** — `/api/v1/evals` list+detail + widened telemetry + notable
  moments. **Blocked on Engineering provisioning the sast_ token** (ask
  filed at the sit-down; routes ship 403-until-token).
- **C3** — progression JSON series endpoint (Aria embeds; Sofia's chart
  page already exists).
- **C4** — calibration host: tables, Monday sampler, `/calibration` review
  queue UI, accuracy trend, controller in **shadow mode** (computes and
  logs u(t), actuates nothing but sample size).
- **C5** — controller live: gate-relaxation actuator (owner sign-off),
  meta-analysis workflow, Aria item ingestion + state read-back.

## 5. Open questions

1. **sast_ tokens** — one (Aria→QA only, Aria pushes its sample to us) or
   two (we also pull from Aria)? Recommend one: push model, single token.
2. Rubric v1 **weights** across SOP/Accuracy/Intent/Escalation — owner
   sign-off (engine is config-driven; seed like sofia_v0).
3. Existing sofia_v0 evals keep their flow/tone sections in history —
   `schema_version` discriminates on the API; confirm Aria's ingestion
   starts at v1 (cleanest) or wants v0 backfill.
4. Retell **STT confidence** appears unavailable on transcript_object —
   confirm with Retell support or drop from §4 scope.
5. PID initial params (Kp/Ki/Kd per actuator), setpoint 90, deadband ±3,
   bootstrap 4 wk — sign-off numbers, all stored in
   `qa_calibration_state.params` (tunable without deploys).
6. Reviewer identity for Aria's items in our UI (Max per the doc) — RBAC:
   calibration page = existing `qa`/`admin` roles.
