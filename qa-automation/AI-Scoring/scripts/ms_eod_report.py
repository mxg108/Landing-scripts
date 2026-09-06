"""Member Support EOD report → Google Sheet (Dialpad Stats API).

For a range of LOCAL dates (America/Mexico_City) pulls the Member Support
call center's Dialpad Stats exports and writes three tabs of the
destination spreadsheet — rows for the processed dates are replaced, other
dates and other tabs are never touched:

  Summary       one row per (date, window): "Full day" from Dialpad's own
                daily stats export (official numbers) + Morning / Afternoon /
                Night computed from the per-call records export
  Calls         one row per entry-point call (inbound + outbound) with the
                answering agent joined in — caller number, direction,
                outcome, wait, duration, shift(s), Dialpad link
  Agents_Daily  one row per (date, agent): per-user daily stats UNION agents
                who were on duty without handling a call (off-the-phone
                specialists), with on-duty minutes and first/last times

Definitions (probed live 2026-09-03; headers + reconciliation table in
sandy-qa/references/fixtures/dialpad_stats_headers.md):

  * The records export has two row kinds per call. ``target_kind =
    CallCenter`` is the entry-point leg (one per call; ``entry_point_call_id``
    empty). ``target_kind = UserProfile`` is an agent leg pointing back via
    ``entry_point_call_id``. Every call count here is over CallCenter rows.
  * Abandoned  = ``category == "abandoned"`` (reproduces Dialpad's daily
    ``abandoned`` exactly). Answered = ``category == "incoming"``.
  * Short abandoned = abandoned AND (date_ended − date_started) <
    SHORT_ABANDON_SECONDS (6 s reproduces Dialpad's ``short_abandoned`` on
    both probe days; reverse-engineered — the reconciliation column flags
    drift).
  * Service level COUNT = answered inbound calls with ``time_to_answer`` ≤
    the call center's ``cc_service_level_seconds`` (30 s; fetched live each
    run). Dialpad's export column ``service_level`` is this count, not a %.
  * Service level %  = sl_count / (inbound − short_abandoned − missed).
    Dialpad does not document the denominator; this is the only candidate
    that reproduces BOTH figures Dialpad Analytics showed the owner
    (2026-09-01 = 74 %, 2026-09-02 = 67 %; spam is NOT excluded). Change
    SL_DENOMINATOR_EXCLUDES if a later day disagrees — the Summary tab
    prints the formula.
  * All Dialpad duration columns are MINUTES; export timestamps are naive
    local time in the row's ``timezone`` column.
  * Shifts (owner decision 2026-09-02): Morning 06:00–16:00, Afternoon
    12:30–22:00, Night 22:00–06:00 (+1 day; labelled by the date it
    STARTED). Morning/Afternoon overlap by design — never sum shift rows.
  * agents_handled = distinct agent emails on agent legs started in the
    window; agents_on_duty = distinct emails with an available / occupied /
    wrap-up interval (from the ``onduty`` records export) overlapping it.

Usage (from qa-automation/AI-Scoring, .env loaded):

    .venv/bin/python scripts/ms_eod_report.py                       # yesterday
    .venv/bin/python scripts/ms_eod_report.py --start 2026-09-01 --end 2026-09-02
    .venv/bin/python scripts/ms_eod_report.py --start 2026-09-01 --end 2026-09-02 --dry-run
"""

from __future__ import annotations

import argparse
import math
import asyncio
import csv
import io
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import httpx  # noqa: E402

BASE_URL = "https://dialpad.com/api/v2"
MS_CALL_CENTER_ID = 5699048497577984
TZ = "America/Mexico_City"
DEFAULT_SHEET_ID = "1IF2vpb7oo3gybCkX82Wmk1YtYuwwszFqwIDHxy_032Y"

SUMMARY_TAB = "Summary"
CALLS_TAB = "Calls"
AGENTS_TAB = "Agents_Daily"

DEFAULT_SL_SECONDS = 30          # fallback when GET /callcenters/{id} fails
DEFAULT_SL_TARGET_PCT = 80
SHORT_ABANDON_SECONDS = 6.0
SL_DENOMINATOR_EXCLUDES = ("short_abandoned", "missed")   # confirmed vs Dialpad UI 09-01 + 09-02

POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 600


