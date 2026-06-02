# Call time vs eval time on Analyst_History — design doc

> **Purpose:** Separate "when the call happened" from "when we scored it"
> on Analyst_History. Today `COL_TIMESTAMP` (col C) holds the approval
> time and every downstream consumer (chiclets, SPC, EWMA, days filter,
> /team/evals drill-down) buckets by it. After this initiative, col C
> means "call connected at" (from Dialpad's `date_connected`), and the
> eval/approval time moves to a new trailing column. Score_Audit
> continues to be the authoritative audit log of "who scored what when"
> — unchanged.
> **Author session:** 2026-06-01.
> **Status:** **Implemented.** PR-1 (schema + writer + reader fallback)
> merged as PR #42; PR-2 (backfill script, sales + MS runs completed)
> merged as PR #43; PR-3 (analytics anchor flip + UI labels +
> "bucketed by call date" hints) lands as the current PR.

---

## Decisions locked (2026-06-01)

| Decision | Choice | Notes |
|---|---|---|
| Schema shape on Analyst_History / FR-AI | Repurpose col C → call_connected; eval timestamp moves to a new trailing column | TRAILING_WIDTH 6 → 7. Long-term semantic: col C = "the time of the thing the row is about." |
| FR1 score_destination tab `metadata.timestamp` | Switch to call_connected | Apps Script email's "Evaluation Date" line becomes the call date — usually what agent/manager actually want. |
| When analytics shift bucketing from eval → call | After backfill is complete (PR-3) | Avoids a window where chiclets/SPC bucket old rows by eval time and new rows by call time. |
| Backfill script | Separate PR after schema PR | PR-1 ships schema + writer + reader-with-fallback. PR-2 ships backfill. PR-3 flips analytics. |
| Fallback when `date_connected` is missing on a new approval | Empty cell | Truthful "we don't know" — downstream readers treat blank as missing; backfill script can retry later. |

---

## What stays the same

- **Score_Audit tab** is unchanged. `append_score_audit_row` keeps writing
  the approval timestamp at the moment of `/score` and `/approve`. This is
  the legal-grade record of "who scored what when."
- **Section/reasoning/confidence column positions** in Analyst_History.
  The new column is **trailing**, so no shift in section columns — the
  Apps Script side reading section scores by letter is unaffected.
- **`history_layout.HistoryLayout.col_score(i)` etc.** semantics. Only
  `TRAILING_WIDTH` changes; everything else shifts naturally via the
  derived properties.

---

## Schema deltas

### `backend/config/history_layout.py`

```python
TRAILING_WIDTH = 7        # was 6 — new column for eval_approved_at

# COL_TIMESTAMP semantic shift:
#   was → eval/approval time (UTC clock, written by sheets_service)
#   now → call's `date_connected` from Dialpad get_call_details
COL_TIMESTAMP = 2         # column letter unchanged; meaning changed

class HistoryLayout:
    ...
    @property
    def col_eval_approved_at(self) -> int:
        """New trailing column. Holds the approval-time UTC string that
        used to live in col C. Stays blank when the writer cannot
        compute it (defensive — should never happen since the writer
        always knows when *it* fired)."""
        return self.confidence_end + 6
```

(Existing `col_source` becomes `confidence_end + 5`; the new column is
positioned after it.)

### `backend/config/teams/<team>.json` — FR1 metadata_cols

```jsonc
"metadata_cols": {
  "timestamp": "A",         // now stores call_connected (date_connected, UTC string)
  "manager_email": "B",
  "agent_name": "C",
  "key_strengths": "N",
  "opportunities": "O",
  "dialpad_link": "P"
  // No new FR1 column for eval-approved-at; that's now only on
  // Analyst_History + Score_Audit. The FR1 tab is purely the email
  // pipeline's input — eval time isn't needed there.
}
```

Both `member_support.json` and `sales.json` need the comment/doc note that
`timestamp` now means call time. The literal JSON value (`"A"`) doesn't
change — only the semantic.

---

## Writer path changes

### `backend/services/scoring_service.py`

`ScorecardWithMeta` gains two optional fields (open Q1 resolution —
capture `date_ended` too so the duration helper has both inputs from
day one):

