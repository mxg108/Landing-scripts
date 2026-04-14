"""Team-level statistical computations for the TeamStatsBoard.

All functions are pure computation — they take raw data (lists or DataFrames)
and return plain dicts/lists. No I/O, no Sheets API calls.

Section lists and column indices are passed as parameters (from TeamConfig)
rather than read from module-level constants.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from backend.config.team_config import AnalystHistoryConfig

# Timestamp formats
_TS_FORMATS = [
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
]


def _strip_accents(s: str) -> str:
    """Remove accent marks for comparison (e.g. Leon -> Leon)."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _parse_ts(val: str) -> datetime | None:
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(str(val).strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


# ---------------------------------------------------------------------------
# load_and_clean
# ---------------------------------------------------------------------------

def load_and_clean(
    history_rows: list[list[str]],
    mails_rows: list[list[str]],
    ah_config: AnalystHistoryConfig,
) -> pd.DataFrame:
    """Convert raw sheet rows into a cleaned DataFrame.

    Args:
        history_rows: Raw rows from Analyst_History (including header).
        mails_rows: Raw rows from Mails sheet (including header).
            Col A = Agent Name, B = Email, C = Supervisor, D = Canonical Name.
        ah_config: Column layout config for the Analyst_History tab.

    Returns:
        DataFrame with columns: agent, timestamp, overall_score,
        per-section scores, identity_validation, customer_resolution,
        manager_email, is_active, supervisor.
    """
    # Build maps from Mails sheet
    canonical_map: dict[str, str] = {}  # raw_name_lower -> canonical
    supervisor_map: dict[str, str] = {}  # canonical_lower -> supervisor
    active_set: set[str] = set()  # canonical_lower names

    for row in mails_rows[1:]:  # skip header
        if len(row) < 2 or not row[0].strip():
            continue
        raw_name = row[0].strip()
        supervisor = row[2].strip() if len(row) > 2 else ""
        canonical = row[3].strip() if len(row) > 3 and row[3].strip() else raw_name

        canonical_map[raw_name.lower()] = canonical
        canonical_map[_strip_accents(raw_name).lower()] = canonical
        supervisor_map[canonical.lower()] = supervisor
        active_set.add(canonical.lower())

    # Parse history rows
    numeric_sections = list(ah_config.section_columns.keys())
    records = []
    for row in history_rows[1:]:  # skip header
        if len(row) < 15:
            continue

        agent_raw = str(row[ah_config.col_agent_name]).strip()
        if not agent_raw:
            continue

        ts = _parse_ts(row[ah_config.col_timestamp])
        if ts is None or ts.year < 2020:
            continue

        try:
            overall = float(row[ah_config.col_overall_score])
        except (ValueError, TypeError):
            continue

        # Normalize agent name
        agent = canonical_map.get(
            agent_raw.lower(),
            canonical_map.get(_strip_accents(agent_raw).lower(), agent_raw),
        )

        # Exclude test rows (Maximiliano Perez with score 0)
        if _strip_accents(agent).lower().startswith("maximiliano") and overall == 0:
            continue

        # Parse numeric section scores
        section_scores = {}
        for sec_name, col_idx in ah_config.section_columns.items():
            try:
                section_scores[sec_name] = float(row[col_idx])
            except (ValueError, TypeError, IndexError):
                section_scores[sec_name] = np.nan

        # Parse Y/N columns.
        # The DataFrame uses fixed column names that downstream compute_*
        # functions and the API response depend on. Config provides the
        # column indices; the DataFrame column names are the internal contract.
        _YN_DF_NAMES = {
            "identity_validation": "identity_validation",
            "customer_resolution_indicator": "customer_resolution",
        }
        yn_scores = {}
        for yn_name, yn_idx in ah_config.yn_columns.items():
            val = str(row[yn_idx]).strip().upper()[:1] if len(row) > yn_idx else ""
            df_col = _YN_DF_NAMES.get(yn_name, yn_name)
            yn_scores[df_col] = val

        manager = str(row[ah_config.col_manager_email]).strip().lower() if len(row) > ah_config.col_manager_email else ""
        dialpad_link = str(row[ah_config.col_dialpad_link]).strip() if len(row) > ah_config.col_dialpad_link else ""

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
# compute_outliers
# ---------------------------------------------------------------------------

def compute_outliers(
    df: pd.DataFrame,
    min_evals: int = 5,
    threshold: float = 3.5,
) -> list[dict]:
    """Modified Z-score outlier detection per agent.

    Uses MAD (Median Absolute Deviation) which is robust to skewed distributions.
    """
    if df.empty:
        return []

    results = []
    for agent, group in df.groupby("agent"):
        if len(group) < min_evals:
            continue

        scores = group["overall_score"].values
        median = np.median(scores)
        mad = np.median(np.abs(scores - median))

        if mad == 0:
            continue

        for _, row in group.iterrows():
            z = 0.6745 * (row["overall_score"] - median) / mad
            if abs(z) > threshold:
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

def compute_ewma(
    df: pd.DataFrame,
    span: int = 5,
    min_evals: int = 3,
) -> list[dict]:
    """Per-agent EWMA with trend detection."""
    if df.empty:
        return []

    results = []
    for agent, group in df.groupby("agent"):
        group = group.sort_values("timestamp")
        if len(group) < min_evals:
            continue

        scores = group["overall_score"]
        ewma_series = scores.ewm(span=span).mean()
        current = float(ewma_series.iloc[-1])

        # Trend: compare current to value 5 evals ago (or first if < 5)
        lookback_idx = max(0, len(ewma_series) - span - 1)
        past = float(ewma_series.iloc[lookback_idx])
        diff = current - past

        if diff > 3:
            trend = "improving"
        elif diff < -3:
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

def compute_monthly_spc(df: pd.DataFrame) -> dict:
    """Monthly team average with Shewhart-style +/-2sigma control limits."""
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

    return {
        "months": [
            {
                "month": row["year_month"],
                "mean": round(float(row["mean"]), 1),
                "count": int(row["count"]),
            }
            for _, row in monthly.iterrows()
        ],
        "ucl": round(center + 2 * sigma, 1),
        "lcl": round(center - 2 * sigma, 1),
        "center": round(center, 1),
    }


# ---------------------------------------------------------------------------
# compute_section_analysis
# ---------------------------------------------------------------------------

def compute_section_analysis(
    df: pd.DataFrame,
    numeric_sections: list[str],
    section_labels: dict[str, str],
) -> dict:
    """Team section stats + per-agent weakness detection."""
    if df.empty:
        return {"team_means": {}, "team_stds": {}, "training_opportunities": []}

    team_means = {}
    team_stds = {}
    for sec in numeric_sections:
        if sec in df.columns:
            vals = df[sec].dropna()
            team_means[sec] = round(float(vals.mean()), 2) if len(vals) > 0 else 0
            team_stds[sec] = round(float(vals.std(ddof=1)), 2) if len(vals) > 1 else 0

    # Per-agent weakness detection
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
            if gap > 0.5:
                if gap >= 1.5:
                    priority = "high"
                elif gap >= 1.0:
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

def compute_binary_stats(df: pd.DataFrame) -> dict:
    """Team-wide binary section stats (Identity Validation, Customer Resolution).

    Returns per-section: team Y%, per-agent breakdown sorted by % ascending.
    """
    if df.empty:
        return {"identity_validation": {}, "customer_resolution": {}}

    result = {}
    for col, label in [
        ("identity_validation", "identity_validation"),
        ("customer_resolution", "customer_resolution"),
    ]:
        if col not in df.columns:
            result[label] = {"team_pct": 0, "agents": []}
            continue

        team_pct = _compute_binary_pct(df[col])

        agents = []
        for agent, group in df.groupby("agent"):
            pct = _compute_binary_pct(group[col])
            total = ((group[col] == "Y") | (group[col] == "N")).sum()
            if total > 0:
                agents.append({
                    "agent": agent,
                    "pct": pct,
                    "yes": int((group[col] == "Y").sum()),
                    "total": int(total),
                })

        agents.sort(key=lambda x: x["pct"])

        result[label] = {
            "team_pct": team_pct,
            "agents": agents,
        }

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
    yes = (series == "Y").sum()
    total = ((series == "Y") | (series == "N")).sum()
    return round(100 * yes / total, 1) if total > 0 else 0


# ---------------------------------------------------------------------------
# compute_agent_roster
# ---------------------------------------------------------------------------

def compute_agent_roster(
    df: pd.DataFrame,
    numeric_sections: list[str],
    section_labels: dict[str, str],
) -> list[dict]:
    """Summary row per agent for the roster table."""
    if df.empty:
        return []

    # Pre-compute team means for weakness detection
    team_means = {}
    for sec in numeric_sections:
        if sec in df.columns:
            vals = df[sec].dropna()
            team_means[sec] = float(vals.mean()) if len(vals) > 0 else 0

    # Pre-compute EWMA for all agents
    ewma_lookup = {}
    for entry in compute_ewma(df):
        ewma_lookup[entry["agent"]] = entry

    results = []
    for agent, group in df.groupby("agent"):
        scores = group["overall_score"]
        mean_score = float(scores.mean())
        std_score = float(scores.std(ddof=1)) if len(scores) > 1 else 0

        # EWMA and trend
        ewma_data = ewma_lookup.get(agent)
        ewma_val = ewma_data["current_ewma"] if ewma_data else None
        trend = ewma_data["trend"] if ewma_data else "flat"

        # Status based on EWMA (or mean if no EWMA)
        ref = ewma_val if ewma_val is not None else mean_score
        if ref >= 90:
            status = "excellent"
        elif ref >= 80:
            status = "good"
        elif ref >= 70:
            status = "watch"
        else:
            status = "at_risk"

        # Binary check percentages
        id_pct = _compute_binary_pct(group["identity_validation"])
        cust_pct = _compute_binary_pct(group["customer_resolution"])

        # Weak sections
        weak = []
        for sec in numeric_sections:
            if sec not in df.columns:
                continue
            agent_vals = group[sec].dropna()
            if len(agent_vals) < 2:
                continue
            agent_avg = float(agent_vals.mean())
            if team_means.get(sec, 0) - agent_avg > 0.5:
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
            "id_val_pct": id_pct,
            "cust_res_pct": cust_pct,
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