def _r1(x: float) -> float:
    """Round half UP to one decimal — matches the TypeScript port
    (Math.round(x*10)/10); Python's round() is half-even on binary floats
    and produced 0.1 s parity diffs."""
    return math.floor(x * 10 + 0.5) / 10


ON_DUTY_STATES = {"available", "occupied", "wrapup", "busy"}
USER_SUM_COLS = ("all_calls", "inbound_calls", "outbound_calls", "answered", "missed",
                 "abandoned", "ring_no_answer", "talk_duration", "hold_duration", "wrapup_duration")

# (key, label, start "HH:MM", end "HH:MM"); end <= start ⇒ crosses midnight.
SHIFTS = (
    ("morning", "Morning", "06:00", "16:00"),
    ("afternoon", "Afternoon", "12:30", "22:00"),
    ("night", "Night", "22:00", "06:00"),
)

SUMMARY_HEADER = [
    "date", "window", "window_local", "source",
    "inbound", "outbound", "answered", "abandoned", "abandon_pct",
    "short_abandoned", "missed", "cancelled", "spam", "voicemail",
    "sl_count", "sl_pct", "sl_target", "sl_formula",
    "asa_s", "avg_wait_abandoned_s", "longest_wait_abandoned_s",
    "agents_handled", "agents_on_duty", "reconciliation", "generated_at",
]
CALLS_HEADER = [
    "date", "started_local", "call_id", "direction", "outcome",
    "caller_number", "line_number", "agent_name", "agent_email",
    "wait_s", "talk_min", "duration_s", "transferred", "voicemail",
    "callback_type", "short_abandoned", "within_sl", "shifts",
    "dialpad_link", "legs",
]
AGENTS_HEADER = [
    "date", "agent_name", "agent_email", "handled_calls", "on_duty",
    "all_calls", "inbound", "outbound", "answered", "missed", "abandoned",
    "ring_no_answer", "talk_min", "hold_min", "wrapup_min",
    "on_duty_min", "first_on_duty", "last_off_duty", "shifts_on_duty",
]


# ---------------------------------------------------------------------------
# Pure helpers — no network, covered by tests/test_ms_eod_report.py
# ---------------------------------------------------------------------------


def parse_ts(raw: Optional[str]) -> Optional[datetime]:
    """'2026-09-01 00:05:19.528115' (naive, export tz) → datetime; None on blank/junk."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def minutes(raw: Optional[str]) -> Optional[float]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def shift_window(day: date, key: str) -> tuple[datetime, datetime]:
    """[start, end) in naive local time for the shift that STARTED on `day`."""
    for k, _label, start_s, end_s in SHIFTS:
        if k != key:
            continue
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
        start = datetime.combine(day, datetime.min.time()).replace(hour=sh, minute=sm)
        end = datetime.combine(day, datetime.min.time()).replace(hour=eh, minute=em)
        if end <= start:
            end += timedelta(days=1)
        return start, end
    raise KeyError(key)


def day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time())
    return start, start + timedelta(days=1)


def window_label(key: str) -> str:
    for k, _label, start_s, end_s in SHIFTS:
        if k == key:
            return f"{start_s}–{end_s}" + (" (+1)" if end_s <= start_s else "")
    return "00:00–24:00"


def in_window(ts: Optional[datetime], start: datetime, end: datetime) -> bool:
    return ts is not None and start <= ts < end


def shifts_containing(ts: datetime) -> list[str]:
    """Shift keys (labelled with the date the shift started) that contain `ts`.
    A 02:00 call belongs to the PREVIOUS date's night shift."""
    out = []
    for day in (ts.date() - timedelta(days=1), ts.date()):
        for key, _l, _s, _e in SHIFTS:
            start, end = shift_window(day, key)
            if in_window(ts, start, end):
                out.append(f"{key}({day.isoformat()})" if day != ts.date() else key)
    return out