```python
class ScorecardWithMeta(...):
    ...
    call_started_at_utc: Optional[datetime] = None
    """Dialpad `date_connected` for this call, normalized to UTC datetime.
    None when get_call_details failed or the field was missing — the
    writer treats None as 'leave the cell blank, backfill will fix it.'"""

    call_ended_at_utc: Optional[datetime] = None
    """Dialpad `date_ended`, same normalization. Plumbed but NOT written
    to Analyst_History in this phase — only consumed by the
    `compute_call_duration` stub (see services/dialpad_client.py).
    Writing it to a column is a follow-up project's call."""
```

`score_call` populates both from `call_details["date_connected"]` and
`call_details["date_ended"]`. We already have the helper `_epoch_to_iso`
in `dialpad_client.py`; add a sibling `_epoch_ms_to_utc_datetime` for
the writer's `datetime` typing.

### `services/dialpad_client.py` — duration stub

```python
def compute_call_duration(
    call_started_at_utc: Optional[datetime],
    call_ended_at_utc: Optional[datetime],
) -> Optional[timedelta]:
    """Difference between Dialpad's `date_ended` and `date_connected`.

    STUB: returned for any caller that wants it, but no production code
    consumes it yet. Plumbed in this PR so a future "call duration as
    an analytics dimension" project doesn't have to re-touch
    scoring_service / ScorecardWithMeta. Returns None when either input
    is missing.
    """
    if call_started_at_utc is None or call_ended_at_utc is None:
        return None
    return call_ended_at_utc - call_started_at_utc
```

Test surface: a happy-path returns the right timedelta, missing-input
returns None. Two tests, both in `test_dialpad_client.py`.

### `backend/services/sheets_service.py`

**`write_draft_to_fr_ai`** (Stage 1) — currently writes the *draft*
timestamp at col C. After:

```python
row[history_layout.COL_TIMESTAMP] = _format_call_time(scorecard.call_started_at_utc)
row[L.col_eval_approved_at] = ""   # filled at Stage 4 (approval)
```

`_format_call_time` returns `"MM/DD/YYYY HH:MM:SS"` (UTC, matching the
existing format) or `""` when `call_started_at_utc is None`.

**`finalize_to_analyst_history`** (Stage 4) — currently overrides col C
with `datetime.now(UTC)`. After:

```python
# Stop overriding col C — it was set at Stage 1 from call data.
# Write the approval time to the new trailing column.
history_row[L.col_eval_approved_at] = datetime.now(timezone.utc).strftime(
    "%m/%d/%Y %H:%M:%S"
)
```

**`write_to_score_destination`** (Stage 2) — already pulls the
timestamp from FR-AI col C. No code change required; the *value* shifts
from approval-time to call-connected automatically, which is what we
want for the FR1 tab. Apps Script email reads it via
`metadata_cols.timestamp`.

---

## Reader path during the transition

The transition spans three PRs. During PR-1 and PR-2, analytics must
keep producing stable results — which means continuing to bucket by
**eval time** until the backfill makes call_started universally
available.

`load_and_clean` reads col C today as the canonical timestamp. After
PR-1 lands, col C on **new rows** is the call connected time, and the
new trailing column holds eval time. On **historical rows**, col C is
still eval time and the new trailing column is blank.

So during the transition, "what eval time is" is computed as:

```python
eval_ts = parse_timestamp(row[L.col_eval_approved_at]) or parse_timestamp(row[COL_TIMESTAMP])
#                          ^^^^^^^^^^^^^^^^^^^^^^^^^   new rows                ^^^^^^^^^^^^^^^^^^^^^^   old rows fallback
```

And "what call time is" is computed as:

```python
call_ts = parse_timestamp(row[COL_TIMESTAMP]) if has_call_time_been_backfilled(row) else None
```

For PR-1, `load_and_clean` keeps writing `df["timestamp"]` from the
eval-time path so chiclets/SPC/EWMA/days-cutoff behavior is unchanged.
A second column `df["call_started"]` gets added (None where unknown)
so PR-3 can flip analytics without another `load_and_clean` touch.

The "has it been backfilled" question is resolved naturally: PR-2
backfill writes call_started to col C and moves the original col C
value (eval time) to the new trailing column for every historical row.
After backfill completes, every row satisfies "trailing column =
eval time, col C = call time" coherently.

---

## Score_Audit unchanged

