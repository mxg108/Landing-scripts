#!/usr/bin/env python3
"""Golden-fixture capture for the team-stats parity gate (PortManifest §11.3).

Runs the PYTHON oracle (team_source + team_stats, the exact production code)
against Railway Postgres, pinned to --max-eval-id (the D1 snapshot boundary)
and a single captured `now`, and writes the full /team/stats + /team/evals
response payloads per team to a fixture JSON. The node runner
(run_parity.mjs) replays the TypeScript port against D1 with the same pins
and diffs the outputs.

Run from qa-automation/AI-Scoring:
    .venv/bin/python ../../sandy-qa/parity/capture_fixture.py --max-eval-id 2525 \
        --out ../../sandy-qa/parity/fixture.json
"""

import argparse
import asyncio
import json
import math
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import asyncpg  # noqa: E402
import pandas as pd  # noqa: E402

from backend.config.team_config import get_team_config  # noqa: E402
from backend.services import team_stats  # noqa: E402
from backend.services.team_source import (  # noqa: E402
    _section_alias_map,
    frame_from_rows,
)

TEAMS = ("member_support", "sales")
DAYS_VARIANTS = (90, 0)


def env_value(key: str) -> str:
    for line in pathlib.Path(".env").read_text().splitlines():
        if line.strip().startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"{key} not in .env")


