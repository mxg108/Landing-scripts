# Sales — QA Scoring Migration Spec

> Canonical specification for the Sales scoring formula. **Same purpose and format as the Member Support
> spec** (`member_support_scoring_migration.md`): make the PostgreSQL engine the owner of score production.
>
> **Populated from `QA_Scoring_Guide.pdf`** (Sales QA — Revised 15-Point Matrix). Structure matches Member
> Support 1:1. Two modeling decisions inherited from the PDF differ from Member Support and are flagged
> throughout — read these before locking `sales_v1`:
>
> 1. **N/A awards full credit on-slot** (frac 1.0), *not* redistribution — the single biggest behavioral
>    difference from Member Support. See §4.
> 2. **The rating→points curve is assumed** `(rating − 1) / 4`. The PDF gives point values but not the
>    curve; this reading is the one consistent with the binary "0 or full points" anchors. See §4 / §10.

| | |
|---|---|
| **Status** | Rubric finalized (Sales QA — Revised 15-Point Matrix); spec **populated from source PDF**, pending confirmation on the §10 flagged items |
| **Formula ID** | `sales_v1` *(proposed — confirm)* |
| **Rubric version** | `v1` *(proposed)* |
| **Score scale** | 0–100 (15 questions, 100 points) |
| **System of record** | this spec → PostgreSQL scoring engine (replaces Sheets) |
| **Last updated** | 2026-06-30 |

---

## 1. Purpose & Scope

**In scope**
- The 15 agreed rubric sections and their score types.
- Base weights (point values, which double as percentage weights).
- The scoring formula: normalization and deterministic evaluation order — *currently a plain weighted sum, no rules.*

**Out of scope** (owned by other docs / sources)
- Ingestion / transport (Dialpad → pipeline), Command Center event wiring, dashboards.
- Prompt engineering for the AI scorer.
- Supervisor-review UI and scorecard fields.
- **Section descriptor prose** (the 0/1 and 1–5 level definitions) — owned by `QA_Scoring_Guide.pdf`, not duplicated here.

**Migration intent** — identical to Member Support: define the formula precisely enough for the
PostgreSQL engine to own score production end-to-end; retire Sheets-as-DB.

> **Version note:** the source is labeled a *revised* matrix, which implies a prior Sales rubric. If a
> prior **engine-owned** version exists, this is a rubric version change (sections added/deprecated), and
> the migration must preserve the mapping between historical scores and the version that produced them.
> The add/deprecate diff (§3a / §3b) **cannot be derived from the new PDF alone** — it needs the prior
> rubric. If `sales_v1` is the first engine-owned version, §3a / §3b are simply empty.

---

## 2. Formula Metadata

| Field | Value |
|---|---|
| `formula_id` | `sales_v1` &nbsp; *(proposed — confirm)* |
| `rubric_version` | `v1` &nbsp; *(proposed)* |
| `supersedes` | `null` &nbsp; *(confirm — see §10)* |
| `scale` | 0–100 (15 questions, 100 points) |
| weight basis | points ≡ percentage points; sum = 100 |
| **NA policy** | **full credit on-slot** (frac 1.0, **no redistribution**) — differs from Member Support |
| special rules | **none** — reserved for future |

---

## 3. Rubric Sections

15 sections. Weight = the PDF's point value; since points sum to 100, **points ≡ percentage weight**.
**Every section accepts N/A, which awards full credit** (frac 1.0 — see §4). Weights are **static** (no
runtime redistribution), so unlike Member Support there is no derived "automated" weight column.

Score-type display: `0/1/NA` = binary (`binary_yn_na`), `1–5/NA` = rating (`rating_1_5_na`).

