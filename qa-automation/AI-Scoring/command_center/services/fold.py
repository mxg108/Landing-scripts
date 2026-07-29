"""Dialpad webhook fold — pure functions from event payload to row shapes.

DispositionDesign §4: the receiver is one route + these fold functions.
This module is deliberately framework-free (no FastAPI, no asyncpg) so the
Sandy re-platform carries it verbatim — the only Dialpad-side change at
that point is the subscription URL.

Payload-shape assumptions are catalogued in DispositionDesign §9 (the
living appendix). Anything observed live that this module doesn't yet
consume gets a line there as it appears.

Hold-cycle derivation rule (§4.1, tested): Dialpad emits NO `unhold`
event — a hold cycle ends at the next `connected` (agent reconnected,
the "reconnect flush") or `hangup` (call ended while on hold).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import jwt

# §4.1.1 monitored call states (webhook_events.state is free TEXT; this
# vocabulary is what the fold understands — unknown states still append
# to webhook_events, they just don't fold).
MONITORED_CALL_STATES = (
    "ringing",
    "connected",
    "hold",
    "hangup",
    "recording",
    "call_transcription",
    "recap_summary",
)

# event_timestamp resolution: an explicit event clock always beats the
# call-lifecycle dates — `date_connected` on a post-hold `connected` event
# may still carry the ORIGINAL connect time, which would misdate the
# reconnect (and invert hold intervals). Lifecycle dates are fallbacks per
# state; `date_started` is the last resort. Epoch-ms values throughout.
_TS_EVENT_FIELDS = ("event_timestamp", "date_updated")
_STATE_TS_FIELDS = {
    "ringing": ("date_rang",),
    "connected": ("date_connected",),
    "hangup": ("date_ended",),
}
# `date` observed live 2026-07-29 as the ONLY clock on agent-status
# events (§9); listed before date_started but after the call-lifecycle
# fields so call-event resolution is unchanged.
_TS_LAST_RESORT = ("date", "date_started")

# Agent-status payloads carry NO `state` key (live 2026-07-29 — the C2
# synthetic wrongly assumed one; the blank '' violated 005's
# webhook_events_state_not_blank CHECK and 500'd every delivery, which
# is the sustained-error pattern that gets Dialpad to auto-disable the
# subscription). The status value's real key is still unverified (§9) —
# these candidates are tried in order after `state` itself; the
# non-blank fallback satisfies the CHECK and keeps the agent-status
# dedupe index (agent_id, state, event_timestamp) meaningful.
_AGENT_STATUS_KEYS = ("new_status", "status", "agent_state", "on_duty_status")
_AGENT_STATUS_FALLBACK = "unknown"


class SignatureError(Exception):
    """The webhook body failed JWT verification."""


def verify_and_decode(raw_body: bytes | str, secret: str) -> dict:
    """Dialpad delivers each event as a JWT signed with the subscription
    secret; the decoded claims ARE the event payload. Raises
    SignatureError on any tamper/expiry/garbage input."""
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8", errors="strict")
    try:
        return jwt.decode(raw_body.strip(), secret, algorithms=["HS256"])
    except jwt.InvalidTokenError as e:
        raise SignatureError(str(e)) from e


def _epoch_ms_to_dt(value: Any) -> Optional[datetime]:
    """Dialpad epoch-ms (int or str) → UTC-aware datetime; None on junk."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


@dataclass(frozen=True)
class NormalizedEvent:
    """The webhook_events row shape, minus received_at/raw (store-side)."""
    event_kind: str                 # 'call' | 'agent_status'
    state: str
    event_timestamp: datetime
    dialpad_call_id: Optional[str]
    dialpad_master_call_id: Optional[str]
    dialpad_entry_point_call_id: Optional[str]
    dialpad_agent_id: Optional[str]
    payload: dict


def normalize_event(payload: dict, *, received_at: Optional[datetime] = None) -> NormalizedEvent:
    """Discriminate call vs agent-status and pull the dedupe/replay keys.

    event_timestamp is from the PAYLOAD (event time), never our clock —
    replay-order correctness depends on it (§4.1). `received_at` is only
    the last-resort fallback for payloads carrying no usable clock at all
    (still NOT NULL-satisfying rather than dropping the event).
    """
    state = str(payload.get("state", "") or "")
    call_id = payload.get("call_id")
    call_id = str(call_id) if call_id not in (None, "") else None

    if call_id is None and not state:
        # Agent-status shape: resolve the status from candidate keys,
        # never blank (see _AGENT_STATUS_KEYS above).
        for field in _AGENT_STATUS_KEYS:
            if payload.get(field) not in (None, ""):
                state = str(payload[field])
                break
        else:
            state = _AGENT_STATUS_FALLBACK

    ts = None
    for field in (
        _TS_EVENT_FIELDS + _STATE_TS_FIELDS.get(state, ()) + _TS_LAST_RESORT
    ):
        ts = _epoch_ms_to_dt(payload.get(field))
        if ts is not None:
            break
    if ts is None:
        ts = received_at or datetime.now(timezone.utc)

    target = payload.get("target") or {}
    agent_id = None
    if isinstance(target, dict) and target.get("type") == "user":
        agent_id = str(target.get("id")) if target.get("id") not in (None, "") else None

    master = payload.get("master_call_id")
    entry = payload.get("entry_point_call_id")
    return NormalizedEvent(
        event_kind="call" if call_id is not None else "agent_status",
        state=state,
        event_timestamp=ts,
        dialpad_call_id=call_id,
        dialpad_master_call_id=str(master) if master not in (None, "") else None,
        dialpad_entry_point_call_id=str(entry) if entry not in (None, "") else None,
        dialpad_agent_id=agent_id,
        payload=payload,
    )