@dataclass
class Call:
    """One entry-point (CallCenter) row, agent legs joined."""
    call_id: str
    started: datetime
    direction: str
    category: str
    categories: set[str]
    external_number: str
    internal_number: str
    queued: Optional[datetime] = None
    first_rang: Optional[datetime] = None
    connected: Optional[datetime] = None
    ended: Optional[datetime] = None
    time_to_answer_min: Optional[float] = None
    talk_min: Optional[float] = None
    voicemail: bool = False
    callback_type: str = ""
    agent_name: str = ""
    agent_email: str = ""
    legs: int = 0

    # --- derived -----------------------------------------------------------
    @property
    def inbound(self) -> bool:
        return self.direction == "inbound"

    @property
    def answered(self) -> bool:
        return self.category == "incoming"

    @property
    def abandoned(self) -> bool:
        return self.category == "abandoned"

    @property
    def spam(self) -> bool:
        return "spam" in self.categories

    @property
    def transferred(self) -> bool:
        return "transferred_to" in self.categories or "transferred_out" in self.categories

    @property
    def lifetime_s(self) -> Optional[float]:
        if self.ended is None:
            return None
        return (self.ended - self.started).total_seconds()

    @property
    def short_abandoned(self) -> bool:
        life = self.lifetime_s
        return self.abandoned and life is not None and life < SHORT_ABANDON_SECONDS

    @property
    def wait_s(self) -> Optional[float]:
        """Caller wait: answered → time_to_answer; otherwise ended − first of
        queued / first_rang (falls back to started)."""
        if self.answered:
            return None if self.time_to_answer_min is None else _r1(self.time_to_answer_min * 60)
        if self.ended is None:
            return None
        anchors = [t for t in (self.queued, self.first_rang) if t]
        anchor = min(anchors) if anchors else self.started
        return _r1((self.ended - anchor).total_seconds())

    @property
    def duration_s(self) -> Optional[float]:
        if self.connected is None or self.ended is None:
            return None
        return _r1((self.ended - self.connected).total_seconds())

    def within_sl(self, sl_seconds: float) -> Optional[bool]:
        if not (self.inbound and self.answered) or self.time_to_answer_min is None:
            return None
        return self.time_to_answer_min * 60 <= sl_seconds


@dataclass
class AgentLeg:
    call_id: str
    entry_point_call_id: str
    started: datetime
    connected: Optional[datetime]
    name: str
    email: str
    categories: set[str]


@dataclass
class DutyRow:
    ts: datetime
    email: str
    name: str
    status: str


def _split_cats(raw: str) -> set[str]:
    return {t.strip() for t in (raw or "").split(",") if t.strip()}


def build_calls(records: Iterable[dict]) -> tuple[list[Call], list[AgentLeg]]:
    """Records export rows → entry-point Calls (agent joined) + agent legs."""
    calls: dict[str, Call] = {}
    legs: list[AgentLeg] = []
    for r in records:
        started = parse_ts(r.get("date_started"))
        if started is None or not (r.get("call_id") or "").strip():
            continue
        kind = (r.get("target_kind") or "").strip()
        if kind == "CallCenter":
            cid = r["call_id"].strip()
            if cid in calls:            # dedupe across merged exports
                continue
            calls[cid] = Call(
                call_id=cid,
                started=started,
                direction=(r.get("direction") or "").strip(),
                category=(r.get("category") or "").strip(),
                categories=_split_cats(r.get("categories") or ""),
                external_number=(r.get("external_number") or "").strip(),
                internal_number=(r.get("internal_number") or "").strip(),
                queued=parse_ts(r.get("date_queued")),
                first_rang=parse_ts(r.get("date_first_rang")),
                connected=parse_ts(r.get("date_connected")),
                ended=parse_ts(r.get("date_ended")),
                time_to_answer_min=minutes(r.get("time_to_answer")),
                talk_min=minutes(r.get("talk_duration")),
                voicemail=(r.get("voicemail") or "").strip().lower() == "true",
                callback_type=(r.get("callback_type") or "").strip(),
            )
        elif kind == "UserProfile" and (r.get("email") or "").strip():
            legs.append(AgentLeg(
                call_id=r["call_id"].strip(),
                entry_point_call_id=(r.get("entry_point_call_id") or "").strip(),
                started=started,
                connected=parse_ts(r.get("date_connected")),
                name=(r.get("name") or "").strip(),
                email=(r.get("email") or "").strip().lower(),
                categories=_split_cats(r.get("categories") or ""),
            ))

    # Join: the agent who ANSWERED (earliest connected leg), else the first leg.
    by_entry: dict[str, list[AgentLeg]] = defaultdict(list)
    seen_leg: set[str] = set()
    deduped_legs: list[AgentLeg] = []
    for leg in legs:
        if leg.call_id in seen_leg:
            continue
        seen_leg.add(leg.call_id)
        deduped_legs.append(leg)
        if leg.entry_point_call_id:
            by_entry[leg.entry_point_call_id].append(leg)
    for cid, call in calls.items():
        group = by_entry.get(cid, [])
        call.legs = len(group)
        if not group:
            continue
        answered = sorted((l for l in group if l.connected), key=lambda l: l.connected)
        pick = answered[0] if answered else sorted(group, key=lambda l: l.started)[0]
        call.agent_name, call.agent_email = pick.name, pick.email
    return list(calls.values()), deduped_legs