| # | key | Label | Category | Score type | Weight | Trigger |
|---|---|---|---|---|---|---|
| 1 | `greeting` | Greeting | Opening & Professionalism | 0 / 1 / NA | 5% | — |
| 2 | `stay_type` | Personal or COHO Stay | Opening & Professionalism | 0 / 1 / NA | 4% | — |
| 3 | `move_reason` | Discover & Personalize the Move Reason | Discovery | 1–5 / NA | 15% | — |
| 4 | `landing_intro` | First Time Hearing About Landing | Discovery | 0 / 1 / NA | 6% | — |
| 5 | `timeline_housing` | Timeline & Housing Needs | Discovery | 1–5 / NA | 8% | — |
| 6 | `landing_guarantee` | Landing Guarantee (Tour Objection) | Pitch & Value Selling | 1–5 / NA | 6% | — |
| 7 | `pricing` | Pricing Breakdown | Pitch & Value Selling | 1–5 / NA | 8% | — |
| 8 | `fit_confirmation` | Confirmation of Fit | Pitch & Value Selling | 1–5 / NA | 5% | — |
| 9 | `objection_handling` | Objection Handling | Objections & Closing | 1–5 / NA | 8% | — |
| 10 | `urgency` | Urgency | Objections & Closing | 0 / 1 / NA | 5% | — |
| 11 | `follow_up` | Follow-up | Objections & Closing | 0 / 1 / NA | 6% | — |
| 12 | `potential_booking` | Potential Booking (PB) | Post-Call & Documentation | 0 / 1 / NA | 4% | — |
| 13 | `notes_mc` | Detailed Notes in MC | Post-Call & Documentation | 0 / 1 / NA | 5% | — |
| 14 | `contact_shared` | Contact Information Shared | Post-Call & Documentation | 0 / 1 / NA | 5% | — |
| 15 | `client_experience` | Client Experience | Post-Call & Documentation | 1–5 / NA | 10% | — |
| | | **Total** | | | **100%** | |

**Sanity check:** 7 scaled sections (60 pts) + 8 binary sections (40 pts) = 15 sections / 100 pts.

> **Note on `client_experience`:** the PDF files it under *Post-Call & Documentation*. It reads as a
> call-wide measure, so its category placement is transcribed faithfully but flagged in §10 for
> confirmation.

### 3a. Deprecated sections (removed vs. the prior Sales version)

**Blocked — requires the prior Sales rubric.** The PDF is the *revised* matrix (new state) only; the
section add/deprecate diff cannot be derived from it. Provide the previous rubric to populate this.
*(If `sales_v1` is the first engine-owned version, this section is empty.)*

- `<old_section_key>` — *reason / replaced by* &nbsp; **TODO (needs prior rubric)**

### 3b. New sections (added vs. the prior Sales version)

**Blocked — same reason as §3a.**

- `<new_section_key>` — *definition* &nbsp; **TODO (needs prior rubric)**

---

## 4. Score Normalization

- **Rating (1–5):** `frac = (rating − 1) / 4` → `1 = 0.00, 2 = 0.25, 3 = 0.50, 4 = 0.75, 5 = 1.00`.
  ⚠️ **Assumed.** The PDF states point values but not the rating→points curve. This mapping is the one
  consistent with the binary **"0 or full points"** anchors (worst rating = 0 pts, best = full pts).
  **Confirm before locking** — the alternative `rating / 5` would give rating 1 = 20% instead of 0%,
  changing every scaled section's score.
- **Binary (0/1):** met (`1`) = `1.0`, not-met (`0`) = `0.0`. Identical to the shared `binary_yn`
  normalization (`Y = 1.0 / N = 0.0`); Sales just labels the two branches `0` / `1`.
- **N/A = full credit (frac = 1.0).** ⚠️ **Opposite of Member Support.** Per the source PDF footer, when a
  criterion was not applicable the section is awarded **full points on its own slot**. There is **no
  weight redistribution** — the section keeps its weight and contributes it in full, so NA behaves exactly
  like a perfect score. *(In Member Support, NA removes the section and spreads its weight elsewhere.)*
- **Section contribution:** `weight_i × frac_i`.
- **Raw score:** `Σ contributions` across all 15 sections → 0–100. **No score-level scaling.**

Because NA awards full credit in place (never moves weight), **all weights are static** — there is no
"automated"/derived weight column as in Member Support.

---

## 5. Formula Rules (Rule Library)

**Empty for `sales_v1`.** The Sales rubric has no score overrides, weight transfers, redistributions, or
scaling rules. NA handling is a **normalization convention** (full credit, §4) that applies uniformly to
every section, so it needs no rule-library entry.

| id | type | enabled | Condition | Effect |
|---|---|---|---|---|
| *(none)* | | | | |

> When Sales Management add rules later, mirror the Member Support rule schema (`id`, `type`, `enabled`,
> `when`, `effect`) so both formulas run on the same engine. Do **not** implement any rules today — none
> exist in the source.

---

## 6. Rule Evaluation Order (deterministic)

1. **normalize** — map each section's answer to `frac` (rating curve, binary, or `NA → 1.0`).
2. **weighted sum** — `Σ(weight × frac)` → 0–100.

