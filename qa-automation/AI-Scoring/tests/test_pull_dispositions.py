"""Unit tests for the C4 Stats-export parse (pure helpers)."""

from __future__ import annotations

from scripts.pull_dispositions import parse_export_csv, split_disposition

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
