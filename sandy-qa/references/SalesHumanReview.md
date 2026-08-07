# Sales always-human-review — parity fix spec (v0.26, 2026-08-06)

**Behavior:** every scored SALES call parks at `scoring_status =
'flagged_human_review'` (draft, no overall_score, queue marker stamped) until
an analyst resolves it in the editor. Sales evals never auto-finalize and
never auto-email. Member Support is unaffected. This was Railway behavior;
the original Sandy port carried only half the gate.

## Mechanism (Railway parity — not a hardcoded team name)

Railway's finalize decision (`backend/routes/scoring.py`):

```
flagged = human_review_trigger_fired(formula, sections)   # §3.14 thresholds
          OR requires_analyst_review(config)               # ← this half was dropped
```

`requires_analyst_review` (`backend/services/eval_store.py:84`): true when the
team's rubric has **manual sections** (`score_type` `manual`|`manual_yn`) whose
formula section lacks **`na_default`** — the AI cannot legitimately fill those
scores, so the eval must hold for an analyst. Provenance: production find
2026-07-06, when Sales' `potential_booking`/`notes_mc` rode NA-default drafts
into full credit under the full_credit NA policy.

Why it hits exactly Sales:

| team | manual sections | formula na_default set | fires |
|---|---|---|---|
| sales (`sales_v2`) | `potential_booking`, `notes_mc` | ∅ | **always** |
| member_support (`member_support_v5`) | `human_review_required` | `{human_review_required}` | never (NA is its designed auto value) |

Verified against live D1 (qa_rubric_versions current + qa_formula_versions
active) on 2026-08-06 before shipping.

## Sandy implementation

`src/routes/scoring.ts` → `requiresAnalystReview(config, formula)` (rubric
sections by `id`, formula sections by `key` — same namespace), OR-ed into the
callback gate ahead of the §3.14 trigger loop. Flagged rows follow the
existing paths: draft insert, `human_review_required_at` stamp, review-queue
visibility, approve/override exits, no eval_approved event, no GAS email.

Notes:
- Config-derived, so a future formula granting `na_default` to Sales' manual
  sections (or a new team with uncovered manual sections) changes behavior
  with **zero code change** — same as Railway.
- Railway's `human_review.mode` (`authoritative`|`informative`) is not ported;
  both teams run the `authoritative` default, which is the only behavior the
  Sandy gate implements. Port the mode switch if a team ever flips.

## Verification

1. Data-level: predicate replicated in Python against live D1 → sales=True,
   member_support=False (above table).
2. Operator check: score one Sales call → it must land in the review queue
   (red editor), with no email; approve it → finalize + email as usual.