def build_duty(rows: Iterable[dict]) -> list[DutyRow]:
    out, seen = [], set()
    for r in rows:
        rid = (r.get("record_id") or "").strip()
        ts = parse_ts(r.get("date"))
        email = (r.get("email") or "").strip().lower()
        if ts is None or not email or (rid and rid in seen):
            continue
        seen.add(rid)
        out.append(DutyRow(ts=ts, email=email, name=(r.get("name") or "").strip(),
                           status=(r.get("on_duty_status") or "").strip().lower()))
    return out


def duty_intervals(rows: list[DutyRow], horizon: datetime) -> dict[str, list[tuple[datetime, datetime]]]:
    """Per agent: [start, end) stretches in an on-duty state (available /
    occupied / wrap-up / busy). A trailing on-state closes at `horizon`."""
    by_email: dict[str, list[DutyRow]] = defaultdict(list)
    for r in rows:
        by_email[r.email].append(r)
    out: dict[str, list[tuple[datetime, datetime]]] = {}
    for email, seq in by_email.items():
        seq.sort(key=lambda r: r.ts)
        intervals, open_at = [], None
        for r in seq:
            on = r.status in ON_DUTY_STATES
            if on and open_at is None:
                open_at = r.ts
            elif not on and open_at is not None:
                if r.ts > open_at:
                    intervals.append((open_at, r.ts))
                open_at = None
        if open_at is not None and horizon > open_at:
            intervals.append((open_at, horizon))
        out[email] = intervals
    return out


