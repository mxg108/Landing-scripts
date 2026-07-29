# QA Scoring Platform → Sandy: Engineering Briefing

**Author:** Max Pérez (QA / AI Scoring)
**Date:** 2026-07-29
**Audience:** Engineering (Sandy platform team) + management
**Purpose:** Working session prep — align on the Railway → Sandy migration: what the QA app is, where the two platforms genuinely differ, the five hard problems, the phased roadmap, and the specific accommodations we need from each other.

---

## 1. Executive summary

The QA Scoring app is a **production system, not a prototype**: it scores Member Support and Sales calls with a two-stage AI pipeline (Gemini audio annotation + Claude judging), feeds HR bonus calculations, drives coaching workflows, and is the system of record for all evaluation history (Postgres, both teams, fully backfilled and verified as of mid-July).

Migrating it to Sandy is **a re-platforming, not a lift-and-shift**: ~15,300 lines of Python backend plus ~6,000 lines of frontend must be rewritten in TypeScript (Sandy Apps + Workflows), and the Railway Postgres database translated to Sandy's D1 (SQLite). Realistic calendar: **2–3 months**, run in parallel with Railway ("shadow" operation) until the two deployments are verified mirror images — only then do we announce and disconnect Railway.

Three things make this briefing necessary rather than just doing it quietly:

1. **This app pre-dates Sandy and was developed in parallel with it.** Its roadmap (webhook-driven dispositions, hold-time capture, AI CSAT, coaching/progression analytics) was scoped with the Ops VP and Member Support Direction before Sandy was an option. The goal is mutual adaptation — the app adapts to Sandy's architecture, and Sandy accommodates a small number of production needs — not discarding scoped features to fit a prototype mold.
2. **A few platform gaps need Engineering's help** — most critically webhook ingress (Dialpad cannot send custom auth headers) and outbound allow-list entries. Section 7 is the concrete ask list.
3. **The riskiest work isn't code** — it's data recoverability (historic Dialpad disposition exports) and parity verification during shadow operation. We have a tested playbook for both from the July Sheets→Postgres migration.

---

## 2. Background: what the app is and how we got here

**For the manager-level view:** every scored call flows Dialpad → AI scoring pipeline → scorecard → analyst review → email to agent + history dashboards. The app serves 7 web pages (scoring console, team dashboard, agent history, call lookup, scorecards), exposes an API used by Google Apps Script (email delivery, HR bonus workbook), and runs a periodic pull of Dialpad call dispositions.

**Recent history (why the timing is what it is):**

| When | Milestone |
|---|---|
| Jul 1–7 | Postgres cutover complete — engine scores from DB for both teams |
| Jul 11–16 | Historical backfill B0–B4 executed and verified (1,657 MS + 328 Sales evaluations), read-path flipped to Postgres, legacy Sheets read path deleted |
| Jul 19–28 | Command Center groundwork (dispositions, AI CSAT, hold intervals — migration 016/017), two-stage scoring with Claude judge shipped |
| Now | Sandy migration staging begins |

The app already runs on the modern half of its own roadmap. The migration must carry that roadmap forward, not reset it.

---

## 3. Railway vs Sandy: the major deltas

