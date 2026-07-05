# Member Support — QA Scoring Migration Spec

> Canonical specification for the Member Support scoring formula (current: `member_support_v3`).
> **Purpose:** decouple the QA scoring pipeline from Google Sheets-as-database and make the
> **PostgreSQL engine the single owner of score production**. Scope is deliberately limited to the
> agreed rubric sections, their initial weights, the scoring formula/rules, and triggers.

| | |
|---|---|
| **Status** | Finalized (Ops VP sign-off; §10 open items **closed 2026-07-04** — see Section10SignoffBriefs.md) |
| **Formula ID** | `member_support_v3` *(v1 = original sign-off; v2 = Ops-signed keys, archived; v3 = §10-close threshold tightening)* |
| **Rubric version** | `member_support_v2` |
| **Score scale** | 0–100 |
| **System of record** | this spec → PostgreSQL scoring engine (replaces Sheets) |
| **Last updated** | 2026-07-04 |

---

## 1. Purpose & Scope

**In scope**
- The 10 agreed rubric sections and their score types.
- Initial/base weights and how weight is redistributed at runtime.
- The scoring formula: normalization, the rule library, and deterministic evaluation order.
- Escalation triggers.

**Out of scope** (owned by other docs)
- Ingestion / transport (Dialpad → pipeline), Command Center event wiring, dashboards.
- Prompt engineering for the AI scorer.
- Supervisor-review UI and scorecard fields.

**Migration intent**
Today scores are computed against Google Sheets acting as both config and store. This spec defines the
formula precisely enough that the PostgreSQL engine can own score production end-to-end; Sheets becomes,
at most, a read-only mirror.

---

## 2. Formula Metadata

| Field | Value |
|---|---|
| `formula_id` | `member_support_v3` |
| `rubric_version` | `member_support_v2` |
| `scale` | 0–100 |
| weight basis | percentage points summing to 100 |
| team rule set | Member Support (scale ×0.5; hard-zero disabled) |

---

## 3. Rubric Sections

Base weight is **canonical** (stored). The **Automated** weight is *derived at runtime* when
`human_review_required = NA` (rule `hrr_na_spread`) and is shown for reference only — it is not stored.

| # | key | Label | Score type | Weight (base) | Weight (automated)* | Trigger |
|---|---|---|---|---|---|---|
| 1 | `greeting` | Greeting | rating 1–5 | 5% | 6.11% | — |
| 2 | `caller_id` | Caller ID (identity validation) | Y / N / NA | 5% | 6.11% | — |
| 3 | `purpose` | Purpose of Call | rating 1–5 | 5% | 6.11% | — |
| 4 | `matching` | Matching the Moment | rating 1–5 | 5% | 6.11% | candidate |
| 5 | `process_adherence` | Process Adherence | rating 1–5 | 25% | 26.11% | **active** |
| 6 | `call_resolution` | Call Resolution | rating 1–5 | 20% | 21.11% | **active** |
| 7 | `comms` | Communication | rating 1–5 | 10% | 11.11% | — |
| 8 | `efficiency` | Efficiency | rating 1–5 | 10% | 11.11% | candidate |
| 9 | `human_review_required` | Human Review Required *(was "Documentation")* | rating 1–5 / NA | 10% | 0% *(NA default)* | — |
| 10 | `cri` | Customer Resolution Indicator | Y / N | 5% | 6.11% | — |
| | | **Total** | | **100%** | **100%** | |

\* Automated column assumes the standard automated case (only `human_review_required = NA` → 9 active
sections, +1.11 pts each). It differs when additional sections are NA (the spread is recomputed over the
sections that remain active).

---

## 4. Score Normalization

- **Rating (1–5):** `frac = (rating − 1) / 4` → `1 = 0.00, 2 = 0.25, 3 = 0.50, 4 = 0.75, 5 = 1.00`.
- **Binary (Y/N):** `Y = 1.0`, `N = 0.0`.
- **NA:** the section is not scored; its weight is redistributed by the applicable rule (never silently lost).
- **Section contribution:** `weight_i × frac_i`.
- **Raw score:** `Σ contributions` over active sections → 0–100, *before* any score-level scaling.

---

## 5. Formula Rules (Rule Library)

Rules belong to a shared engine and are gated per team. For `member_support_v3`:

