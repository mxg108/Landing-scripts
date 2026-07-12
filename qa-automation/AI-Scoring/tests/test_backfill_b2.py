"""B2 enrichment — pure mapping layer (Dialpad payload → qa.* fields).

The DB/Dialpad loop is exercised by --dry-run against real data; these
tests pin the translations that decide what lands in columns.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_seed_b2.py"
spec = importlib.util.spec_from_file_location("backfill_seed_b2", _SCRIPT)
b2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b2)


def test_eval_id_from_link_strips_suffixes_and_query():
    assert b2.eval_id_from_link(
        "https://dialpad.com/callhistory/callreview/123456?x=1") == "123456"
    assert b2.eval_id_from_link(
        "https://dialpad.com/callhistory/callreview/9876 [LONG CALL]") == "9876"
    assert b2.eval_id_from_link("https://d/c/42/") == "42"
    assert b2.eval_id_from_link(None) == ""
    assert b2.eval_id_from_link("") == ""


def test_recording_urls_normalized_shape():
    details = {
        "recording_details": [{"url": "https://a/1", "id": "r1"},
                              {"id": "r2"},           # no url — dropped
                              {"url": "https://a/2"}],
        "screen_recording_urls": ["https://s/1", ""],
    }
    assert b2.recording_urls_from_details(details) == {
        "audio": ["https://a/1", "https://a/2"],
        "screen": ["https://s/1"],
    }
    assert b2.recording_urls_from_details({}) == {"audio": [], "screen": []}


def test_enrichment_fields_mapping():
    details = {
        "call_id": "111", "master_call_id": "222", "entry_point_call_id": "333",
        "direction": "inbound", "mos_score": 4.2,
        "duration": "183000", "total_duration": 0,
        "caller_name": "Bob", "caller_phone": "+15550001111",
        "raw": {"date_connected": "1750000000000", "date_started": None,
                "date_ended": "1750000183000"},
        "recording_details": [], "screen_recording_urls": [],
    }
    f = b2.enrichment_fields(details)
    assert f["dialpad_call_id"] == "111"
    assert f["dialpad_master_call_id"] == "222"
    assert f["dialpad_entry_point_call_id"] == "333"
    assert f["call_type"] == "inbound"
    assert f["call_duration_ms"] == 183000
    assert f["call_connected_at"].tzinfo is not None
    assert f["call_started_at"] is None
    assert (f["call_ended_at"] - f["call_connected_at"]).total_seconds() == 183
    assert f["caller_name"] == "Bob" and f["mos_score"] == 4.2


def test_enrichment_fields_master_falls_back_to_call_id():
    f = b2.enrichment_fields({"call_id": "111", "master_call_id": "", "raw": {}})
    assert f["dialpad_master_call_id"] == "111"


def test_enrichment_fields_empty_payload_is_all_none():
    f = b2.enrichment_fields({"raw": {}})
    assert all(v is None for k, v in f.items() if k != "recording_urls")
    assert f["recording_urls"] == {"audio": [], "screen": []}