def overlap_minutes(intervals: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> float:
    total = 0.0
    for a, b in intervals:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            total += (hi - lo).total_seconds() / 60
    return _r1(total)


def sl_pct(sl_count: int, parts: dict) -> Optional[float]:
    den = parts.get("inbound", 0) - sum(parts.get(k, 0) for k in SL_DENOMINATOR_EXCLUDES)
    return _r1(100 * sl_count / den) if den > 0 else None


def sl_formula() -> str:
    return "sl_count / (inbound" + "".join(f" − {k}" for k in SL_DENOMINATOR_EXCLUDES) + ")"


def summarize_window(
    calls: list[Call],
    legs: list[AgentLeg],
    intervals: dict[str, list[tuple[datetime, datetime]]],
    start: datetime,
    end: datetime,
    sl_seconds: float,
) -> dict:
    """Records-derived metrics for [start, end)."""
    win = [c for c in calls if in_window(c.started, start, end)]
    inbound = [c for c in win if c.inbound]
    abandoned = [c for c in inbound if c.abandoned]
    answered = [c for c in inbound if c.answered]
    parts = {
        "inbound": len(inbound),
        "outbound": sum(1 for c in win if c.direction == "outbound"),
        "answered": len(answered),
        "abandoned": len(abandoned),
        "short_abandoned": sum(1 for c in abandoned if c.short_abandoned),
        "missed": sum(1 for c in inbound if c.category == "missed"),
        "cancelled": sum(1 for c in inbound if c.category == "cancelled"),
        "spam": sum(1 for c in inbound if c.spam),
        "voicemail": sum(1 for c in inbound if c.voicemail),
    }
    parts["sl_count"] = sum(1 for c in answered if c.within_sl(sl_seconds))
    parts["sl_pct"] = sl_pct(parts["sl_count"], parts)
    parts["abandon_pct"] = _r1(100 * len(abandoned) / len(inbound)) if inbound else None
    tta = [c.time_to_answer_min * 60 for c in answered if c.time_to_answer_min is not None]
    parts["asa_s"] = _r1(sum(tta) / len(tta)) if tta else None
    waits = [c.wait_s for c in abandoned if c.wait_s is not None]
    parts["avg_wait_abandoned_s"] = _r1(sum(waits) / len(waits)) if waits else None
    parts["longest_wait_abandoned_s"] = max(waits) if waits else None
    parts["agents_handled"] = len({l.email for l in legs if in_window(l.started, start, end)})
    parts["agents_on_duty"] = sum(
        1 for ivs in intervals.values() if any(b > start and a < end for a, b in ivs)
    )
    return parts


def official_day(stats_row: dict) -> dict:
    """Dialpad's daily stats export row → the same keys as summarize_window."""
    g = lambda k: int(float(stats_row.get(k) or 0))  # noqa: E731
    parts = {
        "inbound": g("inbound_calls"), "outbound": g("outbound_calls"),
        "answered": g("answered"), "abandoned": g("abandoned"),
        "short_abandoned": g("short_abandoned"), "missed": g("missed"),
        "cancelled": g("cancelled"), "spam": g("spam"), "voicemail": g("voicemails"),
        "sl_count": g("service_level"),
    }
    parts["sl_pct"] = sl_pct(parts["sl_count"], parts)
    parts["abandon_pct"] = _r1(100 * parts["abandoned"] / parts["inbound"]) if parts["inbound"] else None
    asa = minutes(stats_row.get("asa"))
    parts["asa_s"] = _r1(asa * 60) if asa is not None else None
    return parts


def reconcile(official: dict, derived: dict) -> str:
    keys = ("inbound", "abandoned", "short_abandoned", "sl_count")
    diffs = [f"{k} records={derived[k]} vs dialpad={official[k]}"
             for k in keys if derived.get(k) != official.get(k)]
    return "OK" if not diffs else "CHECK: " + "; ".join(diffs)


# ---------------------------------------------------------------------------
# Dialpad Stats API
# ---------------------------------------------------------------------------


def _headers() -> dict:
    key = os.environ.get("DIALPAD_API_KEY", "")
    if not key:
        raise RuntimeError("DIALPAD_API_KEY not set")
    return {"Authorization": f"Bearer {key}"}


async def fetch_export(client: httpx.AsyncClient, export_type: str, stat_type: str, *,
                       days_ago: Optional[tuple[int, int]] = None, is_today: bool = False,
                       group_by: Optional[str] = None) -> list[dict]:
    payload: dict = {
        "export_type": export_type, "stat_type": stat_type, "timezone": TZ,
        "target_id": MS_CALL_CENTER_ID, "target_type": "callcenter",
    }
    if is_today:
        payload["is_today"] = True
    else:
        payload["days_ago_start"], payload["days_ago_end"] = days_ago
    if group_by:
        payload["group_by"] = group_by
    resp = await client.post(f"{BASE_URL}/stats", json=payload)
    resp.raise_for_status()
    request_id = resp.json()["request_id"]
    tag = f"{export_type}/{stat_type}{'/' + group_by if group_by else ''} {'today' if is_today else days_ago}"
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT_S
    while True:
        body = (await client.get(f"{BASE_URL}/stats/{request_id}")).json()
        if body.get("status") == "complete":
            dl = await client.get(body["download_url"], follow_redirects=True)
            dl.raise_for_status()
            rows = list(csv.DictReader(io.StringIO(dl.text)))
            print(f"  ✓ {tag}: {len(rows)} rows (request {request_id})")
            return rows
        if body.get("status") == "failed":
            raise RuntimeError(f"stats export {tag} failed: {body}")
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"stats export {tag} timed out (request {request_id})")
        await asyncio.sleep(POLL_INTERVAL_S)


async def fetch_for_dates(client: httpx.AsyncClient, export_type: str, stat_type: str,
                          dates: list[date], today: date, group_by: Optional[str] = None) -> list[dict]:
    """One days_ago-range export for past dates + one is_today export when
    `dates` includes today. Future dates are skipped."""
    past = [(today - d).days for d in dates if (today - d).days >= 1]
    jobs = []
    if past:
        jobs.append(fetch_export(client, export_type, stat_type,
                                 days_ago=(min(past), max(past)), group_by=group_by))
    if today in dates:
        jobs.append(fetch_export(client, export_type, stat_type, is_today=True, group_by=group_by))
    rows: list[dict] = []
    for chunk in await asyncio.gather(*jobs):
        rows.extend(chunk)
    return rows


