# Sandy Port Manifest — `qa-scoring`

**Status:** DRAFT — review before any TypeScript is written (SandyMigration.md Phase 2 gate)
**Date:** 2026-08-02
**Frozen baseline:** `main` @ `ea73ec4` (PR #162) — Python change-freeze declared 2026-08-02
**Sandy app:** `qa-scoring` (`a2cc5b5a-df29-4ae7-9dbb-e270052015e7`) → https://qa-scoring.sandy.hellolanding.tech
**D1 database:** `sandyapp-qa-scoring` (`15a839da-d11e-4914-87da-d4fa16e6ec4b`)
**Spike evidence:** workflow `qa-audio-spike` (`26b2a3dd-718b-41e4-9337-d74abaf5c53b`) — GO on 3.4-min and 16-min real calls (2026-08-02); Dialpad download, Gemini Files upload, 2.5-flash annotate, Claude judge via AI Gateway all verified inside a Workflow.

This manifest maps every module, page, endpoint, table, and env var of
`qa-automation/AI-Scoring` (15.6k LOC Python + 5.7k LOC HTML) to its Sandy target —
or names it deliberately dropped. Companion doc: `qa-automation/AI-Scoring/references/SandyMigration.md`
(the phased roadmap and the five hard problems; this manifest executes its Phase 2).

---

## 1. Freeze declaration & branch dispositions

Railway keeps serving production, deploys ONLY critical fixes from `main` (watch-path
`qa-automation/AI-Scoring/` unchanged). Feature work on the Python stack stops as of the
frozen baseline; the parity harness treats that baseline as the oracle.

Unmerged branches, triaged 2026-08-02:

| Branch | Verdict | Rationale |
|---|---|---|
| `feat/cc-land-c2-c5` | **DO NOT MERGE — knowledge carried here (§10)** | The real Dialpad subscription enum (`transcription`, not design-era `call_transcription`; `dispositions` + `csat` are dedicated states — audited 2026-07-19 against engineering's prod subscription `4859989923299328`). Subscription creation is blocked on the webhook-ingress design anyway; the enum belongs in the Sandy webhook implementation when that lands. |
| `feat/qa-scoring-non-english-audio` | **DO NOT MERGE — spec carried here (§10)** | Mixed-language prompt rule. It is a scoring-behavior change: merging mid-freeze would move the parity oracle. The rule enters the TS prompt port instead (it agrees with the 2026-07-19 owner rule: audio is SOT, branch on eval language). |
| `feat/sql-migration-test-scaffolding` | **DROP — relic** | Tests for migrations 004–008 (long applied) + a y/N prompt on the local runner. Python tests do not port (parity harness replaces them); the runner retires with Railway. Branch can be deleted. |

## 2. Topology

**One app** (`qa-scoring`) + **one D1** + N **Workflows** + ≤2 crons.
The five Postgres schemas collapse into the single D1 (per SandyMigration §5.3), which also
resolves the `qa.evaluations ↔ command_center` cross-schema FK by making it a same-DB join.
`embeddings` (migration 007) is out of scope (Ops VP decision; Pulpo owns retrieval).
`mass_notifications` (migrations 001/002) is NOT part of the QA app — it stays wherever its
own product goes; out of scope here.

## 3. Page map (SSR React, Landing design system, no client bundle)

| Legacy page (`frontend/`) | Route(s) today | Sandy target |
|---|---|---|
| `index.html` (scoring console) | `/`, `/score/{team_id}` | `src/pages/ScoringConsole.tsx` |
| `scorecard.html` | `/scorecard/{team_id}/{job_id}` | `src/pages/Scorecard.tsx` |
| `dashboard.html` (agent history) | `/dashboard`, `/dashboard/{team_id}`, `/dashboard/{team_id}/agent/{name}` | `src/pages/AgentDashboard.tsx` |
| `onepager.html` | `/dashboard/{team_id}/onepager/{name}` | `src/pages/OnePager.tsx` |
| `team_dashboard.html` | `/dashboard/{team_id}` (team view) | `src/pages/TeamDashboard.tsx` — **SSE toast → 30 s meta-refresh/poll (accepted delta)** |
| `team_evals.html` | `/dashboard/{team_id}/evals` | `src/pages/TeamEvals.tsx` |
| `datapoint.html` | `/datapoint/{team_id}/{call_id}`, `/datapoint/{call_id}` | `src/pages/Datapoint.tsx` |
| `lookup.html` | `/lookup/{team_id}` | `src/pages/Lookup.tsx` — stays cross-team by design (no nav link; team scoping is audit-only) |
| `frontend/static/` | — | inline into components / `src/design-system` |

Routing: `url.pathname` switch in `src/index.tsx` fetch handler (template pattern), plain `<a href>` navigation.

## 4. API map (worker routes)

Machine callers (GAS email/HR, Railway double-write during shadow) authenticate with App
Service Tokens (`X-App-Service-Token`); humans ride Google SSO. `API_KEY_{TEAM}` header
auth retires.

| Legacy endpoint | Sandy target |
|---|---|
| `GET /api/health` | keep as-is (worker route) |
| **scoring.py** `GET /calls`, `POST /score`, `POST /score/batch`, `GET /score/{job_id}`, `DELETE /score/{job_id}`, `POST /score/{job_id}/approve`, `/rescore`, `/override`, `POST /datapoints/{call_id}/edit`, `GET /review-queue`, `GET /whoami` | Worker routes; `POST /score` triggers the **scoring Workflow** (§6) and returns `run_id`; job status reads from D1 `workflow_runs` (replaces in-memory `_jobs`). Approve/rescore/override keep the scorecard-actions doctrine (≤50 auto-rescore once; override creates coaching receipt). |
| **team.py** `GET /stats`, `/evals`, `/long_form`, `/sections`, `/mails` | Worker routes over D1 SQL + TS math (§5 stats port). `long_form` + `coverage_regime` are deliberate predictive-analytics groundwork — port, do not prune. |
| **dashboard.py** `GET /agents`, `/agents/{name}/history`, `/onepager`, `/progression` | Worker routes over D1. |
| **datapoints.py** `GET /datapoints`, `GET /datapoints/{call_id}` | Worker routes. |
| **lookup.py** `GET ""`, `GET /calls`, `POST /recording-link`, `GET /scoring-permission` | Worker routes; Dialpad calls go direct (outbound rule required if "Allow Everywhere" is ever removed). |
| **hr_bonus.py** `GET /{month}` | Worker route, service-token gated (GAS HR workbook caller). |
| **events.py** `GET /events/stream` (SSE) | **DROPPED** — one event (`eval_approved`), one consumer; replaced by dashboard poll (accepted delta). |

## 5. Service module map (`backend/services/`)

| Module | Target |
|---|---|
| `scoring_service.py` | **Workflow** `qa-scoring-pipeline` (§6) + thin worker glue |
| `audio_service.py` | Workflow audio leg — spike-proven shape: whole leg in ONE `step.do` (audio bytes cannot cross checkpointed step returns); Gemini Files API resumable upload via fetch; **carry `thinkingConfig.thinkingBudget` (4096)** — spike reproduced the thinking-eats-budget failure; carry salvage + trailing-comma + finish-reason diagnostics from the Python |
| `judge_service.py`, `llm/` | Workflow judge leg via AI Gateway **Anthropic-native passthrough** (structured output; pin `anthropic/<model>`, never `dynamic/sandy-workflows`); Gemini judge variant direct. `{STAGE}_MODEL_PROVIDER` env pattern → explicit config table in D1 |
| `team_stats.py`, `team_source.py`, `stat_points.py`, `score_compute.py` | **Hard problem §5.2** — pandas/numpy → D1 SQL aggregations + explicit TS math (EWMA/SPC hand-ported); golden-fixture parity gate before cutover |
| `hr_bonus_service.py` | TS port (plain-sum formula); parity-gated against July EOM output |
| `data_provider.py`, `db_provider.py`, `eval_store.py`, `assessment_store.py` | Drizzle repositories over D1 |
| `dialpad_client.py` | TS port (fetch); semaphore → sequential calls or queue; rate-limit handling kept |
| `disposition_pull.py` | **Cron #1 (hourly, accepted delta)** → triggers stats-pull Workflow |
| `history_service.py`, `progression_service.py`, `onepager.py`, `annotation_render.py` | TS ports (page data assembly) |
| `sheets_service.py`, `sheets_projection.py` | Port ONLY the live write-projections (analyst history, HR workbook feeds); legacy read paths are already deleted |
| `event_bus.py` | **DROPPED** (with SSE) |
| `read_path_shadow.py` | **DROPPED** — Sheets-cutover artifact, its job is done |
| `version_ship.py` | Moves out of process start → `sandy_push.sh` deploy step (Phase 3) |
| `rule_engine.py`, `data_normalization.py` | TS ports; B1 note: normalization must stay manager-UI-reconfigurable (Sales v2 sign-off) |
| `cc_context.py` | TS port reading same-DB CC tables |
| `rag/` | **DROPPED** — superseded by Pulpo MCP (RAG v2) |
| `middleware/`, `config/` | SSO identity (CF Access JWT) + service tokens replace API-key middleware; team JSON configs port as-is |
| `scripts/`, `backend/scripts/` | Reviewed individually in Phase 4; ops scripts stay laptop-side against D1 export or retire |

## 6. Workflows & crons

| Workflow | Content |
|---|---|
| `qa-scoring-pipeline` | Per call: Dialpad meta+download → Gemini annotate (Stage A) → judge (Stage B, two_stage per team config) → score compute → callback → D1 write. Single-audio-leg step shape from the spike. Fallback semantics preserved (annotation failure → single-stage). |
| `qa-stats-pull` | Hourly disposition/stats ingestion (from `disposition_pull.py`), writes D1 directly, no callback. |

Crons (max 2, fixed minute): hourly stats-pull trigger; daily maintenance (prune
`workflow_runs`, mail digests if kept).

## 7. D1 schema translation (migrations 004–018)

One migration set under `sandy-qa/migrations/`, applied via `sandy.py db migrate`.
Rules: `TIMESTAMPTZ` → TEXT ISO-8601 UTC; `JSONB` → TEXT (serialize in TS);
`SERIAL` → `INTEGER PRIMARY KEY AUTOINCREMENT`; schema prefixes become table prefixes
(`qa_evaluations`, `cc_webhook_events`, …); views (015) → D1 views; partial indexes kept.
Out: 001/002 (mass_notifications), 003 (n/a), 007 (embeddings).
Data import: Postgres dump → transform script → INSERT batches, verified by the same
row-count + checksum reconciliation used in the July backfill (B0–B4 playbook).

## 8. Secrets map (≤20 chars, Dashboard-only for apps; workflow secrets via CLI)

| Legacy env | Sandy home | New name |
|---|---|---|
| `GEMINI_API_KEY` | workflow secret | `GEMINI_API_KEY` (14) ✓ set on spike |
| `DIALPAD_API_KEY` | workflow + app | `DIALPAD_API_KEY` (15) ✓ set on spike |
| `ANTHROPIC_API_KEY_*` | **retired** — AI Gateway `AI_GATEWAY_TOKEN` (org-injected) |
| `API_KEY_MEMBER_SUPPORT` / `API_KEY_SALES` / `API_KEY_PRIVILEGED` | **retired** — SSO + App Service Tokens |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | app secret, split | `GOOGLE_SA_JSON` (14) — verify size limit; else key-parts |
| `GOOGLE_SHEETS_ID{,_SALES}`, `GOOGLE_SHEETS_QA_BONUS_ID`, tabs | D1 config table (not secret) |
| `APPS_SCRIPT_WEBAPP_URL{,_SALES}` | D1 config table |
| `PULPO_MCP_TOKEN` / `PULPO_MCP_URL` | app secret `PULPO_MCP_TOKEN` (15) + config |
| `WEBHOOK_ID/SECRET/URL` | deferred to webhook-ingress design (Engineering) |
| `DATABASE_URL` | gone (D1 binding) |
| `{STAGE}_MODEL_PROVIDER`, `ANNOTATOR_*` dynamic patterns | D1 config table, manager-editable |

## 9. Dropped / replaced — summary

SSE + event bus → 30 s poll · in-memory `_jobs` → Workflow runs + D1 · `version_ship`
startup hook → push-layer step · API-key auth → SSO + service tokens · `rag/` → Pulpo MCP ·
`read_path_shadow` → deleted · Python tests (20k LOC) → golden-fixture parity harness vs
Railway · `embeddings` schema → out of scope · webhooks → deferred to Engineering ingress
design (stats-pull is the interim).

## 10. Unlanded knowledge carried from frozen branches

1. **Dialpad subscription enum** (from `feat/cc-land-c2-c5`): `ringing, connected, hold,
   hangup, recording, transcription, recap_summary, dispositions, csat` — use when Sandy
   webhook ingress lands; fixes the CC v1 subscription-400.
2. **Mixed-language rule** (from `feat/qa-scoring-non-english-audio`), for the TS prompt
   port: audio is SOT; non-English → transcript unreliable; mixed English/other → all audio
   is authoritative, score from the full audio.

## 11. Port order (Phase 4, strangler) & gates

1. D1 schema + data import (reconciliation green) →
2. Read-only pages: Lookup, Datapoint, TeamEvals, AgentDashboard/OnePager (visual parity vs Railway) →
3. Stats endpoints (golden-fixture byte-parity gate) →
4. Scoring Workflow + console (shadow-score same calls both stacks, scorecard diff) →
5. Review actions approve/rescore/override (audit-trail parity) →
6. GAS + HR integration via service tokens →
7. Crons.
Write-path work only after the freeze-period HR runs stay on Railway (next: Sep 1).

## 12. Open items blocking later phases (Engineering sit-down)

Webhook ingress design ★ · Engineer role · App Service Token provisioning ·
outbound-rule permanence ("Allow Everywhere") or explicit entries (Gemini, Google OAuth/Sheets,
Apps Script, Pulpo) · Workflow limits doc (payload/duration/memory) · PII storage location for
the Phase 1 handoff bundle · secrets-blob visibility in workflow run logs (observed in
`fetch-run-secrets-1` step output — confirm it is ciphertext and intended).
