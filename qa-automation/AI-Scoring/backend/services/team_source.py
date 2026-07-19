"""W1 — Postgres row-source for the /team analytics trio (ReadPathFlip §3).

Produces a DataFrame with EXACTLY the schema `team_stats.load_and_clean`
derives from the Analyst_History sheet, so the nine compute_* functions
run unchanged on either source. Parity traps honored (ReadPathFlip §4):

- naive timestamps (UTC wall-clock, tz stripped) — bucket-TZ math relies
  on it;
- eval_id = trailing segment of the dialpad_link text, falling back to
  the D2 superseded link in metadata, then to dialpad_entry_point_call_id;
- departed agents (agent_id NULL) degrade to inactive + blank supervisor
  with the raw name shown;
- excluded_test_agents config filter applied identically;
- yn cells use the Y/N/NA/'' vocabulary `_parse_yn_cell` produces;
  numeric NA (binary_value='NA' on a numeric section) lands NaN, same as
  the sheet's "Not Applicable" failing float-parse.

Section identity: backfilled rows stamp ARCHIVED rubric section ids
(e.g. `caller_identity_validation`) while live engine rows stamp current
short ids (`caller_id`). The pivot resolves every stored section_id to a
history_id via an alias map built from qa.rubric_versions (all archived
rubrics) + the live config — rubric pinning without column loss. Ids
whose history_id has no column in the CURRENT config (e.g. MS v1
`documentation` after the v2 HRR rename, Sales v1's 19 legacy sections)
drop from section analytics by design — the rubric changed; the scores
are not comparable. Overall-score analytics keep the full history.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pandas as pd

from backend.services.data_normalization import strip_accents
from backend.services.eval_store import get_pool

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig


def _eval_id_from_link(link: str) -> str:
    if not link:
        return ""
    clean = link.split("[")[0].strip().split("?")[0].strip()
    return clean.rstrip("/").split("/")[-1]


async def _section_alias_map(conn, config: "TeamConfig") -> dict[str, str]:
    """Every known section_id (current + all archived rubrics for the
    team) → its history_id."""
    alias = {}
    for s in config.sections_by_number:  # current config wins on conflict
        alias[s.history_id] = s.history_id
        alias[s.id] = s.history_id
    # Archived rubrics alias BY POSITION (section_number → the current
    # section occupying that slot): the sheet reads cells positionally,
    # so a renamed slot (MS v1 `documentation` → v2 `human_review_required`,
    # both section 9) keeps its historic scores in the same analytics
    # column. History_ids that still exist match themselves anyway.
    current_by_number = {s.section_number: s.history_id
                         for s in config.sections_by_number}
    rows = await conn.fetch(
        "SELECT rubric_json FROM qa.rubric_versions WHERE team_id = $1",
        config.team_id)
    for r in rows:
        payload = json.loads(r["rubric_json"])
        for s in payload.get("sections", []):
            target = current_by_number.get(s.get("section_number"))
            if target is not None:
                alias.setdefault(s["id"], target)
                alias.setdefault(s.get("history_id", s["id"]), target)
    return alias


def _roster_maps(agent_rows: list) -> tuple[dict, dict, set]:
    """Read-time identity maps from ACTIVE qa.agents rows, mirroring
    load_and_clean's Mails canonical_map exactly (ReadPathFlip §4.4):
    raw-name (verbatim + accent-stripped, lowered) → canonical display;
    supervisor and active membership keyed by canonical-lower. Inactive
    qa.agents rows are excluded — the Mails tab only lists active agents,
    so an inactive match degrades identically to no match."""
    canonical_map: dict[str, str] = {}
    supervisor_map: dict[str, str] = {}
    active_set: set[str] = set()
    for a in agent_rows:
        if not a["active"]:
            continue
        name = (a["name"] or "").strip()
        if not name:
            continue
        canonical = (a["canonical_name"] or "").strip() or name
        canonical_map[name.lower()] = canonical
        canonical_map[strip_accents(name).lower()] = canonical
        canonical_map[canonical.lower()] = canonical
        canonical_map[strip_accents(canonical).lower()] = canonical
        supervisor_map[canonical.lower()] = a["supervisor_email"] or ""
        active_set.add(canonical.lower())
    return canonical_map, supervisor_map, active_set


def frame_from_rows(config: "TeamConfig", eval_rows: list, section_rows: list,
                    alias_map: dict[str, str],
                    agent_rows: list = ()) -> pd.DataFrame:
    """Pure assembly: DB rows → the load_and_clean schema.

    Identity is two-tier: rows carry the finalize-time ``agent_id`` join
    (display/is_active/supervisor pre-resolved in SQL), and rows the stamp
    missed (``agent_id`` NULL — e.g. finalized while the agent's qa.agents
    row was stale; 26 such rows caused the 2026-07-15 roster undercount)
    fall back to READ-time name matching against the CURRENT roster, the
    way the sheet path always resolved against the live Mails tab. Rows
    matching nothing degrade as before: inactive, blank supervisor, raw
    name (true departed agents — §4.3)."""
    numeric_ids = set(config.numeric_history_ids)
    yn_ids = set(config.yn_history_ids)
    excluded = {a.strip().lower() for a in config.excluded_test_agents}
    canonical_map, supervisor_map, active_set = _roster_maps(list(agent_rows))

    # section values per evaluation, keyed by resolved history_id
    by_eval: dict[int, dict[str, object]] = {}
    for s in section_rows:
        hid = alias_map.get(s["section_id"])
        if hid is None or (hid not in numeric_ids and hid not in yn_ids):
            continue
        if hid in numeric_ids:
            val = float(s["numeric_score"]) if s["numeric_score"] is not None else float("nan")
        else:
            val = s["binary_value"] or ""
        by_eval.setdefault(s["evaluation_id"], {})[hid] = val

    records = []
    for ev in eval_rows:
        agent = (ev["agent_display"] or ev["agent_name_raw"] or "").strip()
        is_active = bool(ev["is_active"])
        supervisor = ev["supervisor"] or ""
        if ev["agent_id"] is None and agent:
            # read-time fallback for identity-orphaned rows
            canonical = (canonical_map.get(agent.lower())
                         or canonical_map.get(strip_accents(agent).lower()))
            if canonical is not None:
                agent = canonical
                is_active = canonical.lower() in active_set
                supervisor = supervisor_map.get(canonical.lower(), "")
        if not agent or agent.lower() in excluded:
            continue
        ts = ev["ts"]
        if ts is None or ts.year < 2020:
            continue
        meta = json.loads(ev["dialpad_call_metadata"]) if ev["dialpad_call_metadata"] else {}
        link = (ev["dialpad_link"]
                or (meta.get("backfill") or {}).get("superseded_dialpad_link")
                or "")
        eval_id = _eval_id_from_link(link) or (ev["dialpad_entry_point_call_id"] or "")
        approved = ev["approved_at"]
        rec = {
            "agent": agent,
            "timestamp": pd.Timestamp(ts).tz_localize(None),      # naive UTC
            "eval_approved_at": (pd.Timestamp(approved).tz_localize(None)
                                 if approved is not None else pd.NaT),
            "overall_score": float(ev["overall_score"]),
            "manager_email": (ev["evaluator_email"] or "").lower(),
            "is_active": is_active,
            "supervisor": supervisor,
            "eval_id": eval_id,
        }
        sections = by_eval.get(ev["id"], {})
        for hid in numeric_ids:
            rec[hid] = sections.get(hid, float("nan"))
        for hid in yn_ids:
            rec[hid] = sections.get(hid, "")
        records.append(rec)

    if not records:
        return pd.DataFrame()
    df = pd.DataFrame.from_records(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["eval_approved_at"] = pd.to_datetime(df["eval_approved_at"])
    return df


async def fetch_history_frame(config: "TeamConfig") -> pd.DataFrame:
    """W1 entry point: the load_and_clean-equivalent DataFrame from qa.*."""
    pool = await get_pool()
    if pool is None:
        raise RuntimeError("team_source: no DATABASE_URL configured")
    async with pool.acquire() as conn:
        alias_map = await _section_alias_map(conn, config)
        eval_rows = await conn.fetch(
            "SELECT e.id, e.agent_id, e.agent_name_raw, e.evaluator_email, "
            "  e.overall_score, "
            "  e.dialpad_link, e.dialpad_entry_point_call_id, e.dialpad_call_metadata, "
            "  COALESCE(e.call_connected_at, e.approved_at) AS ts, e.approved_at, "
            "  COALESCE(a.canonical_name, a.name, e.agent_name_raw) AS agent_display, "
            "  COALESCE(a.active, FALSE) AS is_active, "
            "  COALESCE(a.supervisor_email, '') AS supervisor "
            "FROM qa.evaluations e "
            "LEFT JOIN qa.agents a ON a.id = e.agent_id "
            "WHERE e.team_id = $1 AND e.state = 'finalized'",
            config.team_id)
        # W3: a parameterized slice of qa.v_history_long (migration 015) —
        # the view owns the finalized-evals × sections join.
        section_rows = await conn.fetch(
            "SELECT evaluation_id, section_id, numeric_score, binary_value "
            "FROM qa.v_history_long WHERE team_id = $1",
            config.team_id)
        agent_rows = await conn.fetch(
            "SELECT name, canonical_name, active, supervisor_email "
            "FROM qa.agents WHERE team_id = $1",
            config.team_id)
    return frame_from_rows(config, eval_rows, section_rows, alias_map, agent_rows)