async def fetch_call_center(client: httpx.AsyncClient) -> dict:
    try:
        resp = await client.get(f"{BASE_URL}/callcenters/{MS_CALL_CENTER_ID}")
        resp.raise_for_status()
        alerts = resp.json().get("alerts") or {}
        return {
            "name": resp.json().get("name", "Member Support Line"),
            "sl_seconds": float(alerts.get("cc_service_level_seconds") or DEFAULT_SL_SECONDS),
            "sl_target_pct": float(alerts.get("cc_service_level") or DEFAULT_SL_TARGET_PCT),
        }
    except (httpx.HTTPError, ValueError) as exc:
        print(f"  ! call center settings unavailable ({exc}); using {DEFAULT_SL_SECONDS}s / {DEFAULT_SL_TARGET_PCT}%")
        return {"name": "Member Support Line", "sl_seconds": float(DEFAULT_SL_SECONDS),
                "sl_target_pct": float(DEFAULT_SL_TARGET_PCT)}


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


@dataclass
class Report:
    summary_rows: list[list] = field(default_factory=list)
    call_rows: list[list] = field(default_factory=list)
    agent_rows: list[list] = field(default_factory=list)


def _yn(v: Optional[bool]) -> str:
    return "" if v is None else ("Y" if v else "N")


def _fmt(v):
    return "" if v is None else v


def build_report(dates: list[date], records: list[dict], duty_rows: list[dict],
                 daily_stats: list[dict], user_stats: list[dict], cc: dict) -> Report:
    calls, legs = build_calls(records)
    duty = build_duty(duty_rows)
    horizon = max([r.ts for r in duty] + [c.started for c in calls]) + timedelta(minutes=1) \
        if (duty or calls) else datetime.now()
    intervals = duty_intervals(duty, horizon)
    sl_seconds = cc["sl_seconds"]
    sl_target = f"{cc['sl_target_pct']:g}% ≤ {sl_seconds:g}s"
    generated = datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M %Z")
    stats_by_date = {r.get("date"): r for r in daily_stats}
    report = Report()

    for day in dates:
        d_iso = day.isoformat()
        start, end = day_window(day)
        derived = summarize_window(calls, legs, intervals, start, end, sl_seconds)
        official_row = stats_by_date.get(d_iso)
        if official_row:
            parts = official_day(official_row)
            parts["agents_handled"] = derived["agents_handled"]
            parts["agents_on_duty"] = derived["agents_on_duty"]
            parts["avg_wait_abandoned_s"] = derived["avg_wait_abandoned_s"]
            parts["longest_wait_abandoned_s"] = derived["longest_wait_abandoned_s"]
            source, recon = "dialpad daily stats", reconcile(parts, derived)
        else:
            parts, source, recon = derived, "records (no daily stats row)", "no dialpad daily row"
        report.summary_rows.append(_summary_row(d_iso, "Full day", "00:00–24:00", source,
                                                parts, sl_target, recon, generated))
        for key, label, _s, _e in SHIFTS:
            s, e = shift_window(day, key)
            p = summarize_window(calls, legs, intervals, s, e, sl_seconds)
            report.summary_rows.append(_summary_row(d_iso, label, window_label(key),
                                                    "records", p, sl_target, "", generated))

    date_set = {d.isoformat() for d in dates}
    for c in sorted(calls, key=lambda c: c.started):
        if c.started.date().isoformat() not in date_set:
            continue
        report.call_rows.append([
            c.started.date().isoformat(), c.started.strftime("%H:%M:%S"), c.call_id,
            c.direction, c.category, c.external_number, c.internal_number,
            c.agent_name, c.agent_email, _fmt(c.wait_s), _fmt(c.talk_min), _fmt(c.duration_s),
            _yn(c.transferred), _yn(c.voicemail), c.callback_type,
            _yn(c.short_abandoned) if c.abandoned else "", _yn(c.within_sl(sl_seconds)),
            ",".join(shifts_containing(c.started)),
            f"https://dialpad.com/callhistory/callreview/{c.call_id}", c.legs,
        ])

    names = {l.email: l.name for l in legs} | {r.email: r.name for r in duty}
    handled_by_day: dict[tuple[str, str], int] = defaultdict(int)
    for l in legs:
        handled_by_day[(l.started.date().isoformat(), l.email)] += 1
    # One row per (date, user) is the contract; finer rows (the hourly quirk)
    # are SUMMED so an agent gets a daily figure, never a random slice.
    users_by_day: dict[tuple[str, str], dict] = {}
    for r in user_stats:
        if (r.get("type") or "user") != "user" or not r.get("email"):
            continue
        k = (r.get("date"), r["email"].lower())
        prev = users_by_day.get(k)
        if prev is None:
            users_by_day[k] = dict(r)
            continue
        for c in USER_SUM_COLS:
            prev[c] = str(round((minutes(prev.get(c)) or 0) + (minutes(r.get(c)) or 0), 2))
    for day in dates:
        d_iso = day.isoformat()
        start, end = day_window(day)
        emails = {e for (d, e) in handled_by_day if d == d_iso}
        emails |= {e for (d, e) in users_by_day if d == d_iso}
        emails |= {e for e, ivs in intervals.items() if any(b > start and a < end for a, b in ivs)}
        for email in sorted(emails):
            u = users_by_day.get((d_iso, email), {})
            ivs = intervals.get(email, [])
            on_min = overlap_minutes(ivs, start, end)
            inside = [(max(a, start), min(b, end)) for a, b in ivs if b > start and a < end]
            first_on = min(a for a, _ in inside).strftime("%H:%M") if inside else ""
            last_off = max(b for _, b in inside)
            last_off_s = "" if not inside or last_off >= end else last_off.strftime("%H:%M")
            shifts_on = [label for key, label, _s, _e in SHIFTS
                         if overlap_minutes(ivs, *shift_window(day, key)) > 0]
            g = lambda k: _fmt(int(float(u[k]))) if u.get(k) not in (None, "") else ""  # noqa: E731
            m = lambda k: _fmt(minutes(u.get(k))) if u.get(k) not in (None, "") else ""  # noqa: E731
            report.agent_rows.append([
                d_iso, u.get("name") or names.get(email, ""), email,
                handled_by_day.get((d_iso, email), 0), "Y" if on_min > 0 else "N",
                g("all_calls"), g("inbound_calls"), g("outbound_calls"), g("answered"),
                g("missed"), g("abandoned"), g("ring_no_answer"),
                m("talk_duration"), m("hold_duration"), m("wrapup_duration"),
                on_min, first_on, last_off_s, ", ".join(shifts_on),
            ])
    return report


