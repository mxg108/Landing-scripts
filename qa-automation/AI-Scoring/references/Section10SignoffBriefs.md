# §10 Sign-off Briefs — Member Support (Ops VP) + Sales (Sales Management)

> Decision prep for Wave2Plan Phases **3b** and **3c**. Each open item below quotes the spec,
> states what the shipped engine does *today*, adds the real-history numbers where the backfill
> seed can speak (1,678 MS evaluations — see [BackfillPlan.md §1](../../../database/BackfillPlan.md)),
> and gives an engineering recommendation. The two `_scoring_migration.md` specs are the sign-off
> surfaces — they get edited only after these conversations close, never unilaterally.
>
> **Cost of changing a decision later is now low but not zero:** every change = a new
> `formula_version` row (drop revised JSON + restart FastAPI — the whole ceremony since PR #75),
> but historic scores keep their pinned versions forever, and every version bump adds a row to
> explain in audits. Better to lock these now.

**Context to lead with (both conversations):** the legacy sheet formula, reverse-engineered at
99.5% exactness from history, weighted only 6 of 10 sections — Greeting, Caller ID, Matching, and
**Process Adherence carried zero weight** ([BackfillPlan.md §2](../../../database/BackfillPlan.md)).
The v2 formula is the first time those sections affect scores. Historic-compliance deltas will be
large **by design**; leadership should hear that from us before they see a sweep report.

---

## Part A — Member Support §10 (Ops VP, 4 items)

### A1. `matching` / `efficiency`: active triggers or candidates?

- **Spec:** candidates, "under review, not live" (§7).
- **Engine today:** `escalation_flag` fires only on `process_adherence` / `call_resolution`
  frac ≤ 0.5 (ratings 1–3). Matching/efficiency are listed under `triggers.candidates` — inert.
- **Data:** escalation rate over the full history **26.1%** (438/1,678) with today's two triggers.
  Adding matching + efficiency as active: **52.4%** — the human-review queue **doubles** (+742 rows
  added solely by the two candidates; efficiency ≤ 3 is common: holds/dead-air).
- **Recommendation: keep them candidates.** At 52% trigger rate the supervisor-review section
  stops being an exception path. If the VP wants more coverage, a compromise exists: activate at a
  stricter threshold (rating ≤ 2 ⇒ frac ≤ 0.25) — the config supports per-rule `when` clauses
  without engine changes.

### A2. `frequent_caller_shift`: fraction 0.5, target `process_adherence`?

- **Spec:** "configurable share (default 0.5) of call_resolution weight → process_adherence."
- **Engine today:** exactly that (`fraction: 0.5`, `target_configurable: true`), and the
  `frequent_caller` signal is **false-by-default until Command Center ships (Wave 3)** — this rule
  currently never fires in production.
- **Data:** none possible (the signal didn't exist historically).
- **Recommendation: confirm the defaults as-is.** Zero production impact until Wave 3, so this is
  a free confirmation now vs. a re-sign-off later. Flag that when CC ships, the shift compounds
  with `caller_id_na_transfer` (order is deliberate, §6): with caller_id=NA it moves 12.5 pts,
  not 10.

### A3. Hard-zero: keep globally available (off for MS) or remove?

- **Spec:** retained for collections/billing/legal; **disabled** for Member Support.
- **Engine today:** rule present with `enabled: false`; the enabled path is tested (override wins,
  flags still emit).
- **Data:** `caller_id = N` on **24.9%** of all historic evaluations (418/1,678), whose sheet
  scores average **77.3** (median 82). Under hard-zero those would all be 0. Under shipped v2 they
  score ×0.5 (median ≈ 41) — already a dramatic tightening vs. the legacy sheet, which ignored
  caller_id entirely.
- **Recommendation: keep the rule, keep it disabled for MS.** The ×0.5 scale is severe enough as
  the behavioral lever, and a per-team `enabled` flag costs nothing while collections/billing/legal
  teams may want it. Removing it outright would mean re-adding the rule type later for those teams.

### A4. NA-spread with multiple NA sections

- **Spec §10:** "current: recompute equal-additive over remaining active sections."
- **Engine today:** exactly that, deterministically — weight rules run in `evaluation_order`
  against *current* weights, so `caller_id_na_transfer` (all 5 pts → call_resolution) precedes
  `hrr_na_spread` (10 pts equal-additive over whatever is still active: 9 sections normally, 8
  when caller_id is also NA). Covered by unit tests + 14 NA golden fixtures from real rows.
- **Data:** multi-NA co-occurs at **0.4%** (7/1,678: caller_id NA + cri NA). Rare but real.
- **Recommendation: confirm the shipped behavior** (it's the spec's own "current" reading). The
  invariant worth stating to the VP: *NA weight is never silently lost* — the engine hard-fails an
  eval where an NA section's weight has nowhere to go.

---

## Part B — Sales §10 (Sales Management, 7 items)

### B1. Rating→points curve: `(rating−1)/4` vs `rating/5`

- **Spec assumption:** `(rating−1)/4` (worst = 0 pts), consistent with the binary "0 or full
  points" anchors. The §11 worked example (75.00) is built on it.
- **Engine today:** the curve is per-formula config (`normalization.rating_1_5.output`); both are
  one line apart. MS v2 ships `(r−1)/4`.
- **Data point that matters:** the **legacy MS sheet used `r/5`** (rating 1 = 20%, proven at 99.5%
  parity). If Sales' legacy sheet did the same, agents' historic intuition is `r/5` — switching to
  `(r−1)/4` lowers every scaled section by up to 20 points of frac. This is a real behavioral
  change to socialize, not a transcription detail. *Materially changes every scaled score* (spec's
  own words).
- **Recommendation: confirm `(rating−1)/4`** (internally consistent with the PDF's binary
  anchors), but present the `r/5` legacy-composure fact so the choice is made knowingly. Once the
  Sales Analyst_History export lands we can prove which curve their sheet used, same method as MS.

### B2. NA = full credit, all 15 sections, no redistribution

- **Spec:** PDF footer — N/A awards full points on-slot.
- **Engine today:** `na: "full_credit"` is implemented and §11-tested (both NA rows contribute
  full weight in the 75.00 vector). Opposite of MS — the per-formula switch exists precisely for
  this.
- **Recommendation: confirm as written**, with one framing question for management: full-credit
  NA means an agent is *rewarded* for sections that never came up — a call with 5 NAs can only
  lose points on 10 sections. If that's intended (it's what the PDF says), we ship it verbatim.

### B3. Version id & lineage — **engineering constraint to surface**

- **Spec:** `formula_id = sales_v1` *(proposed)*, `supersedes: null` *(confirm)*.
- **Constraint:** `rubric_version = 'sales_v1'` is **already taken** — migration 010 seeded it
  with the *legacy 19-section* rubric, and the archive is immutable (the ship path refuses to
  reuse a version id with different content). The Ops-signed 15-section matrix must ship under a
  new id: **`sales_v1_ops`** (Wave2Plan 3d's name) or `sales_v2`.
- **Recommendation:** `formula_id = sales_v1_ops`, `rubric_version = sales_v1_ops`,
  `supersedes: null` for the formula (first engine-owned formula) while the *rubric* row closes
  the seeded `sales_v1`. §3a/§3b of the spec (add/deprecate diff vs. the 19-section rubric) can
  then be populated from the seeded rubric — the "prior rubric" the spec asked for **exists in the
  archive already**; we don't need an external document.

### B4. Section keys

- **Spec:** proposed keys are the spec author's; confirm/rename to match the codebase.
- **Constraint:** the codebase's existing Sales ids are the legacy 19-section set
  (`situation_match`, `value_uplift`, `flex_long_stay_pitch`, …) that does **not** map 1:1 to the
  15 new sections — Wave2Plan §9 already flags this as unresolvable by renaming.
- **Recommendation: adopt the proposed 15 keys as-is.** They're clean, and the legacy ids stay
  valid forever on historic rows via rubric pinning (`sales_v1`). No renames needed — just
  confirmation that e.g. `move_reason` is a *new* section, not `situation_match` renamed
  (that distinction decides §3a/§3b bookkeeping).

### B5. Persist `category`?

- **Engine today:** `FormulaSection.category` exists, nullable, already round-trips.
- **Recommendation: keep it.** Zero engine cost, and category-level reporting ("Discovery vs.
  Closing") is a likely dashboard ask. Nothing to build either way.

### B6. `client_experience` under *Post-Call & Documentation*

- Pure taxonomy — zero scoring impact (weight and curve are unaffected by category).
- **Recommendation:** whatever management prefers; suggest moving it to a call-wide category only
  if B5 lands as "persist" (otherwise the placement is invisible).

### B7. No rules / no triggers in v1

- **Engine today:** an empty rule set is first-class (`evaluation_order: ["weighted_sum"]`,
  §11-tested). When rules arrive later they reuse the MS engine unchanged — one new formula
  version, no code.
- **Recommendation: confirm none for v1.** Cheap to add later; impossible to un-ship surprises.

---

## Part C — What closing these unblocks

| Sign-off | Unblocks |
|---|---|
| MS §10 (A1–A4) | Spec §10 boxes checked; `member_support_v2` confirmed as-shipped (or one clean v3 bump); Phase 6 sweep interpretation norms |
| Sales §10 (B1–B7) | **3d** ship `sales_v1_ops` (rubric + formula); task #5 sales.json regeneration to the nested shape; Sales golden fixtures once their Analyst_History export lands; Sales Stage-2 compute at cutover |

Neither blocks Stage 1.5/2 dual-write engineering (Phase 4b) — state transitions are
formula-agnostic pre-cutover. Only the *post-cutover* Stage 2 compute step needs the Sales items.
