# Late-Stage Design Registry — features pending docs, decisions, or both

> Single registry of everything the reference corpus mentions, promises,
> or half-scopes that has **no design doc or implementation plan of its
> own** — compiled 2026-07-12 from a full sweep of `references/`,
> `database/SQLMigration.md`, and `landing-ai/LandGPT.md`. Future
> sessions start here: pick an item, write its doc (design-doc-first for
> anything > half a day), link it back, and strike the row.
>
> Ordered by silent-failure impact (data loss / launch block first, doc
> hygiene last). Items already covered by a real design doc (ReadPathFlip,
> SopRag/#99, HRBonusSheet, LandGPT, LandingOpsCommandCenter, CutoverDesign)
> appear only where a *piece* of them is undecided.

---

## Tier 1 — owner-owed decisions blocking scoped work

| Item | Where it's owed | Blocks | Notes |
|---|---|---|---|
| **13 default `na_applies_when` texts** | Sales spec §10a pre-flip checklist; Sales Mgmt | Sales rubric prose correctness (NA = full credit, so loose wording leaks score) | Pipeline live regardless; texts are operational defaults today |
| **GLR v2 bullet points** (General Landing Rules) | Owner | August v6 bundle AND SopRag cascade Stage C prompt | Same deliverable feeds both — write once |
| **HR bonus: Sales section subset** | HR + Sales Mgmt (HRBonusSheet §8) | Sales onboarding to the HR workbook | Config-only once decided (`hr_export` block in sales.json) |
| **HR bonus P4** | Operator | first supervised run 2026-08-01 | clasp create → Script Properties → `installMonthlyTrigger()` (hr-bonus/README.md) |
| **ε-sweep (Phase 6)** | Unblocked by B4 (2026-07-12) | leadership view of historic-compliance deltas | MUST ship with the BackfillPlan §2/§2a narrative attached — deltas are large by design |

## Tier 2 — designed elsewhere, one undecided piece

| Item | Doc | The undecided piece |
|---|---|---|
| SopRag R1–R6 | #99 (pending Ops VP) | bge-m3 hosting (container vs sidecar — decide in R3 with latency numbers); Stage-A prompt's lean on the Dialpad transcript hint; long-call embedding strategy |
| Read-path flip F1–F5 | ReadPathFlip.md | none — ready to build |
| Nightly `reproject` sweep | CutoverDesign (mentioned, never built) | projection-drift repair job: scope, cadence, alerting |
| Analyst_History sheet retirement ("Phase D") | CutoverDesign §7 tab-retention rule | after read-path flip + one full bonus cycle: what (if anything) still reads the sheet? GAS email does — retirement needs an email-source decision |
| LandGPT v1 pilot | LandGPT.md (living) | its own open-questions table: runtime (MLX/llama.cpp/Ollama), annotated-transcript schema fields, Stage-2 hardware, pilot go/no-go |

## Tier 3 — recurring promises with NO design doc (each needs one)

| Feature | Mentioned in | What the future doc must decide |
|---|---|---|
| **Manager version-ship / rubric / formula editor UI** | Section10 B1 (a signed **requirement**: rating curve reconfigurable in the same UI that ships versions); Wave2 §7; SQLMigration v1.3 (`POST /rubric_and_formula` atomic edit) | auth tier, edit surface, validation against archived versions, ship ceremony vs restart-loader |
| **Cost tracking + admin dashboard** (Step 7 / PRD Phase D) | PhaseOne, PRD-MultiTeam | `qa.api_audit_log` already collects cost rows; needs `/api/admin/costs`, dashboard page, per-team budget caps + cutoff semantics. RAG cascade (§6 of SopRag) makes this urgent-adjacent: calls multiply |
| **Dialpad webhook automation + stratified sampler** (Step 8) | PhaseOne, PhaseThree (`sampling_state`), CC Phase 5 (auto-score on `recording` webhook), roadmap "webhooks + 100% local AI" (August) | sampling policy (30–35/agent/week vs 100% Local-AI era), dedupe vs lookup-triggered scores, `sampling_status` column exists and is waiting |
| **Assessment writer/reader + coaching workflow UI + tags apply/remove** | Wave2 §7; migrations 011 (assessments) and v1.2 (tags/coachings) shipped EMPTY schemas | R3 partially covers qa.assessments persistence for the one-pager; coaching/tags UI wholly undesigned |
| **Per-evaluator session identity** | LookupToScore future-work; scoring.py comment ("Future: replace with authenticated session user") | replaces client-supplied `manager_email`; prerequisite for HR/MGMT role split |
| **In-memory `_jobs` store** | PhaseOne tech debt (real: `routes/scoring.py` module dict) | restart loses in-flight scorecards; decide: table in qa.* vs accept (jobs are minutes-long) |
| **Duplicate-call version picker** | PRD-MultiTeam §7 ("v1 shows all versions; future: choose which persists") | interacts with D2 re-eval semantics now in the DB |
| **Call-duration analytics dimension** | CallTimeOnAnalystHistory (`compute_call_duration` wired, unconsumed); B2 backfilled `call_duration_ms` on ~1,900 rows | data now exists — a dashboard axis away |
| **Explicit-NA vs missing in analytics** | NumericNAOption out-of-scope note; standing `xfail` in test_analytics | becomes tractable post-read-path-flip: the DB distinguishes `binary_value='NA'` from absent rows natively — fold into F5 or its own doc |
| **TeamStatsBoard §11 extensions** | change-point detection, mixed-effects, predictive risk, GMM, CUSUM | park until Local-AI 100% coverage; `coverage_regime` per-row tagging is the prerequisite ([[project_predictive_groundwork]]) |
| **LandGPT v2 employee surfaces** | LandGPT.md v2 (chat sessions, storage, RBAC, branded frontend) | post-v1-pilot only |
| **CC Phases 2–5** | LandingOpsCommandCenter.md | wallboard, Slack call-management, profanity detection, MCP server — Wave 3 territory |

## Tier 4 — small forgotten items (low ceremony, just do them)

- **GAS email label** "Evaluation Date" → "Call Date" (CallTime doc; separate GAS-side PR + deploy, never made).
- **Task #7 date-flake** (`test_monthly_summary_buckets_in_project_local_time`) — fails on DST-dependent dates; the suite's only red for weeks.
- **Cell-notes retention + `rebuildHistory()` enrichment** (AgentProgressionDashboard open questions — GAS side).
- **Tier-2 `improvements`→`opportunities` rename** (QAEntry.js/AnalystHistory.js) + `teams/sales/Branding.js` header typo + stale `Main.js` Mails comment.
- **`score_destination` config blocks + sheet tabs cleanup** — after read-path flip + one bonus cycle (paired with Analyst_History retirement decision, Tier 2).
- **`qa.score_audit` DB table usage** — schema exists; verify whether the writer still only appends to the sheet tab, and if so wire the dual-write or retire the table decision into the reproject/retirement doc.

## Tier 5 — doc hygiene (stale status lines that mislead readers)

| Doc | Correction needed |
|---|---|
| PhaseThree.md | **Superseded banner**: its unified-jsonb schema lost to SQLMigration's normalized `qa.*`; do not implement as written |
| LookupToScore.md | Says "Design — not yet implemented"; shipped in PR #28 (+#30 gave `/score/batch` audit/auth/idempotency parity — its own future-work item, already done) |
| NumericNAOption.md | Says "not yet implemented"; shipped in PR #33 |
| LiveDashboard.md | Phases B–E marked unimplemented; shipped in PR #37 (+#38/#39) |
| Wave2Plan.md | MS §10 checklist renders unchecked; closed 2026-07-04 per Section10SignoffBriefs |
| SQLMigration.md | file the promised **v1.5** §3.8 update (Ops-signed formula shape) + note `annotated_transcript` no longer NULL-on-Gemini once the SopRag cascade ships (`gemini_annotate_v1`) |
| PhaseOne.md | Tech-debt table: FR-AI "rewrites same draft row" is the *designed* idempotency now, not debt; job-store row stays (Tier 3) |

---

**Maintenance rule:** when an item here gains a real design doc, replace
its row with a link; when it ships, delete the row in the same PR. This
file should shrink.
