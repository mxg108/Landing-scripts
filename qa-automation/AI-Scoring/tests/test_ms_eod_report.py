"""Pure-logic checkpoints for scripts/ms_eod_report.py (no network, no Sheets).

Pins the rules that were reverse-engineered against Dialpad's own daily
export on 2026-09-01/02 (fixtures/dialpad_stats_headers.md): entry-point
rows only, category-based outcomes, the 6 s short-abandon rule, the ≤30 s
service-level count, the SL% denominator, shift windows incl. the night
straddle, and both agent counts.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "ms_eod_report",
    Path(__file__).resolve().parent.parent / "scripts" / "ms_eod_report.py")
eod = importlib.util.module_from_spec(_spec)
sys.modules["ms_eod_report"] = eod   # dataclasses resolve cls.__module__ via sys.modules
_spec.loader.exec_module(eod)  # type: ignore[union-attr]


def _cc(call_id, started, category, *, direction="inbound", ended=None, connected=None,
        queued=None, tta=None, categories="", talk=None):
    return {
        "call_id": call_id, "target_kind": "CallCenter", "date_started": started,
        "category": category, "direction": direction, "categories": categories,
        "external_number": "+15550001111", "internal_number": "+12058528798",
        "date_queued": queued or "", "date_first_rang": "", "date_connected": connected or "",
        "date_ended": ended or "", "time_to_answer": "" if tta is None else str(tta),
        "talk_duration": "" if talk is None else str(talk), "voicemail": "false",
        "callback_type": "", "entry_point_call_id": "", "email": "", "name": "",
    }


def _leg(call_id, entry, started, email, name="Agent", connected=None):
    return {
        "call_id": call_id, "target_kind": "UserProfile", "date_started": started,
        "entry_point_call_id": entry, "email": email, "name": name,
        "date_connected": connected or "", "categories": "answered,inbound",
        "category": "incoming", "direction": "inbound",
    }


def _duty(ts, email, status, rid=None, name="Agent"):
    return {"date": ts, "record_id": rid or f"{email}-{ts}", "email": email,
            "name": name, "on_duty_status": status}


RECORDS = [
    # answered in 2 s (within SL), agent A
    _cc("c1", "2026-09-01 06:10:00", "incoming", connected="2026-09-01 06:10:02",
        ended="2026-09-01 06:15:02", tta=0.03, talk=5.0),
    _leg("l1", "c1", "2026-09-01 06:10:00", "a@x.com", "A", connected="2026-09-01 06:10:02"),
    # answered after 45 s (outside SL), agent B, transferred from A (two legs)
    _cc("c2", "2026-09-01 13:00:00", "incoming", connected="2026-09-01 13:00:45",
        ended="2026-09-01 13:10:45", tta=0.75, categories="answered,transferred_to"),
    _leg("l2a", "c2", "2026-09-01 13:00:00", "a@x.com", "A"),
    _leg("l2b", "c2", "2026-09-01 13:00:10", "b@x.com", "B", connected="2026-09-01 13:00:45"),
    # abandoned after 90 s in queue (counts)
    _cc("c3", "2026-09-01 23:30:00", "abandoned", queued="2026-09-01 23:30:05",
        ended="2026-09-01 23:31:35", categories="abandoned,unanswered,inbound"),
    # SHORT abandoned: ended 4 s after start (excluded from SL denominator)
    _cc("c4", "2026-09-02 02:00:00", "abandoned", ended="2026-09-02 02:00:04",
        categories="abandoned,unanswered,inbound"),   # night shift of 09-01
    # spam
    _cc("c5", "2026-09-01 10:00:00", "missed", ended="2026-09-01 10:00:30",
        categories="missed,spam,inbound"),
    # outbound
    _cc("c6", "2026-09-01 11:00:00", "outgoing", direction="outbound",
        connected="2026-09-01 11:00:05", ended="2026-09-01 11:03:05"),
    # noise: an agent leg with no email is ignored
    {**_leg("l7", "c1", "2026-09-01 06:10:00", ""), "email": ""},
]

DUTY = [
    _duty("2026-09-01 05:55:00", "a@x.com", "available"),
    _duty("2026-09-01 14:00:00", "a@x.com", "unavailable"),
    _duty("2026-09-01 12:30:00", "b@x.com", "available"),
    _duty("2026-09-01 22:10:00", "b@x.com", "unavailable"),
    # off-the-phone specialist: on duty all morning, never takes a call
    _duty("2026-09-01 06:00:00", "spec@x.com", "available", name="Spec"),
    _duty("2026-09-01 15:00:00", "spec@x.com", "unavailable"),
    # night shifter — trailing on-state closes at the horizon
    _duty("2026-09-01 22:00:00", "n@x.com", "occupied"),
]


def test_shift_windows_including_night_straddle():
    assert eod.shift_window(date(2026, 9, 1), "morning") == (
        datetime(2026, 9, 1, 6, 0), datetime(2026, 9, 1, 16, 0))
    assert eod.shift_window(date(2026, 9, 1), "night") == (
        datetime(2026, 9, 1, 22, 0), datetime(2026, 9, 2, 6, 0))
    # 02:00 on the 2nd belongs to the night shift that started on the 1st
    assert eod.shifts_containing(datetime(2026, 9, 2, 2, 0)) == ["night(2026-09-01)"]
    # 13:00 sits in the designed Morning/Afternoon overlap
    assert eod.shifts_containing(datetime(2026, 9, 1, 13, 0)) == ["morning", "afternoon"]
    assert eod.window_label("night") == "22:00–06:00 (+1)"


def test_build_calls_joins_answering_agent_and_ignores_agent_legs_as_calls():
    calls, legs = eod.build_calls(RECORDS)
    by_id = {c.call_id: c for c in calls}
    assert set(by_id) == {"c1", "c2", "c3", "c4", "c5", "c6"}   # entry-point rows only
    assert by_id["c1"].agent_email == "a@x.com"
    assert by_id["c2"].agent_email == "b@x.com" and by_id["c2"].legs == 2   # who CONNECTED wins
    assert by_id["c2"].transferred is True
    assert [l.email for l in legs] == ["a@x.com", "a@x.com", "b@x.com"]


def test_outcome_rules_match_dialpad_definitions():
    calls, _ = eod.build_calls(RECORDS)
    by_id = {c.call_id: c for c in calls}
    assert by_id["c1"].within_sl(30) is True
    assert by_id["c2"].within_sl(30) is False
    assert by_id["c3"].within_sl(30) is None            # not answered → n/a
    assert by_id["c3"].abandoned and not by_id["c3"].short_abandoned
    assert by_id["c3"].wait_s == 90.0                    # ended − queued
    assert by_id["c4"].short_abandoned is True           # 4 s < 6 s
    assert by_id["c5"].spam is True
    assert by_id["c1"].duration_s == 300.0 and by_id["c1"].wait_s == 1.8


def test_summarize_full_day_and_sl_pct_denominator():
    calls, legs = eod.build_calls(RECORDS)
    duty = eod.build_duty(DUTY)
    intervals = eod.duty_intervals(duty, datetime(2026, 9, 2, 6, 0))
    p = eod.summarize_window(calls, legs, intervals, *eod.day_window(date(2026, 9, 1)), 30)
    assert (p["inbound"], p["outbound"], p["answered"], p["abandoned"]) == (4, 1, 2, 1)
    assert (p["missed"], p["spam"], p["short_abandoned"], p["sl_count"]) == (1, 1, 0, 1)
    # sl% = 1 / (4 inbound − 0 short − 1 missed) = 33.3  (c5 is missed; spam is NOT excluded)
    assert p["sl_pct"] == 33.3
    assert p["asa_s"] == round((1.8 + 45) / 2, 1)
    assert p["avg_wait_abandoned_s"] == 90.0 and p["longest_wait_abandoned_s"] == 90.0
    assert p["agents_handled"] == 2            # A and B took legs on the 1st
    assert p["agents_on_duty"] == 4            # A, B, Spec (no calls), night shifter


def test_night_shift_counts_next_morning_short_abandon():
    calls, legs = eod.build_calls(RECORDS)
    intervals = eod.duty_intervals(eod.build_duty(DUTY), datetime(2026, 9, 2, 6, 0))
    p = eod.summarize_window(calls, legs, intervals, *eod.shift_window(date(2026, 9, 1), "night"), 30)
    assert (p["inbound"], p["abandoned"], p["short_abandoned"]) == (2, 2, 1)
    assert p["sl_pct"] == 0.0                  # denominator 2 − 1 − 0 = 1, sl_count 0
    assert p["agents_handled"] == 0 and p["agents_on_duty"] == 2   # B until 22:10, N from 22:00


def test_duty_intervals_and_overlap_minutes():
    intervals = eod.duty_intervals(eod.build_duty(DUTY), datetime(2026, 9, 2, 6, 0))
    assert intervals["spec@x.com"] == [(datetime(2026, 9, 1, 6, 0), datetime(2026, 9, 1, 15, 0))]
    assert intervals["n@x.com"] == [(datetime(2026, 9, 1, 22, 0), datetime(2026, 9, 2, 6, 0))]
    morning = eod.shift_window(date(2026, 9, 1), "morning")
    assert eod.overlap_minutes(intervals["spec@x.com"], *morning) == 540.0
    assert eod.overlap_minutes(intervals["b@x.com"], *morning) == 210.0   # 12:30–16:00


def test_official_day_and_reconcile_against_probe_figures():
    # Dialpad's own daily rows (probe 2026-09-03). Dialpad Analytics showed the
    # owner 74 % (09-01) and 67 % (09-02); only inbound − short − missed fits both.
    sep1 = {"inbound_calls": "396", "outbound_calls": "110", "answered": "353",
            "abandoned": "21", "short_abandoned": "5", "missed": "6", "cancelled": "7",
            "spam": "4", "voicemails": "1", "service_level": "286", "asa": "0.7"}
    sep2 = {"inbound_calls": "345", "outbound_calls": "87", "answered": "289",
            "abandoned": "37", "short_abandoned": "11", "missed": "0", "cancelled": "5",
            "spam": "2", "voicemails": "1", "service_level": "225", "asa": "1.4"}
    p = eod.official_day(sep1)
    assert p["sl_pct"] == 74.3 and round(p["sl_pct"]) == 74
    assert round(eod.official_day(sep2)["sl_pct"]) == 67
    assert p["abandon_pct"] == 5.3 and p["asa_s"] == 42.0
    assert eod.reconcile(p, {**p}) == "OK"
    assert eod.reconcile(p, {**p, "sl_count": 284}).startswith("CHECK: sl_count records=284")


def test_report_rows_are_shaped_like_headers():
    daily = [{"date": "2026-09-01", "inbound_calls": "4", "outbound_calls": "1", "answered": "2",
              "abandoned": "1", "short_abandoned": "0", "missed": "1", "cancelled": "0",
              "spam": "1", "voicemails": "0", "service_level": "1", "asa": "0.39"}]
    users = [{"date": "2026-09-01", "email": "a@x.com", "name": "A", "type": "user",
              "all_calls": "2", "inbound_calls": "2", "outbound_calls": "0", "answered": "2",
              "missed": "0", "abandoned": "0", "ring_no_answer": "0", "talk_duration": "5",
              "hold_duration": "0", "wrapup_duration": "1"}]
    cc = {"name": "MS", "sl_seconds": 30.0, "sl_target_pct": 80.0}
    rep = eod.build_report([date(2026, 9, 1)], RECORDS, DUTY, daily, users, cc)
    assert len(rep.summary_rows) == 4 and all(len(r) == len(eod.SUMMARY_HEADER) for r in rep.summary_rows)
    assert rep.summary_rows[0][eod.SUMMARY_HEADER.index("reconciliation")] == "OK"
    assert [r[1] for r in rep.summary_rows] == ["Full day", "Morning", "Afternoon", "Night"]
    # Calls tab: only calendar-day rows for the requested date (c4 is on the 2nd)
    assert [r[2] for r in rep.call_rows] == ["c1", "c5", "c6", "c2", "c3"]
    assert all(len(r) == len(eod.CALLS_HEADER) for r in rep.call_rows)
    # Agents tab: union of handled + on-duty, specialist present with 0 calls
    agents = {r[2]: r for r in rep.agent_rows}
    assert set(agents) == {"a@x.com", "b@x.com", "spec@x.com", "n@x.com"}
    spec = agents["spec@x.com"]
    assert spec[eod.AGENTS_HEADER.index("handled_calls")] == 0
    assert spec[eod.AGENTS_HEADER.index("on_duty")] == "Y"
    assert spec[eod.AGENTS_HEADER.index("on_duty_min")] == 540.0
    assert spec[eod.AGENTS_HEADER.index("shifts_on_duty")] == "Morning, Afternoon"
    assert all(len(r) == len(eod.AGENTS_HEADER) for r in rep.agent_rows)


def test_hourly_user_rows_are_summed_into_a_daily_figure():
    base = {"date": "2026-09-01", "email": "a@x.com", "name": "A", "type": "user",
            "abandoned": "0", "ring_no_answer": "0", "wrapup_duration": "0"}
    hourly = [
        {**base, "all_calls": "2", "inbound_calls": "2", "outbound_calls": "0", "answered": "2",
         "missed": "0", "talk_duration": "3.5", "hold_duration": "0.25"},
        {**base, "all_calls": "5", "inbound_calls": "3", "outbound_calls": "2", "answered": "1",
         "missed": "2", "talk_duration": "10.1", "hold_duration": "1"},
    ]
    cc = {"name": "MS", "sl_seconds": 30.0, "sl_target_pct": 80.0}
    rep = eod.build_report([date(2026, 9, 1)], RECORDS, DUTY, [], hourly, cc)
    a = next(r for r in rep.agent_rows if r[2] == "a@x.com")
    H = eod.AGENTS_HEADER
    assert a[H.index("all_calls")] == 7 and a[H.index("answered")] == 3 and a[H.index("missed")] == 2
    assert a[H.index("talk_min")] == 13.6 and a[H.index("hold_min")] == 1.25


def test_rounding_is_half_up_like_the_typescript_port():
    assert eod._r1(30.05) == 30.1 and eod._r1(85.35) == 85.4 and eod._r1(1.7999999) == 1.8