| id | type | enabled | Condition | Effect |
|---|---|---|---|---|
| `hard_zero` | score_override | **false** | `caller_id = N` | Set final score = 0. *Retained for collections / billing / legal; OFF for Member Support.* |
| `caller_id_na_transfer` | weight_transfer | true | `caller_id = NA` | Move all `caller_id` weight → `call_resolution`. |
| `frequent_caller_shift` | weight_transfer | true | `frequent_caller` signal | Move a configurable share (**default 0.5**) of `call_resolution` weight → `process_adherence` (target selectable). Also biases `caller_id` scoring toward NA (prompt-level). |
| `hrr_na_spread` | weight_redistribution | true | `human_review_required = NA` | Split its weight **equally (additive)** across the active scored sections: `+weight / N` each. |
| `caller_id_scale_half` | score_scale | true | `caller_id = N` | Multiply final score × **0.5**. (Section also contributes 0 on its own slot.) |
| `escalation_flag` | flag | true | `process_adherence` frac ≤ 0.25 **OR** `call_resolution` frac ≤ 0.25 | Emit `human_review_required` event; route to supervisor review. *(Tightened from ≤ 0.5 at §10 close — ratings 1–2 only.)* |

**Notes**
- `cri = N` needs **no rule**: binary normalization already gives it `0`, so it simply forfeits its weight. *(The former ×0.9 score-wide multiplier is deprecated.)*
- `caller_id = N` and `caller_id = NA` are mutually exclusive branches.
- The `frequent_caller` signal originates from a **Looker snapshot delivered via Command Center** (external dependency; see §8 `external_signals`).

---

## 6. Rule Evaluation Order (deterministic)

The engine always applies steps in this order:

1. `hard_zero` check — team-gated; no-op for Member Support
2. `caller_id_na_transfer` — weight → resolution
3. `frequent_caller_shift` — resolution → process
4. `hrr_na_spread` — equal-additive spread
5. **weighted sum** — `Σ(weight × frac)`
6. `caller_id_scale_half` — × 0.5
7. `escalation_flag` check — reads *raw* process/resolution ratings; independent of redistribution

Weight-moving rules (2–4) reshape the rubric **before** the sum; the ×0.5 (6) applies **after** it; the
flag (7) reads raw ratings, so redistribution never changes whether a call escalates.

---

## 7. Triggers

