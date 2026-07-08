"""HR bonus sheet month payload — references/HRBonusSheet.md §4–§5.

Builds the JSON the GAS renderer consumes 1:1. Every number is computed
here (aggregation, rounding, display strings, ordering) so the GAS layer
stays math-free. Population and semantics mirror the qa.* read path:
``state='finalized'`` rows, call time = ``COALESCE(call_connected_at,
created_at)``, month bucketing in the project bucket TZ.

`build_payload_from_rows` is pure (rows in, payload out) so the
aggregation math — including the mockup golden-parity test — runs
without a database; `fetch_month_payload` is the thin asyncpg wrapper.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend.config.team_config import TeamConfig
from backend.services.eval_store import get_pool
from backend.services.team_stats import BUCKET_TZ_NAME

_BUCKET_TZ = ZoneInfo(BUCKET_TZ_NAME)

# HR workbook display for binary section values (mockup parity — note the
# HR sheet shows "N/A", not the Analyst_History projection's "Not Applicable").
_BINARY_DISPLAY = {"Y": "Yes", "N": "No", "NA": "N/A"}
_BINARY_AS_FRACTION = {"Y": 1.0, "N": 0.0}

_NUMERIC_TYPES = ("numeric", "manual")
_BINARY_TYPES = ("yn", "manual_yn")


def month_window_utc(month: str) -> tuple[datetime, datetime]:
    """[start, end) of a ``YYYY-MM`` month in the project bucket TZ, as
    UTC instants. A call at 03:33 UTC on June 1 sits before June's start
    (America/Los_Angeles) and belongs to May — same bucketing seam as
    team_stats._months_in_bucket_tz."""
    year, mon = int(month[:4]), int(month[5:7])
    start = datetime(year, mon, 1, tzinfo=_BUCKET_TZ)
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=_BUCKET_TZ)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=_BUCKET_TZ)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _detail_cell(section_row) -> str:
    """One detail-tab section cell: digits for numeric, Yes/No/N/A for
    binary, blank when the evaluation has no row for the section."""
    if section_row is None:
        return ""
    if section_row["numeric_score"] is not None:
        return str(section_row["numeric_score"])
    if section_row["binary_value"] is not None:
        return _BINARY_DISPLAY.get(section_row["binary_value"], "N/A")
    return ""


def _summary_cell(score_type: str, section_rows: list) -> str:
    """One summary-tab section cell: mean to 2 decimals for numeric,
    integer %Yes for binary (N/A excluded from the denominator), blank
    when the agent has no data for the section."""
    if score_type in _NUMERIC_TYPES:
        values = [r["numeric_score"] for r in section_rows
                  if r is not None and r["numeric_score"] is not None]
        return f"{sum(values) / len(values):.2f}" if values else ""
    values = [_BINARY_AS_FRACTION[r["binary_value"]] for r in section_rows
              if r is not None and r["binary_value"] in _BINARY_AS_FRACTION]
    return f"{sum(values) / len(values) * 100:.0f}%" if values else ""


def build_payload_from_rows(
    config: TeamConfig,
    month: str,
    eval_rows: list,
    sections_by_eval: dict[int, dict[str, dict]],
) -> dict:
    """Assemble the §5 response payload from already-fetched rows.

    ``eval_rows`` must be newest-first; ``sections_by_eval`` maps
    evaluation id -> {section_id -> section row}. Rows outside the target
    month are dropped here even though the SQL window already excludes
    them — the no-prior-month-leakage rule is load-bearing (owner-
    confirmed 2026-07-07), so it holds regardless of the caller.
    """
    hr = config.hr_export
    if hr is None:
        raise ValueError(f"team '{config.team_id}' has no hr_export block")

    start_utc, end_utc = month_window_utc(month)
    excluded = {name.lower() for name in hr.excluded_agents}
    section_defs = [(entry.id, config.scoring_id_to_section[entry.id].score_type)
                    for entry in hr.sections]

    by_agent: dict[str, list] = {}
    for ev in eval_rows:
        ts = ev["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if not start_utc <= ts < end_utc:
            continue
        name = ev["agent_name_raw"].strip()
        if not name or name.lower() in excluded:
            continue
        by_agent.setdefault(name, []).append({**dict(ev), "ts": ts})

    agents = []
    for name in sorted(by_agent, key=str.lower):
        evals = sorted(by_agent[name], key=lambda e: e["ts"], reverse=True)
        email = next(
            (e["agent_email"].strip() for e in evals if (e["agent_email"] or "").strip()),
            "",
        )
        overall = [float(e["overall_score"]) for e in evals
                   if e["overall_score"] is not None]
        rows_per_section = {
            sec_id: [sections_by_eval.get(e["id"], {}).get(sec_id) for e in evals]
            for sec_id, _ in section_defs
        }
        agents.append({
            "name": name,
            "email": email,
            "monthly_avg": round(sum(overall) / len(overall), 1) if overall else "",
            "section_summaries": [
                _summary_cell(score_type, rows_per_section[sec_id])
                for sec_id, score_type in section_defs
            ],
            "evaluations": [
                {
                    "period": e["ts"].astimezone(_BUCKET_TZ).strftime("%Y-%m"),
                    "date": e["ts"].astimezone(_BUCKET_TZ).strftime("%m/%d/%Y %H:%M"),
                    "overall_score": float(e["overall_score"])
                    if e["overall_score"] is not None else "",
                    "sections": [
                        _detail_cell(sections_by_eval.get(e["id"], {}).get(sec_id))
                        for sec_id, _ in section_defs
                    ],
                    "evaluator": e["evaluator_email"] or "",
                    "dialpad_link": e["dialpad_link"] or "",
                }
                for e in evals
            ],
        })

    return {
        "team_id": config.team_id,
        "month": month,
        "month_label": datetime(int(month[:4]), int(month[5:7]), 1).strftime("%B %Y"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "section_labels": [entry.hr_label for entry in hr.sections],
        "agents": agents,
    }


async def fetch_month_payload(config: TeamConfig, month: str) -> dict | None:
    """Query Postgres and build the month payload. Returns None when no
    DATABASE_URL is configured (route maps this to 503)."""
    pool = await get_pool()
    if pool is None:
        return None
    start_utc, end_utc = month_window_utc(month)
    hr_section_ids = [entry.id for entry in config.hr_export.sections]

    async with pool.acquire() as conn:
        eval_rows = await conn.fetch(
            "SELECT id, agent_name_raw, agent_email, evaluator_email, "
            "COALESCE(call_connected_at, created_at) AS ts, "
            "overall_score, dialpad_link "
            "FROM qa.evaluations "
            "WHERE team_id = $1 AND state = 'finalized' "
            "AND COALESCE(call_connected_at, created_at) >= $2 "
            "AND COALESCE(call_connected_at, created_at) < $3 "
            "ORDER BY COALESCE(call_connected_at, created_at) DESC",
            config.team_id, start_utc, end_utc,
        )
        sections_by_eval: dict[int, dict[str, dict]] = {}
        if eval_rows:
            section_rows = await conn.fetch(
                "SELECT evaluation_id, section_id, numeric_score, binary_value "
                "FROM qa.evaluation_sections "
                "WHERE evaluation_id = ANY($1::bigint[]) AND section_id = ANY($2::text[])",
                [e["id"] for e in eval_rows], hr_section_ids,
            )
            for row in section_rows:
                sections_by_eval.setdefault(row["evaluation_id"], {})[row["section_id"]] = row

    return build_payload_from_rows(config, month, eval_rows, sections_by_eval)
