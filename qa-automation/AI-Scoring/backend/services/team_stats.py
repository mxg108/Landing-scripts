"""Team-level statistical computations for the TeamStatsBoard.

All functions are pure computation — they take raw data (lists or DataFrames)
plus a TeamConfig / StatsConfig and return plain dicts/lists. No I/O, no
Sheets API calls.

Schema v2.0 (Phase 2): Analyst_History column positions are derived from
``team_config.history_layout`` (no per-team col_* fields). Priority
thresholds in ``compute_section_analysis`` scale with each section's
score_range so 1-10 teams aren't over-flagged by 1-5-calibrated cutoffs.

DataFrame schema (wide form, one row per evaluation):
    agent              str             canonicalized agent name
    timestamp          datetime
    overall_score      float           0-100
    <numeric_history_id>  float, …    one column per numeric section
    <yn_history_id>       str, …      one column per Y/N section ('Y'|'N'|'NA'|'')
    manager_email      str
    is_active          bool            agent appears in active Mails roster
    supervisor         str             from Mails sheet
    eval_id            str             call_id parsed from Dialpad link

For long form, see compute_long_form(): one row per (agent, eval_id, section).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from backend.config import history_layout
from backend.services.data_normalization import (
    is_excluded_test_row,
    parse_timestamp,
    strip_accents,
)

if TYPE_CHECKING:
    from backend.config.team_config import StatsConfig, TeamConfig


# ---------------------------------------------------------------------------
# Priority threshold helpers — scale with each section's score_range
# ---------------------------------------------------------------------------

# Priority bands as a fraction of each section's score range:
#   gap > _GAP_LOW_FRAC * range → at least "low" (registers as a training opp)
#   gap >= _GAP_MED_FRAC * range → "medium"
#   gap >= _GAP_HIGH_FRAC * range → "high"
# Calibrated to match the legacy 1-5 cutoffs (0.5 / 1.0 / 1.5).
_GAP_LOW_FRAC = 0.125
_GAP_MED_FRAC = 0.25
_GAP_HIGH_FRAC = 0.375


def _section_range_size(team_config: TeamConfig, history_id: str, default: int = 4) -> int:
    """Return (max - min) of a section's score range, or *default*."""
    sec = team_config.history_id_to_section.get(history_id)
    if sec and sec.score_range:
        return sec.score_range[1] - sec.score_range[0]
    return default


# ---------------------------------------------------------------------------
# load_and_clean
# ---------------------------------------------------------------------------