def _clean(o):
    """JSON-safe: NaN/NaT → None, datetimes/Timestamps → ISO-Z strings."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float) and math.isnan(o):
        return None
    if isinstance(o, pd.Timestamp):
        if pd.isna(o):
            return None
        return o.to_pydatetime().replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(o, datetime):
        d = o if o.tzinfo else o.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return o


def serialize_frame(df: pd.DataFrame, config) -> list:
    rows = []
    for r in df.to_dict(orient="records"):
        rows.append(_clean({
            "agent": r["agent"],
            "ts": r["timestamp"],
            "eval_approved_at": r["eval_approved_at"],
            "overall_score": r["overall_score"],
            "manager_email": r["manager_email"],
            "is_active": bool(r["is_active"]),
            "supervisor": r["supervisor"],
            "eval_id": r["eval_id"],
            "num": {h: r.get(h) for h in config.numeric_history_ids},
            "yn": {h: r.get(h) for h in config.yn_history_ids},
        }))
    return rows


def stats_payload(df: pd.DataFrame, config, days: int, now_naive_utc: datetime) -> dict:
    """Replicates backend/routes/team.py::team_stats filter order exactly
    (active_only=True, no supervisor, no custom window)."""
    filters = {"days": days, "active_only": True, "supervisor": "",
               "date_from": None, "date_to": None}
    if df.empty:
        raise SystemExit("empty frame — refusing to capture a vacuous fixture")
    df = df[df["is_active"]]
    monthly = team_stats.compute_monthly_summary(df)
    if days > 0:
        df = df[df["timestamp"] >= now_naive_utc - timedelta(days=days)]
    kpis = {
        "total_evals": len(df),
        "avg_score": round(float(df["overall_score"].mean()), 1) if len(df) > 0 else 0,
        "std_score": round(float(df["overall_score"].std(ddof=1)), 1) if len(df) > 1 else 0,
        "analyst_count": df["agent"].nunique(),
    }
    return _clean({
        "team_id": config.team_id,
        "rubric_version": config.rubric_version,
        "coverage_regime": "manager_sample",
        "kpis": kpis,
        "monthly": monthly,
        "roster": team_stats.compute_agent_roster(df, config),
        "outliers": team_stats.compute_outliers(df, config.stats),
        "spc": team_stats.compute_monthly_spc(df, config.stats),
        "section_analysis": team_stats.compute_section_analysis(df, config),
        "binary_stats": team_stats.compute_binary_stats(df, config.yn_section_labels),
        "supervisor_stats": team_stats.compute_supervisor_stats(df),
        "ewma": team_stats.compute_ewma(df, config.stats),
        "distribution": team_stats.compute_distribution(df),
        "filters_applied": filters,
    })


def evals_payload(df: pd.DataFrame, config, year_month: str) -> dict:
    """Replicates backend/routes/team.py::team_month_evals (active_only=True)."""
    df = df[df["is_active"]]
    months = team_stats._months_in_bucket_tz(df["timestamp"])
    df = df[months == year_month].sort_values("timestamp", ascending=False)
    rows = [_clean({
        "agent": str(r["agent"]),
        "timestamp": r["timestamp"],
        "eval_approved_at": r.get("eval_approved_at"),
        "overall_score": float(r["overall_score"]),
        "dialpad_link": (f"https://dialpad.com/callhistory/callreview/{r['eval_id']}"
                         if r.get("eval_id") else ""),
        "eval_id": str(r.get("eval_id", "")),
        "supervisor": str(r.get("supervisor", "")) or None,
        "evaluator_email": str(r.get("manager_email", "")) or None,
    }) for r in df.to_dict(orient="records")]
    return {"team_id": config.team_id, "year_month": year_month, "rows": rows,
            "filters_applied": {"active_only": True, "supervisor": ""}}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-eval-id", type=int, required=True)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()

    now_utc = datetime.now(timezone.utc)
    now_naive = now_utc.replace(tzinfo=None)
    # Pin the monthly current/last labels to the captured instant.
    team_stats._now_in_bucket_tz = lambda: now_utc.astimezone(team_stats._BUCKET_TZ)

    conn = await asyncpg.connect(env_value("DATABASE_URL"), timeout=30)
    fixture = {
        "captured_at": now_utc.isoformat().replace("+00:00", "Z"),
        "max_eval_id": args.max_eval_id,
        "bucket_tz": team_stats.BUCKET_TZ_NAME,
        "teams": {},
    }
    try:
        for team_id in TEAMS:
            config = get_team_config(team_id)
            alias_map = await _section_alias_map(conn, config)
            eval_rows = await conn.fetch(
                "SELECT e.id, e.agent_id, e.agent_name_raw, e.evaluator_email, "
                "  e.overall_score, e.dialpad_link, e.dialpad_entry_point_call_id, "
                "  e.dialpad_call_metadata, "
                "  COALESCE(e.call_connected_at, e.approved_at) AS ts, e.approved_at, "
                "  COALESCE(a.canonical_name, a.name, e.agent_name_raw) AS agent_display, "
                "  COALESCE(a.active, FALSE) AS is_active, "
                "  COALESCE(a.supervisor_email, '') AS supervisor "
                "FROM qa.evaluations e LEFT JOIN qa.agents a ON a.id = e.agent_id "
                "WHERE e.team_id = $1 AND e.state = 'finalized' AND e.id <= $2 "
                "ORDER BY e.id",
                team_id, args.max_eval_id)
            # Deterministic collision convention: when an archived rubric
            # position-aliases an old id into a column that a direct
            # current-id row also writes (sales_v1 value_uplift@6 vs
            # landing_guarantee), the DIRECT row must win. Production Python
            # relies on unordered heap row order here (nondeterministic —
            # 2 of 41 Sales collision rows resolve the other way after
            # section edits reordered their rows); the port pins the
            # convention explicitly, so the fixture does too: direct rows
            # ordered last → last-write-wins = direct-wins.
            current_ids = [s.id for s in config.sections_by_number]
            section_rows = await conn.fetch(
                "SELECT evaluation_id, section_id, numeric_score, binary_value "
                "FROM qa.v_history_long WHERE team_id = $1 AND evaluation_id <= $2 "
                "ORDER BY evaluation_id, (section_id = ANY($3::text[]))::int",
                team_id, args.max_eval_id, current_ids)
            agent_rows = await conn.fetch(
                "SELECT name, canonical_name, active, supervisor_email "
                "FROM qa.agents WHERE team_id = $1", team_id)
            df = frame_from_rows(config, eval_rows, section_rows, alias_map, agent_rows)
            current_ym = str(pd.Period(now_utc.astimezone(team_stats._BUCKET_TZ), freq="M"))
            fixture["teams"][team_id] = {
                "rubric_version": config.rubric_version,
                "frame_rows": len(df),
                "frame": serialize_frame(df, config),
                "stats": {str(d): stats_payload(df.copy(), config, d, now_naive)
                          for d in DAYS_VARIANTS},
                "evals_current_month": evals_payload(df.copy(), config, current_ym),
            }
            print(f"{team_id}: frame={len(df)} rows captured")
    finally:
        await conn.close()

    args.out.write_text(json.dumps(fixture, indent=1))
    print(f"fixture written: {args.out} ({args.out.stat().st_size} bytes), "
          f"now={fixture['captured_at']}, pin={args.max_eval_id}")


asyncio.run(main())
