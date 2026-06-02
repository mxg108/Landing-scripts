"""Derived column layout for Analyst_History and Form Responses AI tabs.

Both tabs share the same shape, parameterized only by N = len(sections).
Once a team's rubric publishes, section_number must never shift even if a
section is later deprecated — old rows keep their position.

Layout (0-indexed columns)::

    0           agent_name
    1           agent_email
    2           timestamp               (call_connected — see semantic note)
    3           evaluator_email
    4           dialpad_link
    5           overall_score
    6 .. 6+N-1  section scores          (one per section, in section_number order)
    6+N .. 6+2N-1  reasoning per section
    6+2N .. 6+3N-1 confidence per section
    6+3N        key_strengths
    6+3N+1      opportunities
    6+3N+2      call_summary
    6+3N+3      caller_name
    6+3N+4      caller_phone
    6+3N+5      source
    6+3N+6      eval_approved_at        (NEW — see semantic note)

Total width: 6 + 3N + 7 columns.

For Sales (N=19) → 70 cols (A–BR). For MS (N=10) → 43 cols (A–AQ).

----------------------------------------------------------------------
Timestamp semantic — see references/CallTimeOnAnalystHistory.md
----------------------------------------------------------------------
PR-1 of the call-time initiative repurposes ``COL_TIMESTAMP`` (col C)
to hold the call's ``date_connected`` from Dialpad (the time the call
actually happened) and introduces a new trailing column,
``col_eval_approved_at``, to hold the eval/approval-time UTC string
that historically lived in col C.

During the transition, ``load_and_clean`` reads ``col_eval_approved_at``
first, falling back to col C when the new column is blank (i.e. on
pre-cutover rows). Once the backfill (PR-2) populates the new column
for every historical row, the fallback path stops firing and the two
columns are coherent across the dataset. PR-3 then flips the analytics
anchor from "eval time" to "call time" by switching ``df["timestamp"]``
to the col C value.

Score_Audit remains the canonical record of "when was each row
scored/approved" — unaffected by this schema shift.
"""

from __future__ import annotations


# Fixed prefix (positions 0-5)
COL_AGENT_NAME = 0
COL_AGENT_EMAIL = 1
COL_TIMESTAMP = 2           # call_connected (PR-1 of call-time initiative)
COL_EVALUATOR_EMAIL = 3
COL_DIALPAD_LINK = 4
COL_OVERALL_SCORE = 5

PREFIX_WIDTH = 6
TRAILING_WIDTH = 7          # bumped 6 → 7 to seat col_eval_approved_at


class HistoryLayout:
    """Derives column positions from the section count.

    Use via TeamConfig.history_layout — the config object caches one
    instance sized for that team's section list.
    """

    def __init__(self, n_sections: int):
        if n_sections < 1:
            raise ValueError(f"n_sections must be >= 1, got {n_sections}")
        self.n = n_sections

    # --- Section ranges (half-open: [start, end)) ---------------------------

    @property
    def scores_start(self) -> int:
        return PREFIX_WIDTH

    @property
    def scores_end(self) -> int:
        return PREFIX_WIDTH + self.n

    @property
    def reasoning_start(self) -> int:
        return self.scores_end

    @property
    def reasoning_end(self) -> int:
        return self.reasoning_start + self.n

    @property
    def confidence_start(self) -> int:
        return self.reasoning_end

    @property
    def confidence_end(self) -> int:
        return self.confidence_start + self.n

    def col_score(self, section_idx: int) -> int:
        self._check_idx(section_idx)
        return self.scores_start + section_idx

    def col_reasoning(self, section_idx: int) -> int:
        self._check_idx(section_idx)
        return self.reasoning_start + section_idx

    def col_confidence(self, section_idx: int) -> int:
        self._check_idx(section_idx)
        return self.confidence_start + section_idx

    # --- Trailing fixed columns ---------------------------------------------

    @property
    def col_key_strengths(self) -> int:
        return self.confidence_end

    @property
    def col_opportunities(self) -> int:
        return self.confidence_end + 1

    @property
    def col_call_summary(self) -> int:
        return self.confidence_end + 2

    @property
    def col_caller_name(self) -> int:
        return self.confidence_end + 3

    @property
    def col_caller_phone(self) -> int:
        return self.confidence_end + 4

    @property
    def col_source(self) -> int:
        return self.confidence_end + 5

    @property
    def col_eval_approved_at(self) -> int:
        """New trailing column. Holds the eval/approval-time UTC string
        that used to live in ``COL_TIMESTAMP`` before the call-time
        initiative (see module docstring). The writer fills it at
        Stage 4 (`finalize_to_analyst_history`). Blank on pre-cutover
        rows until PR-2's backfill script runs; ``load_and_clean``
        falls back to col C during that window."""
        return self.confidence_end + 6

    @property
    def total_width(self) -> int:
        return PREFIX_WIDTH + 3 * self.n + TRAILING_WIDTH

    # --- Internal -----------------------------------------------------------

    def _check_idx(self, i: int) -> None:
        if not 0 <= i < self.n:
            raise IndexError(f"section index {i} out of range [0, {self.n})")


# ---------------------------------------------------------------------------
# Column-letter helpers (used by sheets_service for A1-style ranges)
# ---------------------------------------------------------------------------

def col_index_to_letter(idx: int) -> str:
    """Spreadsheet column letter for a 0-based index. 0 -> 'A', 26 -> 'AA'."""
    if idx < 0:
        raise ValueError(f"column index must be non-negative, got {idx}")
    letters = ""
    n = idx
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return letters


def col_letter_to_index(letter: str) -> int:
    """0-based index for a spreadsheet column letter. 'A' -> 0, 'AA' -> 26."""
    n = 0
    for c in letter.upper():
        if not "A" <= c <= "Z":
            raise ValueError(f"invalid column letter: {letter!r}")
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n - 1
