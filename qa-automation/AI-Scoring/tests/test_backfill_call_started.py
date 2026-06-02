"""Unit tests for pure helpers in scripts/backfill_call_started.py.

The script's main loop is I/O-heavy (gspread + httpx); coverage of the
network/sheet path is via dry-run smoke against the real Sheet in
staging. Here we lock down the pure helpers: eval_id parsing, sheet
format, column-letter math, and the resume-log round-trip.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_AI_SCORING = Path(__file__).resolve().parent.parent
if str(_AI_SCORING) not in sys.path:
    sys.path.insert(0, str(_AI_SCORING))

import pytest

# Import the module via path so we don't depend on whatever sys.path
# the test runner has at import time.
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "backfill_call_started",
    _AI_SCORING / "scripts" / "backfill_call_started.py",
)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


# ---------------------------------------------------------------------------
# _eval_id_from_link — must match services/team_stats.py:_parse_row
# ---------------------------------------------------------------------------

def test_eval_id_from_link_strips_long_call_suffix():
    """The [LONG CALL] suffix the writer appends for >25min calls must
    not leak into the eval_id we hand to Dialpad."""
    eid = backfill._eval_id_from_link(
        "https://dialpad.com/callhistory/callreview/12345 [LONG CALL]"
    )
    assert eid == "12345"


def test_eval_id_from_link_strips_query_string():
    eid = backfill._eval_id_from_link(
        "https://dialpad.com/callhistory/callreview/abc-123?source=email"
    )
    assert eid == "abc-123"


def test_eval_id_from_link_strips_trailing_slash():
    eid = backfill._eval_id_from_link(
        "https://dialpad.com/callhistory/callreview/9876/"
    )
    assert eid == "9876"


def test_eval_id_from_link_empty_input_returns_empty():
    assert backfill._eval_id_from_link("") == ""
    assert backfill._eval_id_from_link("   ") == ""


# ---------------------------------------------------------------------------
# _format_call_time — matches sheets_service._format_call_started so the
# backfill produces cells identical to what the writer emits for new
# approvals. Drift would create two slightly different formats in col C
# and surprise downstream readers.
# ---------------------------------------------------------------------------

def test_format_call_time_renders_utc_clock_string():
    dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert backfill._format_call_time(dt) == "06/01/2026 12:00:00"


def test_format_call_time_none_renders_blank():
    """None → "" so a row with no Dialpad answer isn't written with a
    sentinel — the skip path handles those, the writer shouldn't fall
    through here."""
    assert backfill._format_call_time(None) == ""


def test_format_call_time_matches_writer_helper():
    """Drift guard: if sheets_service's helper format ever changes, this
    test fires so we update both in lockstep."""
    from backend.services.sheets_service import _format_call_started
    dt = datetime(2026, 5, 31, 3, 44, 15, tzinfo=timezone.utc)
    assert backfill._format_call_time(dt) == _format_call_started(dt)


# ---------------------------------------------------------------------------
# _col_letter — 0-based index → spreadsheet column letter
# ---------------------------------------------------------------------------

def test_col_letter_basic_ascii_range():
    assert backfill._col_letter(0) == "A"
    assert backfill._col_letter(2) == "C"
    assert backfill._col_letter(25) == "Z"


def test_col_letter_wraps_to_two_letters():
    """N=19 (sales) → col_eval_approved_at = 6 + 3*19 + 6 = 69 = column BR."""
    assert backfill._col_letter(26) == "AA"
    assert backfill._col_letter(27) == "AB"
    assert backfill._col_letter(69) == "BR"


def test_col_letter_matches_history_layout_helper():
    """Drift guard against the local copy diverging from the production
    col_index_to_letter (in backend/config/history_layout.py)."""
    from backend.config.history_layout import col_index_to_letter
    for idx in (0, 5, 25, 26, 42, 69, 100):
        assert backfill._col_letter(idx) == col_index_to_letter(idx)


# ---------------------------------------------------------------------------
# Resume log round-trip — _append_resume_entry + _load_resume_set
# ---------------------------------------------------------------------------

def test_resume_log_round_trip(tmp_path, monkeypatch):
    """Writing N entries and reading them back yields {(team_id, row_num)}."""
    log_path = tmp_path / ".backfill-log"
    monkeypatch.setattr(backfill, "BACKFILL_LOG", log_path)

    backfill._append_resume_entry("member_support", 42, "eval-A")
    backfill._append_resume_entry("sales", 7, "eval-B")
    backfill._append_resume_entry("member_support", 100, "eval-C")

    seen = backfill._load_resume_set()
    assert seen == {
        ("member_support", 42),
        ("sales", 7),
        ("member_support", 100),
    }


def test_resume_log_missing_file_returns_empty_set(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "BACKFILL_LOG", tmp_path / "nope.log")
    assert backfill._load_resume_set() == set()


def test_resume_log_tolerates_malformed_lines(tmp_path, monkeypatch):
    """Don't crash if someone hand-edited the log into a malformed state.
    Skip the bad line, keep the good ones."""
    log_path = tmp_path / ".backfill-log"
    log_path.write_text(
        "garbage\n"
        "2026-06-01T00:00:00Z\tmember_support\t42\teval-A\n"
        "2026-06-01T00:00:00Z\tsales\tNOT_AN_INT\teval-B\n"
        "2026-06-01T00:00:00Z\tsales\t9\teval-C\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(backfill, "BACKFILL_LOG", log_path)
    seen = backfill._load_resume_set()
    assert seen == {("member_support", 42), ("sales", 9)}


# ---------------------------------------------------------------------------
# Skipped CSV — header on first write, append after
# ---------------------------------------------------------------------------

def test_skipped_csv_writes_header_on_first_row(tmp_path, monkeypatch):
    csv_path = tmp_path / ".backfill-skipped.csv"
    monkeypatch.setattr(backfill, "SKIPPED_CSV", csv_path)

    backfill._append_skipped(42, "Mich Palacios", "abc-123", "no_date_connected")
    backfill._append_skipped(43, "Star Rep", "def-456", "dialpad_error:HTTPError")

    content = csv_path.read_text(encoding="utf-8")
    lines = content.strip().splitlines()
    assert lines[0] == "row_num,agent,eval_id,reason"
    assert lines[1].startswith("42,Mich Palacios,abc-123,")
    assert lines[2].startswith("43,Star Rep,def-456,")