No pre-sum weight moves and no post-sum scaling exist in this version. NA is resolved during normalization
(step 1), so it never reshapes the rubric.

---

## 7. Triggers

**None.** No Sales section escalates or routes to review in this version. (Member Support's
`escalation_flag` + threshold logic have no Sales equivalent yet.)

---

## 8. Canonical Config (machine-readable)

```json
{
  "formula_id": "sales_v1",
  "rubric_version": "v1",
  "supersedes": null,
  "scale": { "min": 0, "max": 100 },
  "normalization": {
    "rating_1_5": { "type": "linear", "input_min": 1, "input_max": 5, "output": [0.0, 1.0] },
    "binary_yn": { "Y": 1.0, "N": 0.0 },
    "na": "full_credit"
  },
  "sections": [
    { "key": "greeting",           "label": "Greeting",                               "category": "Opening & Professionalism", "score_type": "binary_yn_na",  "weight": 5.0,  "trigger": null },
    { "key": "stay_type",          "label": "Personal or COHO Stay",                  "category": "Opening & Professionalism", "score_type": "binary_yn_na",  "weight": 4.0,  "trigger": null },
    { "key": "move_reason",        "label": "Discover & Personalize the Move Reason", "category": "Discovery",                  "score_type": "rating_1_5_na", "weight": 15.0, "trigger": null },
    { "key": "landing_intro",      "label": "First Time Hearing About Landing",       "category": "Discovery",                  "score_type": "binary_yn_na",  "weight": 6.0,  "trigger": null },
    { "key": "timeline_housing",   "label": "Timeline & Housing Needs",               "category": "Discovery",                  "score_type": "rating_1_5_na", "weight": 8.0,  "trigger": null },
    { "key": "landing_guarantee",  "label": "Landing Guarantee (Tour Objection)",     "category": "Pitch & Value Selling",      "score_type": "rating_1_5_na", "weight": 6.0,  "trigger": null },
    { "key": "pricing",            "label": "Pricing Breakdown",                      "category": "Pitch & Value Selling",      "score_type": "rating_1_5_na", "weight": 8.0,  "trigger": null },
    { "key": "fit_confirmation",   "label": "Confirmation of Fit",                    "category": "Pitch & Value Selling",      "score_type": "rating_1_5_na", "weight": 5.0,  "trigger": null },
    { "key": "objection_handling", "label": "Objection Handling",                     "category": "Objections & Closing",       "score_type": "rating_1_5_na", "weight": 8.0,  "trigger": null },
    { "key": "urgency",            "label": "Urgency",                                "category": "Objections & Closing",       "score_type": "binary_yn_na",  "weight": 5.0,  "trigger": null },
    { "key": "follow_up",          "label": "Follow-up",                              "category": "Objections & Closing",       "score_type": "binary_yn_na",  "weight": 6.0,  "trigger": null },
    { "key": "potential_booking",  "label": "Potential Booking (PB)",                 "category": "Post-Call & Documentation",  "score_type": "binary_yn_na",  "weight": 4.0,  "trigger": null },
    { "key": "notes_mc",           "label": "Detailed Notes in MC",                   "category": "Post-Call & Documentation",  "score_type": "binary_yn_na",  "weight": 5.0,  "trigger": null },
    { "key": "contact_shared",     "label": "Contact Information Shared",             "category": "Post-Call & Documentation",  "score_type": "binary_yn_na",  "weight": 5.0,  "trigger": null },
    { "key": "client_experience",  "label": "Client Experience",                      "category": "Post-Call & Documentation",  "score_type": "rating_1_5_na", "weight": 10.0, "trigger": null }
  ],
  "deprecated_sections": [],
  "rules": [],
  "evaluation_order": ["weighted_sum"],
  "triggers": {},
  "external_signals": {}
}
```

**Schema notes vs. Member Support config**
- `normalization` block is structurally identical **except** `na`: `"full_credit"` here vs.
  `"redistribute_per_rules"` in Member Support. This is the switch the engine keys off for NA handling.
- `binary_yn` keeps `Y`/`N` for engine consistency; the Sales rubric labels these branches `1`/`0`.
- `category` is an **added** field (nullable, PDF-sourced) not present in Member Support; the engine can
  ignore it. Confirm whether you want it persisted (§10).
- `rules`, `triggers`, `external_signals`, `deprecated_sections` are all empty by design for this version.