def load_and_clean(
    history_rows: list[list[str]],
    mails_rows: list[list[str]],
    team_config: TeamConfig,
) -> pd.DataFrame:
    """Convert raw sheet rows into a cleaned wide-form DataFrame.

    Args:
        history_rows: Raw rows from Analyst_History (including header).
        mails_rows: Raw rows from Mails sheet (including header).
            Col A = Agent Name, B = Email, C = Supervisor, D = Canonical Name.
        team_config: Active team config — drives column layout via
            ``history_layout``, the test-agent exclusion list, and Y/N
            section ids.

    Returns:
        DataFrame with the schema documented at the top of this module.
        Empty DataFrame if no valid rows.
    """
    L = team_config.history_layout
    yn_section_ids = team_config.yn_history_ids
    excluded_agents = team_config.excluded_test_agents

    # Pre-compute (history_id -> col_index) mappings from the derived layout.
    section_col_by_id = {
        s.history_id: L.col_score(i)
        for i, s in enumerate(team_config.sections_by_number)
    }
    numeric_section_cols = {
        s.history_id: section_col_by_id[s.history_id]
        for s in team_config.numeric_sections
    }
    yn_section_cols = {
        s.history_id: section_col_by_id[s.history_id]
        for s in team_config.yn_sections
    }

    # Build maps from Mails sheet
    canonical_map: dict[str, str] = {}    # raw_name_lower -> canonical
    supervisor_map: dict[str, str] = {}   # canonical_lower -> supervisor
    active_set: set[str] = set()          # canonical_lower names

    for row in mails_rows[1:]:  # skip header
        if len(row) < 2 or not row[0].strip():
            continue
        raw_name = row[0].strip()
        supervisor = row[2].strip() if len(row) > 2 else ""
        canonical = row[3].strip() if len(row) > 3 and row[3].strip() else raw_name

        canonical_map[raw_name.lower()] = canonical
        canonical_map[strip_accents(raw_name).lower()] = canonical
        supervisor_map[canonical.lower()] = supervisor
        active_set.add(canonical.lower())

    # Minimum row width: just the prefix (cols A-F = 6 cells). Anything
    # narrower can't have agent name + overall_score + dialpad_link.
    min_cols = history_layout.COL_OVERALL_SCORE + 1

    records = []
    for row in history_rows[1:]:  # skip header
        if len(row) < min_cols:
            continue

        agent_raw = str(row[history_layout.COL_AGENT_NAME]).strip()
        if not agent_raw:
            continue

        ts = parse_timestamp(row[history_layout.COL_TIMESTAMP])
        if ts is None or ts.year < 2020:
            continue

        try:
            overall = float(row[history_layout.COL_OVERALL_SCORE])
        except (ValueError, TypeError):
            continue

        # Normalize agent name via Mails canonical map
        agent = canonical_map.get(
            agent_raw.lower(),
            canonical_map.get(strip_accents(agent_raw).lower(), agent_raw),
        )

        if is_excluded_test_row(agent, overall, excluded_agents):
            continue

        # Numeric section scores — keyed by history_id
        section_scores = {}
        for sec_name, col_idx in numeric_section_cols.items():
            try:
                section_scores[sec_name] = float(row[col_idx])
            except (ValueError, TypeError, IndexError):
                section_scores[sec_name] = np.nan

        # Y/N section values — keyed by history_id (no rename layer).
        yn_scores = {}
        for yn_name in yn_section_ids:
            yn_idx = yn_section_cols.get(yn_name)
            if yn_idx is None or len(row) <= yn_idx:
                yn_scores[yn_name] = ""
                continue
            yn_scores[yn_name] = str(row[yn_idx]).strip().upper()[:1]

        manager = (
            str(row[history_layout.COL_EVALUATOR_EMAIL]).strip().lower()
            if len(row) > history_layout.COL_EVALUATOR_EMAIL else ""
        )
        dialpad_link = (
            str(row[history_layout.COL_DIALPAD_LINK]).strip()
            if len(row) > history_layout.COL_DIALPAD_LINK else ""
        )

        # Extract eval_id from dialpad link (strip query params + [LONG CALL] suffix)
        eval_id = ""
        if dialpad_link:
            clean = dialpad_link.split("[")[0].strip().split("?")[0].strip()
            eval_id = clean.rstrip("/").split("/")[-1]

        records.append({
            "agent": agent,
            "timestamp": ts,
            "overall_score": overall,
            **section_scores,
            **yn_scores,
            "manager_email": manager,
            "is_active": agent.lower() in active_set,
            "supervisor": supervisor_map.get(agent.lower(), ""),
            "eval_id": eval_id,
        })

    df = pd.DataFrame(records)
    if df.empty:
        return df

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


# ---------------------------------------------------------------------------
# compute_long_form
# ---------------------------------------------------------------------------

def compute_long_form(df: pd.DataFrame, team_config: TeamConfig) -> pd.DataFrame:
    """Pivot the wide-form analytics DataFrame into long form.

    Returns one row per (evaluation, section) — the canonical analytical
    shape for downstream work (per-section trajectories, change-point
    detection, parquet export, ML feature stores).

    Columns: agent, eval_id, timestamp, supervisor, manager_email,
             section_id, section_name, score_type, score
        score is float for numeric sections, str ('Y'/'N'/'NA'/'') for yn.
    """
    base_cols = ["agent", "eval_id", "timestamp", "supervisor", "manager_email"]

    if df.empty:
        return pd.DataFrame(columns=[
            *base_cols, "section_id", "section_name", "score_type", "score",
        ])

    section_lookup = team_config.history_id_to_section
    pieces = []

    # Numeric + manual sections (load_and_clean writes them as float cols)
    for sec_id in team_config.numeric_history_ids:
        if sec_id not in df.columns:
            continue
        sec = section_lookup.get(sec_id)
        piece = df[base_cols].copy()
        piece["score"] = df[sec_id]
        piece["section_id"] = sec_id
        piece["section_name"] = sec.name if sec else sec_id
        piece["score_type"] = sec.score_type if sec else "numeric"
        pieces.append(piece)

    # Y/N sections
    for sec_id in team_config.yn_history_ids:
        if sec_id not in df.columns:
            continue
        sec = section_lookup.get(sec_id)
        piece = df[base_cols].copy()
        piece["score"] = df[sec_id]
        piece["section_id"] = sec_id
        piece["section_name"] = sec.name if sec else sec_id
        piece["score_type"] = "yn"
        pieces.append(piece)

    if not pieces:
        return pd.DataFrame(columns=[*base_cols, "section_id", "section_name", "score_type", "score"])

    long_df = pd.concat(pieces, ignore_index=True)
    return long_df[
        [*base_cols, "section_id", "section_name", "score_type", "score"]
    ]