def _summary_row(d_iso, window, window_local, source, p, sl_target, recon, generated) -> list:
    return [
        d_iso, window, window_local, source,
        p["inbound"], p["outbound"], p["answered"], p["abandoned"], _fmt(p.get("abandon_pct")),
        p["short_abandoned"], p["missed"], p["cancelled"], p["spam"], p["voicemail"],
        p["sl_count"], _fmt(p.get("sl_pct")), sl_target, sl_formula(),
        _fmt(p.get("asa_s")), _fmt(p.get("avg_wait_abandoned_s")), _fmt(p.get("longest_wait_abandoned_s")),
        p["agents_handled"], p["agents_on_duty"], recon, generated,
    ]


# ---------------------------------------------------------------------------
# Google Sheets — replace rows for the processed dates, keep everything else
# ---------------------------------------------------------------------------


def _open_spreadsheet(sheet_id: str):
    import gspread
    from google.oauth2.service_account import Credentials

    creds_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not creds_env:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = (Credentials.from_service_account_info(json.loads(creds_env), scopes=scopes)
             if creds_env.strip().startswith("{")
             else Credentials.from_service_account_file(creds_env, scopes=scopes))
    client = gspread.authorize(creds)
    client.set_timeout(120)
    return client.open_by_key(sheet_id)


def upsert_tab(spreadsheet, tab_name: str, header: list[str], new_rows: list[list],
               dates: set[str], sort_key) -> int:
    """Drop existing rows whose date (col A) is in `dates`, append `new_rows`,
    rewrite the tab sorted. Returns the row count written."""
    import gspread

    try:
        tab = spreadsheet.worksheet(tab_name)
        # UNFORMATTED: numbers come back as numbers, so kept rows are
        # rewritten with their original types (formatted reads are all text).
        existing = tab.get_all_values(value_render_option="UNFORMATTED_VALUE")
    except gspread.WorksheetNotFound:
        tab = spreadsheet.add_worksheet(tab_name, rows=max(len(new_rows) + 20, 100), cols=len(header))
        existing = []
    if existing and existing[0] != header:
        raise RuntimeError(
            f"tab {tab_name!r} has a different header than this script writes — "
            f"rename/archive the tab (or align the columns) before re-running")
    kept = [r for r in existing[1:] if r and r[0] not in dates]
    rows = sorted(kept + [[_fmt(v) for v in r] for r in new_rows], key=sort_key)
    values = [header] + rows
    tab.clear()
    tab.resize(rows=max(len(values) + 20, 100), cols=len(header))
    tab.update(values=values, range_name="A1", value_input_option="RAW")
    tab.freeze(rows=1)
    return len(rows)