---

## 9. Migration Notes (Sheets → PostgreSQL)

- **Source of truth for section text:** `QA_Scoring_Guide.pdf` (Sales QA — Revised 15-Point Matrix).
  Descriptor prose is **not duplicated** here — this spec owns the formula, weights, and normalization
  only (same scope split as Member Support).
- **Same schema as Member Support:** reuse `qa.formula`, `qa.formula_section`, `qa.formula_rule`, keyed by
  `formula_id`. Sales rows carry an **empty `rules` set** and, if you keep `category`, one extra nullable
  column on `qa.formula_section`.
- **NA semantics differ and must be encoded, not assumed:** Sales NA = full credit on-slot (frac 1.0);
  Member Support NA = redistribute. If the engine hard-codes NA→redistribute, add a **per-formula NA
  policy** (`na: "full_credit"` vs `na: "redistribute_per_rules"`).
- **Weights are static** — nothing to compute at score time beyond normalization + weighted sum.
- **Version lineage:** persist `formula_id` / `rubric_version` on every stored score. If a prior
  engine-owned Sales version exists, set `supersedes` and record the §3a / §3b diff so historical rows
  stay interpretable.
- Sheets demoted to read-only mirror (or retired) once score parity is verified against a hold-out set —
  same cutover as Member Support. Use the §11 vector as a first parity/unit test.

---

## 10. Open Items — confirm before locking `sales_v1`

- [ ] **Scaled 1–5 → points curve.** Assumed `(rating − 1) / 4` (worst = 0, best = full; consistent with
      the binary "0 or full points" anchors). Confirm vs. `rating / 5`. *Materially changes every scaled
      section's score.*
- [ ] **NA = full credit.** Confirm the PDF footer ("N/A = full points when not applicable") applies to
      **all 15 sections** and means full-credit-on-slot with **no redistribution** (opposite of Member
      Support).
- [ ] **Version number & lineage.** Confirm `formula_id = sales_v1` and whether it supersedes a prior
      engine-owned version. If so, **provide the prior rubric** so §3a / §3b can be populated — the diff
      can't be produced from the new PDF alone.
- [ ] **Section keys.** Proposed keys are mine; confirm/rename to match existing column names in the
      codebase.
- [ ] **`category` field.** Confirm you want the PDF's category grouping persisted on `qa.formula_section`
      (engine can ignore it; useful for reporting).
- [ ] **`client_experience` placement.** The PDF files it under *Post-Call & Documentation*; confirm
      that's intended (it reads as a call-wide measure).
- [ ] **No rules / no triggers.** Confirmed absent in the PDF. Confirm none are planned for v1 before
      engineering builds against an empty rule set.

---

## 11. Worked Example (test vector)

*Not part of the 1:1 structure — added as a concrete parity/unit test for the engine. It exercises binary
met/not-met, scaled ratings across the range, and NA-full-credit on both a scaled (`timeline_housing`) and
a binary (`contact_shared`) section.*

| key | answer | frac | weight | contribution |
|---|---|---|---|---|
| `greeting` | 1 | 1.00 | 5 | 5.00 |
| `stay_type` | 0 | 0.00 | 4 | 0.00 |
| `move_reason` | 4 | 0.75 | 15 | 11.25 |
| `landing_intro` | 1 | 1.00 | 6 | 6.00 |
| `timeline_housing` | **NA** | **1.00** | 8 | **8.00** |
| `landing_guarantee` | 3 | 0.50 | 6 | 3.00 |
| `pricing` | 5 | 1.00 | 8 | 8.00 |
| `fit_confirmation` | 2 | 0.25 | 5 | 1.25 |
| `objection_handling` | 4 | 0.75 | 8 | 6.00 |
| `urgency` | 1 | 1.00 | 5 | 5.00 |
| `follow_up` | 0 | 0.00 | 6 | 0.00 |
| `potential_booking` | 1 | 1.00 | 4 | 4.00 |
| `notes_mc` | 1 | 1.00 | 5 | 5.00 |
| `contact_shared` | **NA** | **1.00** | 5 | **5.00** |
| `client_experience` | 4 | 0.75 | 10 | 7.50 |
| | | | **Σ** | **75.00** |

Expected final score = **75.00**. Note the two NA sections each contribute their **full weight** (8.00 and
5.00) — this is the behavior to assert against, since it's where Sales diverges from Member Support.