# ---------------------------------------------------------------------------
# compute_outliers
# ---------------------------------------------------------------------------

def compute_outliers(df: pd.DataFrame, stats_config: StatsConfig) -> list[dict]:
    """Modified Z-score outlier detection per agent.

    Uses MAD (Median Absolute Deviation) which is robust to skewed distributions.
    Thresholds (min_evals, z-cutoff) come from the team's StatsConfig.
    """
    if df.empty:
        return []

    results = []
    for agent, group in df.groupby("agent"):
        if len(group) < stats_config.min_evals_for_outlier:
            continue

        scores = group["overall_score"].values
        median = np.median(scores)
        mad = np.median(np.abs(scores - median))

        if mad == 0:
            continue

        for _, row in group.iterrows():
            z = 0.6745 * (row["overall_score"] - median) / mad
            if abs(z) > stats_config.outlier_z_threshold:
                results.append({
                    "agent": agent,
                    "date": row["timestamp"].strftime("%Y-%m-%d"),
                    "score": round(row["overall_score"], 1),
                    "agent_median": round(float(median), 1),
                    "modified_z": round(float(z), 2),
                    "classification": "exceptional" if z > 0 else "concerning",
                    "eval_id": row.get("eval_id", ""),
                })

    results.sort(key=lambda x: abs(x["modified_z"]), reverse=True)
    return results


# ---------------------------------------------------------------------------
# compute_ewma
# ---------------------------------------------------------------------------

def compute_ewma(df: pd.DataFrame, stats_config: StatsConfig) -> list[dict]:
    """Per-agent EWMA on overall_score with trend detection.

    Trend is improving / declining / flat based on |Δ| > ewma_trend_delta
    between current EWMA and the value `span` evaluations ago.
    """
    if df.empty:
        return []

    span = stats_config.ewma_span
    min_evals = stats_config.ewma_min_evals
    trend_delta = stats_config.ewma_trend_delta

    results = []
    for agent, group in df.groupby("agent"):
        group = group.sort_values("timestamp")
        if len(group) < min_evals:
            continue

        scores = group["overall_score"]
        ewma_series = scores.ewm(span=span).mean()
        current = float(ewma_series.iloc[-1])

        lookback_idx = max(0, len(ewma_series) - span - 1)
        past = float(ewma_series.iloc[lookback_idx])
        diff = current - past

        if diff > trend_delta:
            trend = "improving"
        elif diff < -trend_delta:
            trend = "declining"
        else:
            trend = "flat"

        results.append({
            "agent": agent,
            "current_ewma": round(current, 1),
            "trend": trend,
            "eval_count": len(group),
        })

    results.sort(key=lambda x: x["current_ewma"])
    return results


# ---------------------------------------------------------------------------
# compute_monthly_spc
# ---------------------------------------------------------------------------

def compute_monthly_spc(df: pd.DataFrame, stats_config: StatsConfig) -> dict:
    """Monthly team average with Shewhart-style ±k·sigma control limits."""
    if df.empty:
        return {"months": [], "ucl": 0, "lcl": 0, "center": 0}

    df = df.copy()
    df["year_month"] = df["timestamp"].dt.to_period("M").astype(str)

    monthly = df.groupby("year_month").agg(
        mean=("overall_score", "mean"),
        count=("overall_score", "count"),
    ).reset_index()

    monthly = monthly.sort_values("year_month")

    means = monthly["mean"].values
    center = float(np.mean(means))
    sigma = float(np.std(means, ddof=1)) if len(means) > 1 else 0
    k = stats_config.spc_sigma_multiplier

    return {
        "months": [
            {
                "month": row["year_month"],
                "mean": round(float(row["mean"]), 1),
                "count": int(row["count"]),
            }
            for _, row in monthly.iterrows()
        ],
        "ucl": round(center + k * sigma, 1),
        "lcl": round(center - k * sigma, 1),
        "center": round(center, 1),
    }