def write_report(sheet_id: str, report: Report, dates: list[date]) -> dict:
    ss = _open_spreadsheet(sheet_id)
    date_set = {d.isoformat() for d in dates}
    window_order = {"Full day": 0, **{label: i + 1 for i, (_k, label, _s, _e) in enumerate(SHIFTS)}}
    counts = {
        SUMMARY_TAB: upsert_tab(ss, SUMMARY_TAB, SUMMARY_HEADER, report.summary_rows, date_set,
                                lambda r: (r[0], window_order.get(r[1], 9))),
        CALLS_TAB: upsert_tab(ss, CALLS_TAB, CALLS_HEADER, report.call_rows, date_set,
                              lambda r: (r[0], r[1])),
        AGENTS_TAB: upsert_tab(ss, AGENTS_TAB, AGENTS_HEADER, report.agent_rows, date_set,
                               lambda r: (r[0], r[1])),
    }
    counts["url"] = ss.url
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(report: Report) -> None:
    cols = ["date", "window", "inbound", "answered", "abandoned", "short_abandoned",
            "sl_count", "sl_pct", "asa_s", "agents_handled", "agents_on_duty", "reconciliation"]
    idx = [SUMMARY_HEADER.index(c) for c in cols]
    widths = [max(len(c), *(len(str(r[i])) for r in report.summary_rows)) for c, i in zip(cols, idx)]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for r in report.summary_rows:
        print("  " + "  ".join(str(r[i]).ljust(w) for i, w in zip(idx, widths)))


async def main() -> int:
    today = datetime.now(ZoneInfo(TZ)).date()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=date.fromisoformat, default=today - timedelta(days=1),
                        help="first local date (default: yesterday)")
    parser.add_argument("--end", type=date.fromisoformat, default=None,
                        help="last local date inclusive (default: --start)")
    parser.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    parser.add_argument("--dry-run", action="store_true", help="compute + print, do not write the sheet")
    args = parser.parse_args()
    end = args.end or args.start
    if end < args.start:
        parser.error("--end is before --start")
    dates = [args.start + timedelta(days=i) for i in range((end - args.start).days + 1)]
    # The night shift of the last date ends at 06:00 the next day.
    record_dates = dates + [end + timedelta(days=1)]
    # Dialpad quirks (probed 2026-09-06): a `stats` export whose range is a
    # SINGLE day comes back per user per HOUR (an `hour` column) instead of
    # per day — always span two days and filter by date. The `onduty` export
    # only carries transitions, so the 00:00 state needs one day of look-back.
    day_before = args.start - timedelta(days=1)
    stats_dates = [day_before] + dates
    duty_dates = [day_before] + record_dates

    print(f"→ Member Support EOD report {dates[0]} … {dates[-1]} ({TZ}); today={today}")
    async with httpx.AsyncClient(headers=_headers(), timeout=60) as client:
        cc = await fetch_call_center(client)
        print(f"  call center: {cc['name']} — service level {cc['sl_target_pct']:g}% ≤ {cc['sl_seconds']:g}s")
        records, duty, daily, users = await asyncio.gather(
            fetch_for_dates(client, "records", "calls", record_dates, today),
            fetch_for_dates(client, "records", "onduty", duty_dates, today),
            fetch_for_dates(client, "stats", "calls", stats_dates, today, group_by="date"),
            fetch_for_dates(client, "stats", "calls", stats_dates, today),
        )

    report = build_report(dates, records, duty, daily, users, cc)
    print(f"→ {len(report.summary_rows)} summary rows, {len(report.call_rows)} calls, "
          f"{len(report.agent_rows)} agent-days")
    _print_summary(report)
    if args.dry_run:
        print("→ dry run — sheet not written")
        return 0
    counts = write_report(args.sheet_id, report, dates)
    print(f"✓ written: {SUMMARY_TAB}={counts[SUMMARY_TAB]} {CALLS_TAB}={counts[CALLS_TAB]} "
          f"{AGENTS_TAB}={counts[AGENTS_TAB]} rows total → {counts['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
