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


def frame_from_rows(config: "TeamConfig", eval_rows: list, section_rows: list,
                    alias_map: dict[str, str]) -> pd.DataFrame:
    """Pure assembly: DB rows → the load_and_clean schema."""
    numeric_ids = set(config.numeric_history_ids)
    yn_ids = set(config.yn_history_ids)
    excluded = {a.strip().lower() for a in config.excluded_test_agents}

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
            "is_active": bool(ev["is_active"]),
            "supervisor": ev["supervisor"] or "",
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
            "SELECT e.id, e.agent_name_raw, e.evaluator_email, e.overall_score, "
            "  e.dialpad_link, e.dialpad_entry_point_call_id, e.dialpad_call_metadata, "
            "  COALESCE(e.call_connected_at, e.approved_at) AS ts, e.approved_at, "
            "  COALESCE(a.canonical_name, a.name, e.agent_name_raw) AS agent_display, "
            "  COALESCE(a.active, FALSE) AS is_active, "
            "  COALESCE(a.supervisor_email, '') AS supervisor "
            "FROM qa.evaluations e "
            "LEFT JOIN qa.agents a ON a.id = e.agent_id "
            "WHERE e.team_id = $1 AND e.state = 'finalized'",
            config.team_id)
        section_rows = await conn.fetch(
            "SELECT es.evaluation_id, es.section_id, es.numeric_score, es.binary_value "
            "FROM qa.evaluation_sections es "
            "JOIN qa.evaluations e ON e.id = es.evaluation_id "
            "WHERE e.team_id = $1 AND e.state = 'finalized'",
            config.team_id)
    return frame_from_rows(config, eval_rows, section_rows, alias_map)
