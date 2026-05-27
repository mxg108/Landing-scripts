# Numeric-section N/A — design doc

> **Purpose:** Honor `na_applicable` in the scorecard UI for numeric sections,
> and honor it in the *negative* direction for Y/N sections too. Today the
> dropdown only respects score_type, not `na_applicable`, so:
> - **Numeric + `na_applicable: true` → N/A option missing.** (Reported case;
>   user screenshot on `flex_long_stay_pitch`.)
> - **Y/N + `na_applicable: false` → N/A option leaks.** Member Support has
>   several yn sections where N/A should not be selectable but currently is.
> **Author session:** 2026-05-25.
> **Status:** Design — not yet implemented.

---

## Decisions locked (2026-05-25)

| Decision | Choice | Notes |
|---|---|---|
| Storage of explicit N/A on numeric | Reuse `yn_value="NA"` on `ScorecardSection` | Smallest model change. `score` stays `Optional[int]`; `yn_value="NA"` is the explicit N/A flag for any score_type. |
| Scope | Fix both directions (numeric+NA missing, yn+NA leak) | One pass, one consistent rule: dropdown options are derived from `score_type` × `na_applicable`. |
| Source of `score_type` in the UI | Switch from `aiById[*].score_type` to `_teamSections[*].score_type` | Team config is the source of truth. AI response can drift (and currently does — see screenshot anomaly below). |
| AI prompt | Numeric+`na_applicable: true` emits `score: <1-5 integer or null>, yn_value: <null or NA>` | Lets Gemini explicitly return NA on numeric sections where the question doesn't apply. |
| Sheet display | Continue reusing `YN_DISPLAY["NA"] = "Not Applicable"` for any N/A, regardless of score_type | One display string, one parse rule downstream. |

---

## Goal + motivation

Today, `teams/sales.json` declares e.g. `flex_long_stay_pitch` with
`na_applicable: true`, but the scorecard UI offers only `1/5 … 5/5` in the
dropdown — there's no way for an analyst to mark the section N/A. The AI also
has no way to do so via the prompt schema for numeric sections, even though
the team config says N/A is meaningful.

The mirror problem exists on Y/N sections: the UI always renders the N/A
option for *any* yn section, even when the team config says
`na_applicable: false`. The result is sections like `member_support.json`'s
`identity_verified` (yn, `na_applicable: false`) where analysts can pick N/A
and there's no schema gate stopping them.

**After this work**, the dropdown for every section is derived from team
config:

| `score_type` | `na_applicable` | Dropdown options |
|---|---|---|
| `numeric` | `false` | `1/5, 2/5, 3/5, 4/5, 5/5` |
| `numeric` | `true`  | `1/5, 2/5, 3/5, 4/5, 5/5, N/A` |
| `yn`      | `false` | `Y, N` |
| `yn`      | `true`  | `Y, N, NA` |
| `manual`  | (same as numeric) | as above |
| `manual_yn` | (same as yn)    | as above |

---

## Scope

**In scope:**
- `ScorecardSection`: document that `yn_value: "NA"` is now valid for any
  `score_type`. No new fields.
- `qa_scoring_prompt.build_output_schema`: emit N/A as a valid output for
  numeric sections where `na_applicable: true`.
- `frontend/scorecard.html`: derive dropdown options from `ts.score_type` ×
  `ts.na_applicable`. Stop branching on `aiById[*].score_type`.
- `frontend/scorecard.html` approval payload: when the analyst picks N/A on a
  numeric section, send `score: null, yn_value: "NA"`.
- `sheets_service._format_ai_score`: honor `yn_value == "NA"` before
  formatting numeric. (`stage_1.5` apply_analyst_edits goes through the same
  helper.)
- Dashboard read path: `load_and_clean` already coerces "Not Applicable" to
  `np.nan` for numeric cols (line 158-161) — N/A and "AI didn't score" become
  indistinguishable in aggregates, which matches today's behavior for
  unscored cells. Acceptable for v1; flagged as follow-up.

