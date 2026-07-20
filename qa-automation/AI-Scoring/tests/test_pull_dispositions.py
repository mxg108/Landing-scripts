"""Unit tests for the Stats-export parse + pull-loop gating (pure
helpers in backend/services/disposition_pull.py; the CLI re-exports)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend.services.disposition_pull import (
    parse_export_csv,
    periodic_interval_minutes,
    split_disposition,
)

# Mirrors the 2026-07-15 sample export header exactly.
_HEADER = (
    "date_started,call_id,operator_id,operator_name,operator_email,"
    "disposition,direction,external_number,internal_number,date_rang,"
    "date_queued,date_connected,date_ended,target_id,target_kind,"
    "office_id,company_id,salesforce_activity_id,recording_url,note,timezone"
)


def _row(call_id: str, disposition: str) -> str:
    return (
        f"2026-07-15 00:05:54.388365,{call_id},58046,Andrei Trejo,"
        f"a@hellolanding.com,{disposition},inbound,REDACTED,12058528798,"
        "2026-07-15 00:06:02,,2026-07-15 00:06:04,2026-07-15 00:21:51,"
        "56990,CallCenter,48396,47255,,https://dialpad.test/rec,,"
        "America/Mexico_City"
    )


def test_split_category_and_sub():
    assert split_disposition("Access & Entry~Smart-lock failure") == (
        "Access & Entry", "Smart-lock failure",
    )


def test_split_bare_category():
    """Agent stopped at level 1 — the 07-15 sample's dominant shape."""
    assert split_disposition("Reservation & Stay Changes") == (
        "Reservation & Stay Changes", None,
    )


def test_split_empty():
    assert split_disposition("") == (None, None)
    assert split_disposition("   ") == (None, None)


def test_parse_keeps_undispositioned_rows_for_coverage():
    """Rows without a disposition stay in the parse — they are the
    coverage denominator (83% in the 07-15 sample); only unjoinable
    rows (no call_id) drop."""
    text = "\n".join([
        _HEADER,
        _row("111", "Billing~Refund"),
        _row("222", ""),
        _row("", "Billing"),
    ])
    records = parse_export_csv(text)
    assert [r.call_id for r in records] == ["111", "222"]
    assert records[0].disposition_category == "Billing"
    assert records[0].disposition == "Refund"
    assert records[1].disposition_category is None


def test_parse_real_sample_shape():
    text = "\n".join([_HEADER, _row("6529997769220096", "Reservation & Stay Changes")])
    (record,) = parse_export_csv(text)
    assert record.call_id == "6529997769220096"
    assert record.disposition_category == "Reservation & Stay Changes"
    assert record.disposition is None


def test_parse_localizes_naive_clocks_via_row_timezone():
    """Export timestamps are NAIVE in the row's own `timezone` column
    (America/Mexico_City in the sample) — the parse localizes them so
    the created calls row carries UTC-aware clocks."""
    text = "\n".join([_HEADER, _row("111", "Billing")])
    (record,) = parse_export_csv(text)
    expected = datetime(2026, 7, 15, 0, 6, 4,
                        tzinfo=ZoneInfo("America/Mexico_City"))
    assert record.connected_at == expected
    assert record.connected_at.astimezone(timezone.utc).tzinfo is timezone.utc
    assert record.direction == "inbound"
    assert record.agent_name == "Andrei Trejo"


def test_periodic_interval_gating(monkeypatch):
    """The in-app loop only runs when CC_STATS_PULL_INTERVAL_MIN is a
    positive number — local dev and tests never hit the Stats API."""
    monkeypatch.delenv("CC_STATS_PULL_INTERVAL_MIN", raising=False)
    assert periodic_interval_minutes() is None
    for bad in ("", "abc", "0", "-5"):
        monkeypatch.setenv("CC_STATS_PULL_INTERVAL_MIN", bad)
        assert periodic_interval_minutes() is None
    monkeypatch.setenv("CC_STATS_PULL_INTERVAL_MIN", "30")
    assert periodic_interval_minutes() == 30.0