| Dimension | Railway (today) | Sandy (target) | Impact |
|---|---|---|---|
| **Language/runtime** | Python 3.14, FastAPI, long-lived server process | TypeScript worker, request→response (milliseconds); no long-lived process | Full backend rewrite; background work moves to Sandy Workflows |
| **Heavy/AI work** | In-process background tasks (30 s–3 min per scored call) | **Sandy Workflows** — durable jobs designed exactly for this (a call-transcript-scoring template already exists) | Architecture actually improves: jobs survive restarts |
| **Database** | Postgres (5 schemas, JSONB, timestamptz) | D1 = Cloudflare's managed SQLite; one DB per app; JSON and timestamps stored as text | Schema translation + data import; volumes are small (~2k evaluations), capacity is not a concern |
| **Real-time dashboard** | Server push (SSE) | Not possible in request→response workers | Replaced with 30 s polling — **accepted** (only one event type exists) |
| **Scheduled jobs** | In-process loop, 30-min disposition pull | App cron: max 2 schedules, hourly or daily | Hourly disposition pull — **accepted** |
| **Auth** | API keys in links/headers | Google SSO (hellolanding.com) on every page + App Service Tokens for machine callers | Security upgrade; agents log in with Google when opening scorecard links — **accepted** |
| **Inbound webhooks** | Any route can receive them | SSO wall blocks header-less external POSTs | **Open problem — needs joint design (§5.6, §7)** |
| **Deploys** | Auto on merge to main (watch-path filtered) | No git integration; scripted push/publish with a personal 10-day credential | We build a push layer mirroring our existing `push.sh` discipline (§6, Phase 3) |
| **Secrets** | Env vars, any name, CLI-settable | Dashboard-only, names ≤ 20 chars | Rename map; some dynamic env-var patterns redesigned |
| **Outbound calls** | Unrestricted | Strict allow-list (with auto-injected auth for known APIs) | Need entries for Gemini, Google Sheets/OAuth, Apps Script, Pulpo (§7) |

**Accepted user-visible deltas (decided, so nobody relitigates them mid-project):** dashboard updates by 30-second polling instead of instant push; Google SSO on scorecard/dashboard links; disposition freshness hourly instead of every 30 minutes.

---

## 4. Guiding principles

1. **Data safety first.** Consolidated export of everything (database dumps + laptop-only backfill artifacts + sheet snapshots) delivered to Engineering-controlled storage before any risky work. Months of verified history must never depend on one machine or one mistake.
2. **Strangler, not big bang.** Railway keeps running and deploying unchanged throughout. Sandy is built up alongside it, page by page, pipeline by pipeline.
3. **Railway is the oracle.** The ~20k lines of Python tests don't port. Instead, the same golden-fixture + parity-harness approach that validated the July read-path flip validates the TypeScript port: same inputs into both stacks, byte-compared outputs.
4. **No announcement until mirror.** Shadow operation runs double-writes with nightly reconciliation; we cut over only after an explicit exit checklist passes (§6, Phase 5).
5. **Mutual adaptation.** The app absorbs the rewrite; Sandy absorbs a short list of production accommodations (§7). Neither side hard-forces the other.

---

## 5. The five hard problems (and one joint design problem)

Each stated twice: what it means technically, and why it matters in plain terms.

### 5.1 Audio through Workflows — the go/no-go gate

**Technical:** scoring is audio-first by policy (audio is the source of truth, especially for Spanish calls). Today the pipeline downloads the Dialpad recording to a temp file and uploads it to Gemini. Workers have no filesystem; the download→upload must stream through memory inside a Sandy Workflow, and Workflow payload/duration limits for multi-megabyte audio are not yet documented to us.

**Plain terms:** the single most important unknown. If a Workflow can score one real call end-to-end, the whole architecture works; if not, we redesign before writing anything else. **Phase 0 is a one-call spike that answers this in days, not months.**

### 5.2 The statistics engine

**Technical:** team stats, agent trend series (EWMA/SPC), and HR bonus math run on pandas/numpy DataFrames — Python libraries with no TypeScript equivalent. Port target: SQL aggregations in D1 + explicit TypeScript math, verified byte-for-byte against golden fixtures captured from production.

**Plain terms:** the numbers on dashboards and in HR bonus sheets must come out *identical* after the rewrite. We have the comparison harness that proves it (it caught real drift during the July migration); the port is tedious but verifiable.

### 5.3 Postgres → D1 translation

**Technical:** five Postgres schemas collapse into one SQLite database; JSONB and timezone-aware timestamps become text; cross-schema reads (`qa` ↔ `command_center`) become same-DB joins; migrations run manually via the Sandy CLI. The `embeddings` schema is explicitly out of scope (per Ops VP decision; D1 has no vector support and Pulpo now owns retrieval).