The Score_Audit tab is the canonical audit log. `append_score_audit_row`
writes a fresh approval timestamp at /score time and at /approve time.
That doesn't move. Even after PR-3 flips analytics to call-time
bucketing, the audit answer to "when was this call scored" is one
Score_Audit row away.

---

## Backfill script (PR-2)

`qa-automation/AI-Scoring/scripts/backfill_call_started.py`:

- Reads Analyst_History rows.
- For each row where the new trailing column is blank:
  - Parse `call_id` from `dialpad_link` (col E).
  - `get_call_details(call_id)` → `date_connected`.
  - **Two-cell update**: write the old col C value into the trailing
    column (eval time relocation), then overwrite col C with the
    formatted `date_connected` (call time).
  - Rate-limit (≤ 5 req/s per Dialpad's published limit).
  - Sleep + retry on 429.
  - Skip + log when get_call_details returns no `date_connected`.
- Per-row audit: append a record to a new local
  `.backfill-log` file (gitignored) so a partial run can resume.
- `--dry-run` mode: prints planned mutations, writes nothing.
- `--max-rows N`: cap for incremental runs.
- `--team-id <id>`: process one team at a time.

Rate-limit math: 5 req/s × 600s = 3,000 rows/run upper bound. For
member_support (~3k rows) one run; for sales (~600 rows) one run.

---

## Analytics shift (PR-3)

Single edit in `load_and_clean`:

```python
# Before:
df["timestamp"] = ...  # eval time

# After:
df["timestamp"] = df["call_started"]   # populated by load_and_clean from col C
df["eval_approved_at"] = ...           # the now-trailing eval-approval-time column
```

Every chiclet/SPC/EWMA/days-filter/outlier consumer reads
`df["timestamp"]` and gets call-bucketed data automatically. The
`eval_approved_at` column stays available for any consumer that wants
to filter or display by approval time.

Manual verification at PR-3 time:
- May 2026 chiclet shifts from "evals approved in May" → "calls placed
  in May". Visible numerical change expected.
- A call placed late April but scored early May moves from May → April
  bucket. Worth flagging in the PR-3 PR body so reviewers don't see it
  as a regression.

---

## Edge cases + fallback policy

1. **`date_connected` missing or `get_call_details` failed at /score time.**
   `call_started_at_utc` stays None. `write_draft_to_fr_ai` writes
   `""` to col C. Finalize writes the eval-approval-time to the new
   trailing column. The row's "what time was the call" answer is
   blank — picked up by the next backfill run if Dialpad returns the
   info later.

2. **Long-call flag** (`flagged_long_call` is True). Doesn't change call
   time semantics; the flag stays on its dialpad_link `[LONG CALL]`
   suffix as today.

3. **Approval long after scoring.** Common; the call-time stays
   stable across draft → approve. Stage 4 only sets the trailing
   approval-time column.

4. **Manual upload with no Dialpad metadata.** `score_call` calls
   `get_call_details(call_id)`. If `call_id` is fake/external, the
   call returns blank and col C stays empty. Acceptable — the analyst
   knew this was a non-Dialpad call when uploading.

5. **Frontend "Date" displays.** The `/team/evals` drill-down's Date
   column, EWMA expansion's Date column, and Recent Evals' time-ago all
   currently read `timestamp`. After PR-3 they show call time. Worth
   labeling clearly in the UI ("Call date" instead of "Date") in the
   PR-3 PR.

6. **Existing `_to_utc` boundary fix on `/team/evals`** (from PR #39
   chiclets work) covers naive timestamps. The new column writes UTC
   strings the same way the existing one did, so the fix continues to
   apply.

---

## Implementation phases + pytest checkpoints

1. **Phase 0 — Schema bump (no behavior change)**
   - `TRAILING_WIDTH` 6 → 7.
   - `col_eval_approved_at` property on `HistoryLayout`.
   - Update `total_width` assertions.
   - Tests: layout returns the right column index for the new field at
     N=10, N=19, and N=1 boundary.
   - Checkpoint: `pytest tests -q` green.

2. **Phase 1 — Writer plumbing (Stage 1 + Stage 4)**
   - `ScorecardWithMeta.call_started_at_utc` field.
   - `scoring_service.score_call` populates from `date_connected`.
   - `write_draft_to_fr_ai` writes formatted call-time to col C; blank
     to col_eval_approved_at.
   - `finalize_to_analyst_history` stops overriding col C; writes
     approval time to col_eval_approved_at instead.
   - Tests: writer path produces the expected row shape under (a)
     call_details with valid date_connected, (b) call_details missing,
     (c) call_details failed (None). Mock the sheet writer.
   - Checkpoint: green.

3. **Phase 2 — Reader with fallback**
   - `load_and_clean` produces `df["timestamp"]` from
     col_eval_approved_at if present else col C. New column
     `df["call_started"]` parsed from col C when the new col is
     populated (heuristic that the row is "new shape"), else None.
   - Tests: an old-style row (only col C populated) still yields a
     valid `df["timestamp"]`. A new-style row yields
     `df["timestamp"]` from the trailing column and
     `df["call_started"]` from col C.
   - Checkpoint: green. No analytics behavior change; chiclets/SPC/etc
     still bucket by `df["timestamp"]` which is eval time during the
     transition.

4. **Phase 3 — FR1 metadata.timestamp semantic shift**
   - JSON comments only (no schema change to the JSON keys).
   - Update `write_to_score_destination` docstring to note the cell now
     holds call time.
   - No code change to the actual write logic — it already copies col C
     verbatim.
   - Apps Script side note: the email template's "Evaluation Date"
     label should change to "Call Date" — separate PR on the GAS side
     after this lands.

5. **Phase 4 — PR open**
   - PR body warns: "this lands the schema and writer; analytics still
     bucket by eval time. Backfill (PR-2) and analytics shift (PR-3)
     are follow-ups."

After PR-1 merges:

6. **Phase 5 — Backfill script (PR-2)** — separate PR, see "Backfill
   script" section above.

7. **Phase 6 — Analytics shift (PR-3)** — separate PR, single edit in
   `load_and_clean` plus UI labels.

---

## Open questions — resolved 2026-06-01

1. **Capture `date_ended` + plumb a duration helper.** **Resolved: yes.**
   Capture `date_ended` on the same path as `date_connected`. Stub a
   `compute_call_duration(call_started_at_utc, call_ended_at_utc) -> Optional[timedelta]`
   helper in `services/dialpad_client.py` (or a new `services/call_metrics.py`)
   that is **wired into the writer but not yet consumed by analytics** —
   plumbing only. Implementation of "what to do with the duration"
   (display, analytics bucket, outlier inputs) is deferred to a later
   project. This avoids re-touching `ScorecardWithMeta` /
   `sheets_service` later when the duration feature lands.
2. **Backfill rate limit.** **Confirmed: 5 req/s** is still the
   Dialpad standard. Slower run is the right trade for reliability;
   the script will stay well under the cap with a small jitter.
3. **PR-3 tooltip.** **Resolved: yes.** Bucket-shift would be
   confusing to management on its own — chiclets, SPC chart, and SPC
   datapoint hover get a brief "bucketed by call date" annotation
   when PR-3 lands. Specific copy decided at PR-3 time.

### Newly-raised risk — backfill ID mismatch

Historical rows store `dialpad_link` keyed by `entry_point_call_id`
(per PR #32 fix), but `get_call_details(call_id)` is documented
against the master `call_id`. For direct calls the two ids coincide;
for queue-routed inbound calls they diverge. The backfill script
must handle both cases:

- **Lookup attempt 1**: call `get_call_details(eval_id)` where
  `eval_id` is parsed from `dialpad_link`. For direct calls this
  works; the response carries `date_connected` and `date_ended`.
- **Lookup attempt 2** (queue calls): if attempt 1 returns 404 or
  blank, try `get_recent_calls` (already exists) to search by
  `entry_point_call_id` and recover the master id. If that's
  unworkable, log the row's `eval_id` to the backfill audit and
  skip — analyst can resolve manually.

Skipped rows stay readable under the PR-2 fallback (`load_and_clean`
falls back to col C for non-backfilled rows). After PR-3 flips
analytics, skipped rows show "—" for the date in the UI — a known
incomplete-data state, not a corrupted one.

Recommend the backfill script emit a `.backfill-skipped.csv` with
columns (`row_num`, `agent`, `eval_id`, `reason`) so the gaps are
discoverable and triagable in bulk rather than buried in a log file.