- **Threshold:** a trigger section scoring **1–2** (`frac ≤ 0.25`, inclusive) escalates the call.
  *(Tightened from 1–3 at §10 close: over the 1,678-eval history the 1–3 threshold escalated
  26.1% of calls; 1–2 escalates 12.0% — intervention where it's most effective.)*
- **Active triggers:** `process_adherence`, `call_resolution`.
- **Candidate triggers** (under review, **not live**): `matching`, `efficiency`.
  *(§10 close: stay candidates — activating them at the old threshold would have escalated 52.4%
  of all calls.)*
- **Routes to:** `human_review_required` — the supervisor scores that section on review.

---

## 8. Canonical Config (machine-readable)

```json
{
  "formula_id": "member_support_v3",
  "rubric_version": "member_support_v2",
  "supersedes": "member_support_v2",
  "scale": { "min": 0, "max": 100 },
  "normalization": {
    "rating_1_5": { "type": "linear", "input_min": 1, "input_max": 5, "output": [0.0, 1.0] },
    "binary_yn": { "Y": 1.0, "N": 0.0 },
    "na": "redistribute_per_rules"
  },
  "sections": [
    { "key": "greeting",              "label": "Greeting",                      "score_type": "rating_1_5",    "weight": 5.0,  "trigger": null },
    { "key": "caller_id",             "label": "Caller ID",                     "score_type": "binary_yn_na",  "weight": 5.0,  "trigger": null },
    { "key": "purpose",               "label": "Purpose of Call",               "score_type": "rating_1_5",    "weight": 5.0,  "trigger": null },
    { "key": "matching",              "label": "Matching the Moment",           "score_type": "rating_1_5",    "weight": 5.0,  "trigger": "candidate" },
    { "key": "process_adherence",     "label": "Process Adherence",             "score_type": "rating_1_5",    "weight": 25.0, "trigger": "active" },
    { "key": "call_resolution",       "label": "Call Resolution",               "score_type": "rating_1_5",    "weight": 20.0, "trigger": "active" },
    { "key": "comms",                 "label": "Communication",                 "score_type": "rating_1_5",    "weight": 10.0, "trigger": null },
    { "key": "efficiency",            "label": "Efficiency",                    "score_type": "rating_1_5",    "weight": 10.0, "trigger": "candidate" },
    { "key": "human_review_required", "label": "Human Review Required",         "score_type": "rating_1_5_na", "weight": 10.0, "trigger": null, "na_default": true },
    { "key": "cri",                   "label": "Customer Resolution Indicator", "score_type": "binary_yn",     "weight": 5.0,  "trigger": null }
  ],
  "rules": [
    { "id": "hard_zero",             "type": "score_override",        "enabled": false, "when": { "section": "caller_id", "equals": "N" },  "effect": { "set_score": 0 }, "note": "retained for collections/billing/legal; disabled for member_support_v2" },
    { "id": "caller_id_na_transfer", "type": "weight_transfer",       "enabled": true,  "when": { "section": "caller_id", "equals": "NA" }, "effect": { "from": "caller_id", "to": "call_resolution", "amount": "all" } },
    { "id": "frequent_caller_shift", "type": "weight_transfer",       "enabled": true,  "when": { "signal": "frequent_caller" },            "effect": { "from": "call_resolution", "to": "process_adherence", "fraction": 0.5, "target_configurable": true }, "note": "signal wired false-by-default until command_center ships (Wave 3)" },
    { "id": "hrr_na_spread",         "type": "weight_redistribution", "enabled": true,  "when": { "section": "human_review_required", "equals": "NA" }, "effect": { "method": "equal_additive", "from": "human_review_required", "to": "active_sections" } },
    { "id": "caller_id_scale_half",  "type": "score_scale",           "enabled": true,  "when": { "section": "caller_id", "equals": "N" },  "effect": { "multiply": 0.5 } },
    { "id": "escalation_flag",       "type": "flag",                  "enabled": true,  "when": { "any": [ { "section": "process_adherence", "frac_lte": 0.25 }, { "section": "call_resolution", "frac_lte": 0.25 } ] }, "effect": { "emit_event": "human_review_required", "route": "supervisor_review" }, "note": "threshold tightened 1-3 -> 1-2 per Ops VP sign-off 2026-07-04 (Section10SignoffBriefs A1)" }
  ],
  "evaluation_order": [
    "hard_zero",
    "caller_id_na_transfer",
    "frequent_caller_shift",
    "hrr_na_spread",
    "weighted_sum",
    "caller_id_scale_half",
    "escalation_flag"
  ],
  "triggers": {
    "threshold": { "op": "lte", "frac": 0.25, "ratings": [1, 2] },
    "active": ["process_adherence", "call_resolution"],
    "candidates": ["matching", "efficiency"],
    "routes_to": "human_review_required"
  },
  "external_signals": {
    "frequent_caller": { "source": "looker_snapshot", "via": "command_center" }
  },
  "human_review_triggers": [
    { "section_id": "process_adherence", "max_score_to_trigger": 2 },
    { "section_id": "call_resolution",   "max_score_to_trigger": 2 }
  ]
}
```

---

## 9. Migration Notes (Sheets → PostgreSQL)

- Base weights and rule config move into PostgreSQL (suggested: `qa.formula`, `qa.formula_section`,
  `qa.formula_rule`), versioned by `formula_id`.
- Automated weights are **computed, not stored** (derived by `hrr_na_spread`).
- The engine **emits** `human_review_required` events rather than writing review state back to Sheets.
- Sheets is demoted to a read-only mirror (or retired) once score parity is verified against a hold-out set.

---

## 10. Open Decisions

All items **closed 2026-07-04** (Ops VP, via Section10SignoffBriefs.md Part A):

- [x] `matching` / `efficiency` **stay candidates**. Additionally, the active-trigger threshold was
      tightened from ratings 1–3 to **1–2** (frac ≤ 0.25) — shipped as `member_support_v3`.
      Historic incidence: 26.1% → 12.0% of calls escalate.
- [x] `frequent_caller_shift` defaults confirmed as-is (fraction 0.5, target `process_adherence`).
- [x] Hard-zero **stays globally available, disabled for Member Support**.
- [x] Multiple-NA spread confirmed as shipped: recompute equal-additive over remaining active
      sections, order per §6; NA weight is never silently lost (engine hard-fails otherwise).