**Plain terms:** the filing cabinet changes brands. Everything fits (our data is small); the risk is translation errors, which the shadow-phase reconciliation is designed to catch nightly.

### 5.4 The runtime model — real-time features and schedules

**Technical:** the in-process event bus, SSE stream, in-memory job store, and startup-time version shipping all assume one long-lived server. On Sandy: polling replaces SSE (conveniently, exactly one event type with one consumer page exists), Workflow run records replace the in-memory job store, and rubric/formula version shipping moves from "on process start" to an explicit deploy step.

**Plain terms:** several small conveniences of "one always-on server" disappear. Each has a clean, already-identified replacement; the deltas users can see are the three accepted ones in §3.

### 5.5 Deployment reality — no deploy-on-merge

**Technical:** Sandy has no git integration or headless deploy credential; pushes use a personal 10-day token, and secrets are dashboard-only. We will build `sandy_push.sh` mirroring our existing Apps Script `push.sh` (manifest registry, hermetic build, changed-path detection reproducing Railway's watch-path behavior, append-only audit log, deploy↔commit-SHA linkage) as a disciplined post-merge step.

**Plain terms:** today a merge deploys automatically; on Sandy a human runs one command after merge. Acceptable for our team size — but it is a real operational difference Engineering should know we've planned around, and a standing item if Sandy ever grows CI hooks.

### 5.6 Webhook ingress — the joint design problem ★

**Technical:** Dialpad webhooks (call events, dispositions, AI CSAT, hold intervals) sign their payloads with a shared-secret JWT but **cannot attach custom HTTP headers**. Sandy's SSO wall admits machine callers only via the `X-App-Service-Token` header — so Dialpad's POSTs cannot reach a Sandy app at all today.

**Plain terms:** webhooks are not a nice-to-have; they are the designed backbone of the app's next phase (automatic disposition filling, hold-time capture, AI CSAT — all scoped with Ops VP and MS Direction, schema already shipped in migrations 016/017). The interim stats-polling path works for the migration itself, but the platform needs an ingress answer — e.g., a path-scoped inbound rule that accepts a token in the URL or validates the webhook's JWT signature at the edge. **This is the centerpiece request of the sit-down, and the core reason I'm requesting Engineer access (§7).**

---

## 6. Phased roadmap

Each phase names the hard problems (§5) it retires. Estimates assume this runs alongside normal QA duties.

### Phase 0 — Prerequisites & the spike *(days)* → retires §5.1, de-risks §5.5
- Sandy re-auth; verify role, app quota, outbound allow-list state, Workflow limits.
- **The spike:** one real call scored end-to-end in a Sandy Workflow (Dialpad download → Gemini annotate → Claude judge → D1 write). Go/no-go for the whole architecture.
- Request App Service Tokens + allow-list entries (§7); confirm permanence of the current global outbound rule.
- Repo hygiene: merge in-flight work; freeze discipline agreed.

### Phase 1 — Data completion & consolidated handoff *(~1 week)* → insurance for everything
- **Test Dialpad Stats-export depth for January first** — if history that old can't be exported, every week of delay shrinks the recoverable window. Then enable Sales dispositions (target→team mapping, Sales call-center wiring, migration 017 verification, historic fill to whatever depth Dialpad serves).
- Build the handoff bundle: Postgres dumps (`qa` + `command_center`), the laptop-only backfill artifacts (seed CSVs, staging files, all verification run reports), sheet snapshots, manifest README → **Engineering-controlled storage, not git (PII)**.

### Phase 2 — Port manifest & staging workspace *(~1 week)* → plans §5.2 + §5.3
- The Python app stays exactly where it is (Railway keeps deploying it). The "migration folder" is the **new** `sandy-qa/` workspace: Sandy app scaffold + a port manifest mapping every module and page to its target (worker route / Workflow / cron / D1 table / deliberately dropped). Design-doc-first; the manifest is reviewed before TypeScript is written.

### Phase 3 — The push layer *(~1 week, overlaps Phase 2)* → solves §5.5
- `sandy_push.sh` as described in §5.5, plus deployment-advisory surfacing and token-freshness preflight.

### Phase 4 — The port, strangler-ordered *(3–5 weeks)* → executes §5.1–5.4
- D1 schema + initial data import → read-only pages first (dashboards, lookup, history) → scoring Workflow → review/approval/override actions (the audited paths last) → Apps Script + HR bonus integration via service tokens → crons (hourly disposition pull; one daily maintenance cron).
- **Roadmap continuity built in:** the port carries the coaching and progression data model forward and **formalizes it — first-class timestamps and lifecycle states on coachings, coaching→evaluation links, and agent progression snapshots — so continuous-training effectiveness becomes measurable** (time-to-improvement after coaching, progression deltas per coaching cycle). This was scoped pre-Sandy; the migration is the natural moment to make it structural.
- Sequencing guard: write-path work holds until the **Aug 1 HR bonus run** completes on Railway; Python change-freeze during shadow.

### Phase 5 — Shadow operation *(2+ weeks)* → proves §5.2 + §5.3, validates §5.4
- Railway double-writes every finalized evaluation to Sandy (service-token API); failures logged loudly **and queued for replay** — a silent double-write failure is the one thing that can invalidate shadow results.
- Nightly reconciliation: row counts + content checksums per table per day, Postgres vs D1. Same calls shadow-scored on both stacks, scorecards diffed.
- **Exit checklist (the definition of "mirror"):** N consecutive zero-drift reconciliation days · stats parity on both teams · all 7 pages + HR run + email flow verified on Sandy · runbook written. Only after this passes do we announce.

### Phase 6 — Cutover & decommission *(days + 30-day safety window)*
- Freeze Railway writes → final delta import → repoint Apps Script/HR/scripts → monitor → Railway kept dormant ~30 days → disconnect.

---

## 7. What we're asking of Engineering

| # | Ask | Why | When |
|---|---|---|---|
| 1 | **Webhook ingress design session** (§5.6) — path-scoped inbound rule with URL-carried token, or edge validation of Dialpad's JWT signature | Dialpad can't send custom headers; webhooks are the app's designed backbone | At the sit-down |
| 2 | **Engineer role** for me on Sandy | Self-serve allow-list management, app administration, event-log visibility during a 2–3-month migration with weekly platform touches; webhook/inbound-rule work | Next week |
| 3 | **App Service Tokens**: Apps Script → Sandy (email/HR callers), Railway → Sandy (shadow double-write) | The two machine-to-machine seams of the migration | Phase 0 |
| 4 | **Outbound allow-list entries** (if not covered): Gemini API, Google OAuth token endpoint + Sheets API, our Apps Script web-app URL, Pulpo MCP | The app's existing integrations | Phase 0 |
| 5 | **Confirm permanence of the current global outbound rule** ("Allow Everywhere") or commit to explicit rules before it's removed | Strangler pattern depends on Sandy→Railway calls during transition | At the sit-down |
| 6 | **Workflow limits documentation** (payload size, duration, memory) for the audio spike | §5.1 go/no-go | Phase 0 |
| 7 | **Storage location for the data-safety bundle** (PII — not git) | Phase 1 handoff | Phase 1 |

---

## 8. What Engineering gets in return

- A real production workload proving out Workflows, D1, service tokens, and the cron system — with a written parity methodology Sandy can reuse for future migrations.
- Retirement of a Railway dependency (cost + platform consolidation).
- A documented push-layer pattern (`sandy_push.sh`) other monorepo teams can adopt.
- A concrete, well-specified webhook-ingress use case to design the platform feature against — better than designing it in the abstract.

---

*Companion documents: `BackfillPlan.md`, `ReadPathFlip.md`, `DispositionDesign.md`, `CutoverDesign.md` (this directory / `database/`). Full platform constraint audit available on request.*