# ---------------------------------------------------------------------------
# compute_section_analysis
# ---------------------------------------------------------------------------

def compute_section_analysis(df: pd.DataFrame, team_config: TeamConfig) -> dict:
    """Team section stats + per-agent weakness detection.

    Priority bands scale with each section's score_range so 1-10 teams
    aren't over-flagged by cutoffs calibrated for 1-5. Bands:
      gap > 0.125 * range → at least "low"
      gap >= 0.25  * range → "medium"
      gap >= 0.375 * range → "high"
    """
    numeric_sections = team_config.numeric_history_ids
    section_labels = team_config.section_labels

    if df.empty:
        return {"team_means": {}, "team_stds": {}, "training_opportunities": []}

    team_means = {}
    team_stds = {}
    for sec in numeric_sections:
        if sec in df.columns:
            vals = df[sec].dropna()
            team_means[sec] = round(float(vals.mean()), 2) if len(vals) > 0 else 0
            team_stds[sec] = round(float(vals.std(ddof=1)), 2) if len(vals) > 1 else 0

    opportunities = []
    for agent, group in df.groupby("agent"):
        for sec in numeric_sections:
            if sec not in df.columns:
                continue
            agent_vals = group[sec].dropna()
            if len(agent_vals) < 2:
                continue
            agent_avg = float(agent_vals.mean())
            t_avg = team_means.get(sec, 0)
            gap = t_avg - agent_avg

            range_size = _section_range_size(team_config, sec)
            gap_low = _GAP_LOW_FRAC * range_size
            gap_medium = _GAP_MED_FRAC * range_size
            gap_high = _GAP_HIGH_FRAC * range_size

            if gap > gap_low:
                if gap >= gap_high:
                    priority = "high"
                elif gap >= gap_medium:
                    priority = "medium"
                else:
                    priority = "low"
                opportunities.append({
                    "agent": agent,
                    "section": section_labels.get(sec, sec),
                    "agent_avg": round(agent_avg, 2),
                    "team_avg": round(t_avg, 2),
                    "gap": round(gap, 2),
                    "n": len(agent_vals),
                    "priority": priority,
                })

    opportunities.sort(key=lambda x: x["gap"], reverse=True)

    return {
        "team_means": {section_labels.get(k, k): v for k, v in team_means.items()},
        "team_stds": {section_labels.get(k, k): v for k, v in team_stds.items()},
        "training_opportunities": opportunities,
    }


# ---------------------------------------------------------------------------
# compute_binary_stats
# ---------------------------------------------------------------------------

def compute_binary_stats(
    df: pd.DataFrame,
    yn_section_labels: dict[str, str],
) -> list[dict]:
    """Per-section Y/N stats, returned as an ordered list.

    Each entry: {section_id, label, team_pct, agents: [{agent, pct, yes, total}]}.
    Frontend iterates this list — no hardcoded section keys.

    Empty DataFrame still returns one entry per configured Y/N section
    (with team_pct=0, agents=[]) so the dashboard renders a placeholder
    rather than collapsing the panel.
    """
    result = []
    for section_id, label in yn_section_labels.items():
        if df.empty or section_id not in df.columns:
            result.append({
                "section_id": section_id,
                "label": label,
                "team_pct": 0.0,
                "agents": [],
            })
            continue

        team_pct = _compute_binary_pct(df[section_id])

        agents = []
        for agent, group in df.groupby("agent"):
            pct = _compute_binary_pct(group[section_id])
            total = ((group[section_id] == "Y") | (group[section_id] == "N")).sum()
            if total > 0:
                agents.append({
                    "agent": agent,
                    "pct": pct,
                    "yes": int((group[section_id] == "Y").sum()),
                    "total": int(total),
                })

        agents.sort(key=lambda x: x["pct"])
        result.append({
            "section_id": section_id,
            "label": label,
            "team_pct": team_pct,
            "agents": agents,
        })

    return result


# ---------------------------------------------------------------------------
# compute_supervisor_stats
# ---------------------------------------------------------------------------