def extract_disposition(payload: dict) -> tuple[Optional[str], Optional[str]]:
    """`call_dispositions` → (category, subdisposition).

    Format is `Category~Subdisposition`; a bare `Category` (agent stopped
    at level 1) yields (category, None). The field's exact live shape is
    verify-on-first-event (§9) — this accepts a string, a list of strings
    (last entry wins: dispositions are re-selectable and the latest is the
    agent's final answer), or a list/dict with a name/value key.
    """
    raw = payload.get("call_dispositions")
    label: Any = None
    if isinstance(raw, str):
        label = raw
    elif isinstance(raw, list) and raw:
        last = raw[-1]
        if isinstance(last, str):
            label = last
        elif isinstance(last, dict):
            label = last.get("name") or last.get("value")
    elif isinstance(raw, dict):
        label = raw.get("name") or raw.get("value")

    if not label or not str(label).strip():
        return (None, None)
    category, _, sub = str(label).strip().partition("~")
    return (category.strip(), sub.strip() or None)


def extract_ai_csat(payload: dict) -> Optional[float]:
    """Dialpad Ai CSAT when the event carries one. Distinct from the
    user-survey CSAT (survey_id-keyed; out of scope)."""
    raw = payload.get("ai_csat")
    if isinstance(raw, dict):
        raw = raw.get("score")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


# Direct payload-field → calls-column copies (set-if-present).
_CALL_FIELD_COLUMNS = (
    ("direction", "direction"),
    ("external_number", "external_number"),
    ("internal_number", "internal_number"),
    ("group_id", "group_id"),
    ("is_transferred", "is_transferred"),
    ("was_recorded", "was_recorded"),
    ("mos_score", "mos_score"),
)

# Lifecycle clocks: payload epoch-ms field → calls column.
_CLOCK_COLUMNS = (
    ("date_started", "started_at"),
    ("date_rang", "rang_at"),
    ("date_connected", "connected_at"),
    ("date_ended", "ended_at"),
)


@dataclass(frozen=True)
class FoldResult:
    """What one call event does to durable state.

    call_columns: command_center.calls columns to set (upsert by
    (team_id, dialpad_call_id); absent keys are left untouched so an
    event that doesn't know a field never nulls it).
    hold_interval: a COMPLETED hold cycle to insert, or None.
    """
    call_columns: dict
    hold_interval: Optional[dict]


def fold_call_event(
    prior: Optional[dict],
    ev: NormalizedEvent,
    team_id: str,
) -> FoldResult:
    """Fold one normalized call event onto the prior calls-row snapshot.

    `prior` is the current row's {last_state, last_state_at,
    total_hold_seconds} (None for a first-seen call). Pure — the caller
    owns SELECT ... FOR UPDATE / upsert / insert.
    """
    payload = ev.payload
    cols: dict[str, Any] = {
        "team_id": team_id,
        "dialpad_call_id": ev.dialpad_call_id,
        "last_state": ev.state,
        "last_state_at": ev.event_timestamp,
    }
    if ev.dialpad_master_call_id:
        cols["dialpad_master_call_id"] = ev.dialpad_master_call_id
    if ev.dialpad_entry_point_call_id:
        cols["dialpad_entry_point_call_id"] = ev.dialpad_entry_point_call_id
    if ev.dialpad_agent_id:
        cols["dialpad_agent_id"] = ev.dialpad_agent_id

    target = payload.get("target") or {}
    if isinstance(target, dict):
        if target.get("type") == "user" and target.get("name"):
            cols["agent_name"] = str(target["name"])
        elif target.get("name"):
            cols["target_name"] = str(target["name"])
            cols["target_type"] = str(target.get("type") or "") or None

    contact = payload.get("contact") or {}
    if isinstance(contact, dict):
        if contact.get("name"):
            cols["caller_name"] = str(contact["name"])
        if contact.get("email"):
            cols["caller_email"] = str(contact["email"])

    for field, col in _CALL_FIELD_COLUMNS:
        if payload.get(field) not in (None, ""):
            cols[col] = payload[field]

    for field, col in _CLOCK_COLUMNS:
        dt = _epoch_ms_to_dt(payload.get(field))
        if dt is not None:
            cols[col] = dt
    if payload.get("duration") not in (None, ""):
        try:
            cols["total_duration_ms"] = int(float(payload["duration"]))
        except (ValueError, TypeError):
            pass

    category, sub = extract_disposition(payload)
    if category:
        cols["disposition_category"] = category
        cols["disposition"] = sub
        cols["disposition_source"] = "webhook"

    ai_csat = extract_ai_csat(payload)
    if ai_csat is not None:
        cols["ai_csat"] = ai_csat

    # Hold-cycle materialization: prior state 'hold' closes at this event
    # if it is a connected (reconnect flush) or hangup. The cycle's start
    # is the hold event's own timestamp (prior.last_state_at).
    hold_interval = None
    if (
        prior is not None
        and prior.get("last_state") == "hold"
        and prior.get("last_state_at") is not None
        and ev.state in ("connected", "hangup")
    ):
        started_at = prior["last_state_at"]
        # Clamp against clock inversion (a closing event misdated before
        # the hold start — e.g. a lifecycle-date fallback): record a
        # zero-length cycle rather than violate the ordering CHECK or
        # silently drop the state transition.
        ended_at = max(started_at, ev.event_timestamp)
        seconds = int((ended_at - started_at).total_seconds())
        hold_interval = {
            "team_id": team_id,
            "dialpad_call_id": ev.dialpad_call_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "seconds": seconds,
            "ended_by": ev.state,
        }
        cols["total_hold_seconds"] = int(prior.get("total_hold_seconds") or 0) + seconds

    return FoldResult(call_columns=cols, hold_interval=hold_interval)
