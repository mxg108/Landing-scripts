# Railway retirement — full-repo audit & kill sequence (analysis)

*2026-08-30/31, three-agent sweep (backend runtime · database/ artifacts ·
repo-wide domain consumers) + live-D1 verification. Companion to
SandyMigration.md Phase 5/6 — this document REPLACES the original Phase 5
exit checklist (see §6) and expands the Phase 6 kill list into a runbook.
Analysis only; every action below is a proposal until Max/Engineering
sign off.*

## 0. TL;DR

Post-NS (nightly disposition sweep, v0.59/v0.60) the Railway service has
**no function Sandy lacks except two**: the HR-bonus month endpoint
(GAS reads it; Sep 1 run stays on Railway BY DESIGN; port required before
the Oct 1 run) and Dialpad **webhook ingress** (platform-blocked, the
pinned Engineering sit-down item). Everything else still running on
Railway is either duplicate write-path (analysts scoring on the old
domain — **actively leaking Max's personal Anthropic key**), projection
plumbing that dies with it (Analyst_History/Score_Audit/GAS row mode), or
shadow-era scaffolding on Max's laptop (shadow_sync, parity) to be
retired deliberately. The blocking work is small; most of the effort is
sequencing, comms, and data preservation.

## 1. What Railway still does today (audited, file:line in §refs)

| Function | Status | Sandy equivalent |
|---|---|---|
| Scoring console + lookup + dashboards (old domain) | **LIVE, analysts still using it** — `/` 307s straight into the scoring console; `API_KEY_*` bearer tokens cached in localStorage; `?api_key=` bookmarks authenticate | Fully migrated (v0.25+) |
| Stage-B Claude judge + Gemini annotate per scored call | LIVE — funds: `ANTHROPIC_API_KEY → _LANDING → _PERSONAL` chain lands on **the personal key** (only one set); Gemini on own key | Sandy scores via AI Gateway (Engineering-billed) + Gemini |
| Half-hour `disposition_pull` loop → PG `command_center.calls` | LIVE (`CC_STATS_PULL_INTERVAL_MIN`) | **v0.59 nightly sweep** (UPDATE-only during shadow; INSERT arm flips at cutover §4.5) |
| Progression assessments (`Get AI Assessment` button) | LIVE — spends + writes `qa.assessments` | CL4 insights workflow |
| Analyst_History projection + Score_Audit tab writes | LIVE (only writers) | D1 `qa_score_audit` (Sandy-born); Sheets tabs freeze at cutover — owner must accept |
| GAS scorecard email (row mode, Railway-born evals) | LIVE | Payload mode (zero sheet reads) since v0.22 |
| HR bonus `/api/{team}/hr-bonus/{month}` | LIVE — **only consumer-facing API with NO Sandy port** | none (port = blocker) |
| Dialpad webhook ingress `POST /api/webhooks/dialpad` | Registered; JWT-body auth; liveness re-checkable once the mirror catches up (tail frozen with the Aug-20 sync wedge; 102,913 events total historically) | **None — platform-blocked** (Dialpad can't send custom headers; sit-down ask #1) |
| Boot-time `version_ship` formula archive | every deploy | seeds/`db migrate` flow |
| Mails-tab runtime reads (roster identity) + `import_agents.py` | Residual (AA3-deprecated) | Sandy roster-authoritative since AA0 |

Sandy makes **zero runtime HTTP calls to Railway** (verified). The only
Sandy→Railway coupling is `shadow_sync.py`/`pg_to_d1.py`/parity reading
Railway **Postgres** from Max's laptop. Dead code: `RAILWAY_BASE` +
`interimPage` in `teamApi.ts:45/:224` (defined, never called — delete).

## 2. The credit leak — mechanics and stop options

**Confirmed active**: Railway-born evals through Aug 28 (mirror
undercounts — sync was wedged): alejandra@ (9/day, Aug 27–28),
andrei.trejo@ (→Aug 25), uriel.medina@ (→Aug 14); ~90 evals since Aug 10.
Each spends: Gemini annotate (own key, unchanged billing) + **Claude
judge on `ANTHROPIC_API_KEY_PERSONAL`** (`llm/anthropic.py:64-74` chain;
only the personal key is set; the ≥20-char filter means a placeholder in
slot 1/2 silently falls through). `two_stage_shadow` heritage note: the
shadow judge spends even when its result is discarded.

**Stop options, smallest blast radius first (Max picks):**
1. `SCORING_MODEL_PROVIDER=gemini` or `SCORING_PIPELINE=single` on
   Railway env → kills all Anthropic spend, app stays functional
   (analysts can keep mis-scoring on Railway, now on Gemini only).
2. Blank `ANTHROPIC_API_KEY_PERSONAL` on Railway → judge raises a clean
   provider error; scoring on Railway breaks loudly = forcing function to
   move analysts. (Zero-code; env is ops, not a freeze violation.)
3. Rotate `API_KEY_*` env values → 401s every cached analyst token and
   bookmarked `?api_key=` URL. ⚠ **HR bonus GAS uses the team's API key**
   (`hr_bonus.py` mounts under TEAM auth; README: team API key) — rotate
   only AFTER the Sep 1 run, or update the GAS Script Property in
   lockstep.
4. Comms regardless: tell alejandra/andrei/uriel directly + publish
   `qa-scoring.sandy.hellolanding.tech`. **No doc anywhere tells analysts
   the new URL** — `readme.txt:505` still presents
   `landing-scripts-production.up.railway.app` as the live address (fix
   it). Two hostnames exist (`hellolanding-qa.up.railway.app` in dead
   code) — confirm which one is bookmarked before announcing.

Recommended combo: (1) immediately + (4) now + (2) after comms land +
(3) after Sep 1.

## 3. Blockers before "freeze Railway writes"

| # | Blocker | Size | Owner |
|---|---|---|---|
| B1 | **Port HR bonus endpoint** (`hr_bonus.py` + `hr_bonus_service.py` → `sandy-qa` route; repoint GAS `BACKEND_BASE_URL` + auth story for GAS→Sandy [sast_ token — sit-down ask #3]) — needed before the **Oct 1** run; Sep 1 stays on Railway by design | ~1 session | Max |
| B2 | **Nightly sweep INSERT arm** (NightlyScoring §9): `seen_via='stats_pull'` cc_calls creation + Sales `provider_config` onboarding (Sales callcenter id + the 4 addendum disposition gaps) | small | Max |
| B3 | **Final data pass**: full `cc_*` re-import (shadow only mirrors cc incrementally and it trails), then delta close. ⚠ `pg_to_d1.py --wipe` is UNSCOPED — it would delete Sandy-born rows (the 2026-08-03 incident class). The final pass must reuse shadow_sync's range-scoped semantics per table | ~1 session + verify | Max |
| B4 | **Data-safety bundle** off the laptop (see §5) — before, not after | small + storage decision | Max + Eng (ask #7) |
| B5 | **Analyst comms + redirect** (§2.4) and repoint/park the Railway domain | small | Max |
| B6 | **Owner acceptance**: Analyst_History + Score_Audit Sheets tabs freeze at cutover (D1 `qa_score_audit` + `qa_roster_events` + `cron_runs`/`qa_disposition_pulls` become the audit surface). SQLMigration retention promises (audit permanence) must be restated for D1 | decision | Max/owner |
| B7 | **Webhook decision**: retirement does NOT wait for webhook ingress (stats-pull is the working path; CC v1 webhooks are future work) — but the Dialpad subscription pointing at Railway must be deliberately deleted/parked so it doesn't 5xx-loop into auto-disable limbo | sit-down | Eng |
| B8 | **mass-notifications JDBC** (`Database.gs` → Railway PG directly, not HTTP) — separate app, separate thread (sandy-mass-notifications PRD Phase 6 backfill); listed here so the PG shutdown date accounts for it | separate track | Max |

Non-blockers that die WITH Railway (deliberate retirement, same day):
laptop crons (`shadow_sync.sh` 30-min, `parity/nightly.sh` — remove from
crontab, don't let them fail forever), `database/runner.py`,
`import_agents.py`, `sheets_projection.py`, GAS row mode,
`version_ship` archive, SSE heartbeat tuning, the `.deploy-trigger`
watch-path machinery, CONTRIBUTING.md Railway deploy notes.

## 4. Kill sequence (proposed runbook skeleton)

1. **Now**: spend stop (§2 combo) + comms + fix `readme.txt`.
2. **Sep 1**: HR run executes on Railway (watch it; loud failure mode
   emails ALERT_EMAIL). Sandy's first EOM branch also fires — verify both.
3. **Pre-freeze week**: B1 (HR port + GAS repoint), B2 (INSERT arm +
   Sales config), B4 (data bundle), B6 sign-off, B7 subscription
   teardown scheduled.
4. **Freeze Railway writes**: rotate/blank `API_KEY_*` (analysts already
   moved), stop the disposition loop (`CC_STATS_PULL_INTERVAL_MIN`
   unset), leave read-only if desired.
5. **Final delta import** (B3): range-scoped per-table delta + full cc_*
   re-import + reconciliation (one-way: every Railway-born row present in
   D1; Sandy-born rows are canonical and PG never had them).
6. **Flip the sweep INSERT arm** (config or one-line deploy) — Sandy
   becomes sole call-ingest.
7. **Retire laptop crons** (crontab -r the two entries), archive
   `shadow_sync`/`parity` code with a tombstone header.
8. **Dormant 30 days** (Railway service stopped, PG retained) → final PG
   dump into the data bundle → disconnect + delete. mass-notifications
   (B8) must be off PG before the delete, or PG outlives the app on its
   own clock.

## 5. Data-safety bundle (single-laptop-loss risk — do FIRST)

Gitignored, single-copy, PII-bearing, on this laptop only (~12 MB):
- `database/analyst_history_{member_support,sales}.csv` (backfill seeds;
  parity fixture scripts read them by absolute path)
- `database/backfill_staging/` — **includes the 19 forever-
  `import_blocked` evaluations that exist NOWHERE else by design**
- `database/eom_exports/` — June+July 2026 deliverables, unreproducible
  once the PG-backed generators die
- `database/export_document_test_data.csv`, stray one-pager HTML
- `database/dialpad_stats_records_dispositions_example.csv` — the NS2
  E2E fixture; currently untracked AND unignored → add a `.gitignore`
  line + include in the bundle
- At cutover: the final PG dump joins the bundle.
Destination = Engineering ask #7 (PII — not git). Until then: any
company-approved encrypted storage beats one SSD.

## 6. Exit-criteria amendment (parity harness EOL)

The Phase 5 exit checklist ("N zero-drift days, stats parity both
teams") is **unsatisfiable and obsolete**: since Sandy became the write
path (evals ≥10M) and roster authority (AA0 departures), D1 ≠ PG **by
design**. 2026-08-30 run: 41 MS mismatches, proven identical under
v0.58 modules — structural, not regression (D1 +33 Aug Sandy-born evals;
−28 July evals/−2 roster from departures PG never learns). Sales still
0 diffs. Replace with **one-way completeness checks** at cutover (B3):
per-table Railway-born row counts/checksums PG vs D1 (range-scoped),
plus the operational gates already passed (all pages live, actions,
email, HR run, crons). Retire `nightly.sh` either at cutover or now with
a Railway-born-only re-scope if a drift signal is still wanted interim.

## 7. Sit-down agenda additions (beyond the standing list)

1. Webhook ingress: confirm "retire without it" (B7) + subscription
   teardown; CC v1 webhooks become a NEW Sandy-side design when needed.
2. sast_ token ask: **largely mooted** — shadow sync retires instead of
   being replaced by Railway push; still wanted for GAS→Sandy (HR bonus
   B1) and any machine callers.
3. PII/retention on D1: Railway PG encryption-at-rest statement in
   SQLMigration §8.4 doesn't carry over; restate the D1 story +
   audit-permanence promise (B6).
4. AI-gateway per-team cost attribution (standing) + confirmation that
   killing the personal key ends the last non-Engineering AI spend.
5. Railway account/billing shutdown date (Hobby tier) after the 30-day
   dormancy.

## 8. Incidents found during this audit (fixed in-session)

- **Assessments mirror wedged since 2026-08-20**: CL4 insights let
  `qa_assessment_sections` autoincrement → Sandy-born sections took low
  ids (554–580) → PG serial collided → UNIQUE failure every sync run
  (evals unaffected — earlier step). Fixed v0.60 (`aid*100+n` ids) +
  27 rows re-idded live. Doctrine reaffirmed: **Sandy-born rows in any
  Railway-parity table must own explicit high-range ids — including
  child tables.** Sweep other child tables for the same class at B3.
- Sync + parity crons 401'd from token expiry Aug 29→30 (re-authed;
  next expiry ~Sep 9).
