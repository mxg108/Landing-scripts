"""Unit tests for dialpad_client pure helpers.

Most of dialpad_client is async + HTTP-bound; this file only covers the
pure helpers that don't touch the network. Live-API smokes live under
scripts/.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.services.dialpad_client import (
    _epoch_ms_to_utc_datetime,
    build_dialpad_link,
    compute_call_duration,
    parse_transcript_payload,
)


# ---------------------------------------------------------------------------
# parse_transcript_payload — DispositionDesign C0 moment parse.
# Payload shape probed 2026-07-19: moment lines carry the TYPE in `content`
# (`moment_type` is None) and the AGENT in `name`.
# ---------------------------------------------------------------------------

def _real_payload():
    return {
        "call_id": "DP123",
        "lines": [
            {
                "type": "transcript",
                "name": "Jane Agent",
                "content": "Thank you for calling Landing, this is Jane.",
                "time": "2026-07-15T10:00:00Z",
            },
            {
                "type": "moment",
                "moment_type": None,
                "name": "Jane Agent",
                "content": "call_purpose",
                "time": "2026-07-15T10:00:30Z",
            },
            {
                "type": "transcript",
                "name": "Member",
                "content": "Hi, my smart lock is not working.",
                "time": "2026-07-15T10:00:42Z",
            },
            {
                "type": "real_time_moment",
                "moment_type": None,
                "name": "Jane Agent",
                "content": "whole_call_summary_fragment",
                "time": "2026-07-15T10:01:00Z",
            },
            {
                "type": "moment",
                "moment_type": None,
                "name": "Jane Agent",
                "content": "call_disposition",
                "time": "2026-07-15T10:05:12Z",
            },
        ],
    }


def test_moment_type_parsed_from_content_not_name():
    """The rotted filter matched `moment_type`/`name`; current payloads put
    the type in `content` and the agent in `name`. The agent's name must
    never surface as a marker type again."""
    out = parse_transcript_payload(_real_payload())
    types = [m["type"] for m in out["moments_display"]]
    assert types == ["call_purpose", "whole_call_summary_fragment", "call_disposition"]
    assert all(m["agent"] == "Jane Agent" for m in out["moments_display"])


def test_full_marker_set_kept_nothing_filtered():
    """DispositionDesign §2: filtering is a prompt decision, never a storage
    decision. Former FILTERED_MOMENT_TYPES members (call_disposition,
    whole_call_summary_fragment) are kept — the full set feeds
    dialpad_call_metadata.moments."""
    out = parse_transcript_payload(_real_payload())
    assert len(out["moments_display"]) == 3
    kept = {m["type"] for m in out["moments_display"]}
    assert "call_disposition" in kept
    assert "whole_call_summary_fragment" in kept


def test_markers_never_reach_prompt_text():
    """C0 checkpoint: zero marker lines in the prompt. The parse emits no
    moments_text at all — transcript_text is the only prompt-bound string,
    and it carries no `[...] at <ts>` marker lines."""
    out = parse_transcript_payload(_real_payload())
    assert "moments_text" not in out
    assert "] at " not in out["transcript_text"]
    assert "call_disposition" not in out["transcript_text"]


def test_moment_type_field_wins_when_populated():
    """Older/other payload shapes populated `moment_type`; it takes
    precedence over `content` when present."""
    out = parse_transcript_payload({
        "lines": [
            {
                "type": "moment",
                "moment_type": "ai_csat_reboot",
                "name": "Jane Agent",
                "content": "ignored",
                "time": "2026-07-15T10:00:00Z",
            },
        ],
    })
    assert [m["type"] for m in out["moments_display"]] == ["ai_csat_reboot"]


def test_typeless_marker_skipped():
    """No moment_type and no content → unclassifiable; skipped rather than
    persisting an empty-string type."""
    out = parse_transcript_payload({
        "lines": [
            {"type": "moment", "moment_type": None, "name": "Jane Agent",
             "content": "", "time": "2026-07-15T10:00:00Z"},
        ],
    })
    assert out["moments_display"] == []


def test_marker_timestamps_relative_to_call_start():
    """mm:ss offsets are computed from the first transcript line; the raw
    ISO time is kept alongside for persistence."""
    out = parse_transcript_payload(_real_payload())
    disposition = out["moments_display"][-1]
    assert disposition["timestamp"] == "5:12"
    assert disposition["time"] == "2026-07-15T10:05:12Z"


def test_marker_before_first_transcript_line_keeps_raw_time():
    """A marker arriving before any transcript line has no call-start to
    offset from — mm:ss stays blank but the raw time survives."""
    out = parse_transcript_payload({
        "lines": [
            {"type": "moment", "moment_type": None, "name": "Jane Agent",
             "content": "call_purpose", "time": "2026-07-15T10:00:00Z"},
            {"type": "transcript", "name": "Jane Agent", "content": "Hello.",
             "time": "2026-07-15T10:00:05Z"},
        ],
    })
    marker = out["moments_display"][0]
    assert marker["timestamp"] == ""
    assert marker["time"] == "2026-07-15T10:00:00Z"


def test_transcript_lines_unaffected_by_moment_fix():
    out = parse_transcript_payload(_real_payload())
    assert out["transcript_text"] == (
        "Jane Agent: Thank you for calling Landing, this is Jane.\n"
        "Member: Hi, my smart lock is not working."
    )
    assert out["transcript_display"][0]["timestamp"] == "0:00"
    assert out["transcript_display"][1]["timestamp"] == "0:42"


# ---------------------------------------------------------------------------
# build_dialpad_link — entry_point_call_id precedence
# ---------------------------------------------------------------------------

def test_build_dialpad_link_prefers_entry_point_id():
    """Dialpad's recording page is keyed by entry_point_call_id. When the
    caller supplies it, the link must use it, not the per-leg call_id."""
    link = build_dialpad_link(
        call_id="leg-123",
        entry_point_call_id="entry-999",
    )
    assert link == "https://dialpad.com/callhistory/callreview/entry-999"


def test_build_dialpad_link_falls_back_to_call_id():
    """Direct calls (and callers that haven't been updated) pass no
    entry_point_call_id; the link uses call_id directly."""
    link = build_dialpad_link("leg-123")
    assert link == "https://dialpad.com/callhistory/callreview/leg-123"


def test_build_dialpad_link_treats_empty_entry_point_as_absent():
    """An empty/whitespace entry_point_call_id should NOT produce a
    /callreview/ URL (the trailing path would be empty/whitespace and
    404 in Dialpad). Fall back to call_id instead."""
    link = build_dialpad_link("leg-123", entry_point_call_id="")
    assert link == "https://dialpad.com/callhistory/callreview/leg-123"
    link = build_dialpad_link("leg-123", entry_point_call_id="   ")
    assert link == "https://dialpad.com/callhistory/callreview/leg-123"


def test_build_dialpad_link_default_entry_point_is_empty():
    """Default-arg behavior unchanged from pre-refactor for existing
    callers that pass only call_id."""
    link = build_dialpad_link("leg-123")
    assert "leg-123" in link
    assert "callreview" in link


# ---------------------------------------------------------------------------
# _epoch_ms_to_utc_datetime + compute_call_duration — call-time initiative
# plumbing (PR-1 of references/CallTimeOnAnalystHistory.md).
# ---------------------------------------------------------------------------

def test_epoch_ms_to_utc_datetime_roundtrips_to_utc_aware():
    """A known epoch-ms value parses to the expected UTC instant. Result
    is tz-aware so downstream callers don't trip the naive-vs-aware
    pitfall the chiclet PR fixed for /team/evals timestamps."""
    expected = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    epoch_ms = int(expected.timestamp() * 1000)
    dt = _epoch_ms_to_utc_datetime(epoch_ms)
    assert dt is not None
    assert dt.tzinfo is timezone.utc
    assert dt == expected


def test_epoch_ms_to_utc_datetime_accepts_string_epoch():
    """Dialpad returns date_connected as a string in some payload shapes;
    int casting must handle that without raising."""
    expected = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    dt = _epoch_ms_to_utc_datetime(str(int(expected.timestamp() * 1000)))
    assert dt is not None
    assert dt == expected


def test_epoch_ms_to_utc_datetime_none_for_missing_inputs():
    """None / empty-string / unparseable → None so the writer sees 'we
    don't know' and leaves the col C cell blank."""
    assert _epoch_ms_to_utc_datetime(None) is None
    assert _epoch_ms_to_utc_datetime("") is None
    assert _epoch_ms_to_utc_datetime("not-a-number") is None


def test_compute_call_duration_happy_path():
    """date_ended - date_connected → call duration timedelta."""
    started = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 6, 1, 12, 8, 30, tzinfo=timezone.utc)
    assert compute_call_duration(started, ended) == timedelta(minutes=8, seconds=30)


def test_compute_call_duration_returns_none_when_either_missing():
    """Either side missing → None (no guessing, no zero-duration fallback).
    Plumbing-only stub; downstream consumers will read None as 'unknown
    duration' rather than 'zero-length call.'"""
    started = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert compute_call_duration(started, None) is None
    assert compute_call_duration(None, started) is None
    assert compute_call_duration(None, None) is None


# ---------------------------------------------------------------------------
# _format_call_started (sheets_service helper) — exercised here because
# it's a sibling pure function that consumes _epoch_ms_to_utc_datetime's
# output and writes the sheet-side format string.
# ---------------------------------------------------------------------------

def test_format_call_started_renders_utc_clock_string():
    from backend.services.sheets_service import _format_call_started
    dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert _format_call_started(dt) == "06/01/2026 12:00:00"


def test_format_call_started_none_renders_blank():
    """None → "" so the col C cell stays empty for backfill (PR-2) to
    fill later. NOT "None" or some sentinel string."""
    from backend.services.sheets_service import _format_call_started
    assert _format_call_started(None) == ""