def compute_supervisor_stats(df: pd.DataFrame) -> list[dict]:
    """Per-supervisor aggregation using Mails-derived supervisor column."""
    if df.empty or "supervisor" not in df.columns:
        return []

    filtered = df[df["supervisor"].str.strip() != ""]
    if filtered.empty:
        return []

    results = []
    for sup, group in filtered.groupby("supervisor"):
        results.append({
            "supervisor": sup,
            "avg_score": round(float(group["overall_score"].mean()), 1),
            "std_score": round(float(group["overall_score"].std(ddof=1)), 1) if len(group) > 1 else 0,
            "eval_count": len(group),
            "agent_count": group["agent"].nunique(),
        })

    results.sort(key=lambda x: x["avg_score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# _compute_binary_pct (helper)
# ---------------------------------------------------------------------------

def _compute_binary_pct(series: pd.Series) -> float:
    """Compute percentage of 'Y' out of valid Y/N responses."""
    yes = int((series == "Y").sum())
    total = int(((series == "Y") | (series == "N")).sum())
    return round(100.0 * yes / total, 1) if total > 0 else 0.0


# ---------------------------------------------------------------------------
# compute_agent_roster
# ---------------------------------------------------------------------------

def compute_agent_roster(df: pd.DataFrame, team_config: TeamConfig) -> list[dict]:
    """Summary row per agent for the roster table.

    binary_pcts is a dict {section_id: pct} — one entry per configured
    Y/N section. The frontend renders these dynamically; old keys
    (id_val_pct / cust_res_pct) no longer exist.

    Weakness detection uses the same scaled threshold as
    ``compute_section_analysis`` (gap > 0.125 * section_range).
    """
    if df.empty:
        return []

    numeric_sections = team_config.numeric_history_ids
    section_labels = team_config.section_labels
    yn_section_ids = team_config.yn_history_ids
    stats_config = team_config.stats

    team_means = {}
    for sec in numeric_sections:
        if sec in df.columns:
            vals = df[sec].dropna()
            team_means[sec] = float(vals.mean()) if len(vals) > 0 else 0

    ewma_lookup = {entry["agent"]: entry for entry in compute_ewma(df, stats_config)}

    results = []
    for agent, group in df.groupby("agent"):
        scores = group["overall_score"]
        mean_score = float(scores.mean())
        std_score = float(scores.std(ddof=1)) if len(scores) > 1 else 0

        ewma_data = ewma_lookup.get(agent)
        ewma_val = ewma_data["current_ewma"] if ewma_data else None
        trend = ewma_data["trend"] if ewma_data else "flat"

        ref = ewma_val if ewma_val is not None else mean_score
        if ref >= 90:
            status = "excellent"
        elif ref >= 80:
            status = "good"
        elif ref >= 70:
            status = "watch"
        else:
            status = "at_risk"

        binary_pcts = {}
        for yn_id in yn_section_ids:
            if yn_id in group.columns:
                binary_pcts[yn_id] = _compute_binary_pct(group[yn_id])
            else:
                binary_pcts[yn_id] = 0.0

        weak = []
        for sec in numeric_sections:
            if sec not in df.columns:
                continue
            agent_vals = group[sec].dropna()
            if len(agent_vals) < 2:
                continue
            agent_avg = float(agent_vals.mean())
            range_size = _section_range_size(team_config, sec)
            if team_means.get(sec, 0) - agent_avg > _GAP_LOW_FRAC * range_size:
                weak.append(section_labels.get(sec, sec))

        is_active = bool(group["is_active"].iloc[0]) if "is_active" in group.columns else False
        supervisor = group["supervisor"].iloc[0] if "supervisor" in group.columns else ""

        results.append({
            "agent": agent,
            "n": len(group),
            "mean": round(mean_score, 1),
            "std": round(std_score, 1),
            "ewma": ewma_val,
            "trend": trend,
            "binary_pcts": binary_pcts,
            "status": status,
            "weak_sections": weak,
            "is_active": is_active,
            "supervisor": supervisor,
        })

    results.sort(key=lambda x: x["mean"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# compute_distribution
# ---------------------------------------------------------------------------

def compute_distribution(df: pd.DataFrame) -> list[dict]:
    """Score distribution histogram with non-overlapping bins."""
    if df.empty:
        return []

    bins = [
        (0, 20, "0-20"),
        (21, 40, "21-40"),
        (41, 60, "41-60"),
        (61, 70, "61-70"),
        (71, 80, "71-80"),
        (81, 90, "81-90"),
        (91, 100, "91-100"),
    ]

    scores = df["overall_score"].values
    result = []
    for lo, hi, label in bins:
        count = int(((scores >= lo) & (scores <= hi)).sum())
        result.append({"bin": label, "count": count})

    return result