**Out of scope:**
- Distinguishing "explicit N/A" from "missing data" in stats. Today both
  coalesce to NaN. If we want to count N/A rate per section, that's a
  separate analytics feature.
- Pre-existing yn-NA coercion bug in `load_and_clean:170` — `"Not Applicable"
  .upper()[:1] == "N"`, so historical N/A on yn columns has been miscoded as
  N. ~~Out of scope; tracked separately.~~ **Resolved 2026-05-27** in a
  follow-up PR: `_parse_yn_cell` helper replaces the `[:1]` slice and
  preserves "Not Applicable" / "NA" / "N/A" as the "NA" sentinel.
  Historical rows are re-parsed correctly on the next read — no data
  migration needed.
- Backfill of historical numeric rows. Existing sheet cells stay as-is.

---

## Screenshot anomaly (must fix as part of this work)

The reported screenshot is on `flex_long_stay_pitch`, which `sales.json:185`
declares as `score_type: "yn"` — yet the UI shows a `1/5..5/5` dropdown. The
cause is `scorecard.html:457`:

```js
if (s.score_type === 'yn') { ... }  // s = aiById[ts.id]
```

The frontend branches on the AI's echoed `score_type`, not the team config's.
If Gemini returns `score_type: "numeric"` for a yn section (or if the AI row
was written before sales.json was updated), the dropdown silently flips
shape. Switching the branch source to `ts.score_type` fixes this and removes
a class of UI-state-drift bugs.

---

## Schema deltas

### `backend/models/scorecard.py`

```python
class ScorecardSection(BaseModel):
    id: str
    name: str
    score: Optional[int] = None   # 1-5 for numeric (or null for yn / explicit N/A)
    score_type: str               # "numeric" | "yn" | "manual" | "manual_yn"
    yn_value: Optional[str] = None
    # "Y" / "N" — yn sections only
    # "NA"    — yn sections AND numeric sections with na_applicable: true
    confidence: str
    reasoning: str
    audio_dependent: bool = False
    flags: List[str] = []
```

