"""Unit tests for the Command Center webhook fold (pure functions).

DispositionDesign §4 / C2 checkpoint: signature verify + fold + hold-cycle
derivation, incl. the reconnect flush. The DB write-through is exercised
in tests/integration/test_cc_ingest.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import jwt as pyjwt
import pytest

from command_center.services import fold
from command_center.routes.webhooks import resolve_team

SECRET = "test-subscription-secret"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


T0 = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)


def _at(seconds: int) -> datetime:
    return datetime.fromtimestamp(T0.timestamp() + seconds, tz=timezone.utc)


def _call_event(state: str, *, at: int, call_id: str = "DP-1", **extra) -> dict:
    return {
        "call_id": call_id,
        "state": state,
        "event_timestamp": _ms(_at(at)),
        "date_started": _ms(T0),
        **extra,
    }


# ---------------------------------------------------------------------------
# Signature verify
# ---------------------------------------------------------------------------


def test_signed_body_roundtrips():
    payload = {"call_id": "DP-1", "state": "ringing"}
    token = pyjwt.encode(payload, SECRET, algorithm="HS256")
    assert fold.verify_and_decode(token, SECRET) == payload


def test_wrong_secret_rejected():
    token = pyjwt.encode({"call_id": "DP-1"}, "attacker-secret", algorithm="HS256")
    with pytest.raises(fold.SignatureError):
        fold.verify_and_decode(token, SECRET)


def test_tampered_body_rejected():
    token = pyjwt.encode({"call_id": "DP-1"}, SECRET, algorithm="HS256")
    header, claims, sig = token.split(".")
    with pytest.raises(fold.SignatureError):
        fold.verify_and_decode(f"{header}.{claims}x.{sig}", SECRET)


def test_garbage_body_rejected():
    with pytest.raises(fold.SignatureError):
        fold.verify_and_decode(b"not-a-jwt-at-all", SECRET)


# ---------------------------------------------------------------------------
# normalize_event
# ---------------------------------------------------------------------------


def test_call_event_kind_from_call_id():
    ev = fold.normalize_event(_call_event("ringing", at=0))
    assert ev.event_kind == "call"
    assert ev.dialpad_call_id == "DP-1"


def test_agent_status_kind_without_call_id():
    ev = fold.normalize_event({
        "state": "available",
        "target": {"id": 42, "type": "user"},
        "event_timestamp": _ms(T0),
    })
    assert ev.event_kind == "agent_status"
    assert ev.dialpad_agent_id == "42"


def test_event_clock_beats_lifecycle_dates():
    """A post-hold `connected` event may still carry the ORIGINAL
    date_connected — the explicit event clock must win or the reconnect
    is misdated and hold intervals invert."""
    ev = fold.normalize_event({
        "call_id": "DP-1",
        "state": "connected",
        "date_connected": _ms(_at(30)),        # original connect
        "event_timestamp": _ms(_at(297)),      # the reconnect
    })
    assert ev.event_timestamp == _at(297)


def test_lifecycle_date_fallback_per_state():
    ev = fold.normalize_event({
        "call_id": "DP-1",
        "state": "hangup",
        "date_ended": _ms(_at(720)),
        "date_started": _ms(T0),
    })
    assert ev.event_timestamp == _at(720)


def test_ids_stringified():
    ev = fold.normalize_event({
        "call_id": 123,
        "master_call_id": 456,
        "entry_point_call_id": 789,
        "state": "ringing",
        "event_timestamp": _ms(T0),
    })
    assert ev.dialpad_call_id == "123"
    assert ev.dialpad_master_call_id == "456"
    assert ev.dialpad_entry_point_call_id == "789"


# ---------------------------------------------------------------------------
# extract_disposition — Category~Sub split
# ---------------------------------------------------------------------------


def test_disposition_category_and_sub():
    assert fold.extract_disposition(
        {"call_dispositions": "Access & Entry~Smart-lock failure"}
    ) == ("Access & Entry", "Smart-lock failure")


def test_disposition_bare_category():
    assert fold.extract_disposition({"call_dispositions": "Billing"}) == (
        "Billing", None,
    )


def test_disposition_list_last_wins():
    """Dispositions are re-selectable; the latest is the agent's final
    answer."""
    assert fold.extract_disposition(
        {"call_dispositions": ["Billing~Refund", "Access & Entry~Lockout"]}
    ) == ("Access & Entry", "Lockout")


def test_disposition_dict_name_key():
    assert fold.extract_disposition(
        {"call_dispositions": [{"name": "Billing~Refund"}]}
    ) == ("Billing", "Refund")


def test_disposition_absent_or_empty():
    assert fold.extract_disposition({}) == (None, None)
    assert fold.extract_disposition({"call_dispositions": ""}) == (None, None)
    assert fold.extract_disposition({"call_dispositions": []}) == (None, None)


# ---------------------------------------------------------------------------
# extract_ai_csat
# ---------------------------------------------------------------------------


def test_ai_csat_numeric_and_string():
    assert fold.extract_ai_csat({"ai_csat": 4.5}) == 4.5
    assert fold.extract_ai_csat({"ai_csat": "4.5"}) == 4.5


def test_ai_csat_nested_score():
    assert fold.extract_ai_csat({"ai_csat": {"score": 3.0}}) == 3.0


def test_ai_csat_absent_or_junk():
    assert fold.extract_ai_csat({}) is None
    assert fold.extract_ai_csat({"ai_csat": "n/a"}) is None


# ---------------------------------------------------------------------------
# fold_call_event — hold-cycle derivation (§4.1 rule: no unhold event;
# a cycle ends at the next connected or hangup)
# ---------------------------------------------------------------------------


def _fold(state: str, *, at: int, prior: dict | None = None, **extra):
    ev = fold.normalize_event(_call_event(state, at=at, **extra))
    return fold.fold_call_event(prior, ev, "member_support")


def test_first_event_creates_call_columns():
    result = _fold("ringing", at=0)
    cols = result.call_columns
    assert cols["team_id"] == "member_support"
    assert cols["dialpad_call_id"] == "DP-1"
    assert cols["last_state"] == "ringing"
    assert cols["last_state_at"] == _at(0)
    assert result.hold_interval is None


def test_connected_without_prior_hold_no_interval():
    prior = {"last_state": "ringing", "last_state_at": _at(0), "total_hold_seconds": 0}
    result = _fold("connected", at=30, prior=prior)
    assert result.hold_interval is None
    assert "total_hold_seconds" not in result.call_columns


def test_reconnect_flush_closes_cycle():
    """hold → connected: the cycle ends at the reconnect, ended_by
    'connected', and the rollup accumulates."""
    prior = {"last_state": "hold", "last_state_at": _at(195), "total_hold_seconds": 0}
    result = _fold("connected", at=297, prior=prior)
    hi = result.hold_interval
    assert hi is not None
    assert hi["started_at"] == _at(195)
    assert hi["ended_at"] == _at(297)
    assert hi["seconds"] == 102
    assert hi["ended_by"] == "connected"
    assert result.call_columns["total_hold_seconds"] == 102


def test_hangup_on_hold_closes_cycle():
    """A call that ends while on hold closes its final cycle with
    ended_by='hangup'."""
    prior = {"last_state": "hold", "last_state_at": _at(662), "total_hold_seconds": 102}
    result = _fold("hangup", at=720, prior=prior)
    hi = result.hold_interval
    assert hi is not None
    assert hi["seconds"] == 58
    assert hi["ended_by"] == "hangup"
    assert result.call_columns["total_hold_seconds"] == 160


def test_hold_event_opens_cycle_without_row():
    """The hold row materializes only when the cycle CLOSES."""
    prior = {"last_state": "connected", "last_state_at": _at(30), "total_hold_seconds": 0}
    result = _fold("hold", at=195, prior=prior)
    assert result.hold_interval is None
    assert result.call_columns["last_state"] == "hold"
    assert result.call_columns["last_state_at"] == _at(195)


def test_inverted_clock_clamps_to_zero_cycle():
    """A closing event misdated before the hold start records a
    zero-length cycle rather than violating the ordering CHECK."""
    prior = {"last_state": "hold", "last_state_at": _at(195), "total_hold_seconds": 0}
    result = _fold("connected", at=100, prior=prior)
    hi = result.hold_interval
    assert hi is not None
    assert hi["started_at"] == hi["ended_at"] == _at(195)
    assert hi["seconds"] == 0


def test_disposition_and_ai_csat_fold_into_columns():
    result = _fold(
        "hangup", at=720,
        call_dispositions="Access & Entry~Smart-lock failure",
        ai_csat=4.5,
    )
    cols = result.call_columns
    assert cols["disposition_category"] == "Access & Entry"
    assert cols["disposition"] == "Smart-lock failure"
    assert cols["disposition_source"] == "webhook"
    assert cols["ai_csat"] == 4.5


def test_no_disposition_no_columns():
    """The absence path: no disposition keys at all — the upsert must not
    touch the disposition columns (never nulls a stats_pull backfill)."""
    result = _fold("hangup", at=720)
    for col in ("disposition_category", "disposition", "disposition_source", "ai_csat"):
        assert col not in result.call_columns


# ---------------------------------------------------------------------------
# resolve_team — config-mapped Dialpad ids
# ---------------------------------------------------------------------------


def test_resolve_team_by_call_center_target():
    payload = {"target": {"id": 4716644561813504, "type": "callcenter"}}
    assert resolve_team(payload) == "member_support"


def test_resolve_team_single_team_fallback():
    """v1 has exactly one configured team; an event whose target is the
    individual user still belongs to it."""
    payload = {"target": {"id": 999999, "type": "user"}}
    assert resolve_team(payload) == "member_support"