No field added or removed — just a docstring/comment update. Validator
optional: forbid `yn_value="NA"` when the team config says
`na_applicable: false` (defense in depth; the UI already won't offer it).

### `qa_scoring_prompt.build_output_schema`

Today (line 101-108):
```python
if sec.score_type == "numeric":
    score_val = "<1-5 integer>"; yn_val = "null"
else:
    yn_val = "<Y or N or NA>" if sec.na_applicable else "<Y or N>"
```

After:
```python
if sec.score_type == "numeric":
    if sec.na_applicable:
        score_val = "<1-5 integer or null if NA>"
        yn_val = "<null or NA>"
    else:
        score_val = "<1-5 integer>"
        yn_val = "null"
else:
    yn_val = "<Y or N or NA>" if sec.na_applicable else "<Y or N>"
```

Rubric block (line 63) also extends: numeric+`na_applicable: true` gets the
"Mark NA if …" note.

### Frontend — `scorecard.html` lines 415-467

Single render rule, driven by team config:

```js
// inside _teamSections.map(ts => ...)
const naOpt = ts.na_applicable
  ? `<option value="NA" ${s.yn_value==='NA'?'selected':''}>N/A</option>`
  : '';

if (ts.score_type === 'yn') {            // NB: ts, not s
  scoreControl = `<select ... data-field="yn_value">
    <option value="Y" ...>Y</option>
    <option value="N" ...>N</option>
    ${naOpt}
  </select>`;
} else {
  scoreControl = `<select ... data-field="score">
    ${[1,2,3,4,5].map(v => `<option value="${v}" ...>${v}/5</option>`).join('')}
    ${naOpt}
  </select>`;
}
```

The numeric `<select>` keeps `data-field="score"` for 1-5 options, but the
N/A option's `value="NA"` lives in the same select. The approval-payload
serializer needs to detect `"NA"` and split it: `score: null, yn_value: "NA"`.

Manual section dropdown (line 417-440) gets the same treatment.

### `sheets_service._format_ai_score`

```python
def _format_ai_score(sec_def: SectionDef, ai_section: dict) -> str:
    yn = ai_section.get("yn_value")
    if yn == "NA":
        return YN_DISPLAY["NA"]            # "Not Applicable"
    if sec_def.score_type == "yn":
        return YN_DISPLAY.get(yn or "NA", "Not Applicable")
    score = ai_section.get("score")
    return str(score) if score is not None else "N/A"
```

N/A check moves to the top so it short-circuits regardless of score_type.
`apply_analyst_edits_to_fr_ai` already routes through this for AI sections;
the `manual` branch (line 331) needs the same N/A-first check added.

---

## Implementation phases + pytest checkpoints

1. **Phase 0 — Schema doc**
   - Update `ScorecardSection` docstring/comment on `yn_value`.
   - No behavior change. Tests still green: `pytest qa-automation/AI-Scoring/tests -q`.

2. **Phase 1 — Prompt builder**
   - Extend `build_output_schema` + `build_scoring_rubric` for numeric+na.
   - Add tests in `tests/test_qa_scoring_prompt.py` (or wherever the prompt
     tests live — TBC) for two cases:
       - numeric + `na_applicable: true` → `<null or NA>` appears in schema.
       - numeric + `na_applicable: false` → `yn_val == "null"` unchanged.
   - Checkpoint: prompt tests green; no integration tests touched yet.

3. **Phase 2 — sheets_service**
   - Move N/A check to top of `_format_ai_score`.
   - Update `apply_analyst_edits_to_fr_ai` manual branch.
   - Tests: round-trip a `ScorecardSection(score_type="numeric",
     yn_value="NA")` through the formatter, assert "Not Applicable".
   - Checkpoint: sheets tests green.

4. **Phase 3 — Frontend**
   - Switch dropdown branch to `ts.score_type`.
   - Add `naOpt` to both branches; gate on `ts.na_applicable`.
   - Approval-payload serializer: when the numeric select's value === "NA",
     emit `score: null, yn_value: "NA"`.
   - Manual section dropdown: same treatment.
   - No automated UI tests today; manual verification (load a scored call on
     sales, confirm NA appears on `flex_long_stay_pitch` if it ever returns
     to numeric, confirm member_support yn sections with
     `na_applicable: false` no longer show NA).

5. **Phase 4 — Full sanity pass**
   - `pytest qa-automation/AI-Scoring/tests -q`.
   - Re-score one Sales call end-to-end, watch FR-AI row.
   - PR.

---

## Open questions — resolved 2026-05-25

1. **Validator for `yn_value="NA"` on a section without `na_applicable`?**
   **Resolved: yes — add it.** Future model abstractions / non-Gemini providers
   may hallucinate NA on non-NA sections; cheap defense-in-depth. Implemented
   as two layers on `ScorecardSection`:
   - **Always-on cross-field validator** (no team config needed): if
     `yn_value == "NA"`, `score` must be None; if `score` is set, `yn_value`
     must be None; etc.
   - **Context-aware validator** (runs only when caller passes
     `context={"section_def": sec_def}` to `model_validate`): rejects
     `yn_value == "NA"` when `sec_def.na_applicable is False`.
   The scoring pipeline parses AI responses with the context attached; tests
   and stored-data round-trips parse without context (relaxed).
2. **Count `yn_value="NA"` as "scored" for the approval gate?**
   **Resolved: yes.** Explicit N/A is a complete analyst decision.
   `approveScorecard` and `checkManualScores` should treat `"NA"` (or empty
   for plain N/A on numeric) the same as a real score. Wire in Phase 3.
3. **`team_stats.compute_long_form` treatment of explicit numeric N/A.**
   **Resolved: known tech debt.** Numeric N/A coalesces to `np.nan` today —
   indistinguishable from "unscored." Document with an `xfail` test in
   `test_analytics.py` so the gap surfaces when we revisit during the
   SQL/analytics migration. No code change in this work.
