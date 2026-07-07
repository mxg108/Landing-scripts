#!/usr/bin/env python3
"""Landing-branded agent one-pager — print-ready HTML (July R2/R3 groundwork).

Reads an Analyst_History-shaped CSV export, filters to one agent + one month,
and renders a single-page, print-to-PDF HTML artifact: overall score with
call count and standard deviation, per-section averages and trends, and a
per-call score sparkline. R3 swaps the CSV for qa.evaluations and enriches
with the qa.assessments AI Assessment at end of month — the layout reserves
its slot.

Usage:
    cd qa-automation/AI-Scoring
    python scripts/export_onepager.py \
        --csv ../../database/export_document_test_data.csv \
        --month 2026-06 [--agent "Name"] [--out path.html]

Print to PDF from any browser (the page is sized for Letter portrait).
"""

from __future__ import annotations

import argparse
import html
import statistics
from datetime import datetime
from pathlib import Path

import pandas as pd

# Landing brand palette (qa-automation/teams/*/Branding.js)
NAVY = "#15192D"
BLUE = "#1A61D9"
LIGHT_BLUE = "#E7EFFB"
GREEN = "#28A745"
AMBER = "#E8A317"
RED = "#D9534F"
GRAY = "#4A4A4A"

_PREFIX = 6          # agent, email, timestamp, evaluator, link, overall
_N_SECTIONS = 10     # MS layout; sections occupy cols 6..15

_YN_VALUES = {"Yes": 1.0, "Y": 1.0, "No": 0.0, "N": 0.0}


def _parse(csv_path: Path, month: str, agent: str | None):
    df = pd.read_csv(csv_path)
    df.columns = [c.strip() for c in df.columns]
    df["_ts"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    df = df[df["_ts"].dt.strftime("%Y-%m") == month]
    if agent:
        df = df[df["Agent Name"].str.strip().str.lower() == agent.strip().lower()]
    else:
        agent = df["Agent Name"].str.strip().mode().iat[0]
        df = df[df["Agent Name"].str.strip() == agent]
    if df.empty:
        raise SystemExit(f"no rows for agent={agent!r} month={month}")
    df = df.sort_values("_ts")
    section_cols = list(df.columns[_PREFIX:_PREFIX + _N_SECTIONS])
    return df, agent, section_cols


def _section_stats(df: pd.DataFrame, col: str) -> dict:
    values = df[col].astype(str).str.strip()
    numeric = pd.to_numeric(values, errors="coerce")
    is_binary = values.isin(_YN_VALUES).any()
    midpoint = len(df) // 2
    out = {"name": col, "na": int(values.isin(["Not Applicable", "NA"]).sum())}
    if is_binary:
        mapped = values.map(_YN_VALUES).dropna()
        out["kind"] = "binary"
        out["avg"] = round(mapped.mean() * 100, 1) if len(mapped) else None
        first, second = mapped.iloc[:midpoint], mapped.iloc[midpoint:]
        out["delta"] = (
            round((second.mean() - first.mean()) * 100, 1)
            if len(first) and len(second) else None
        )
    else:
        scores = numeric.dropna()
        out["kind"] = "numeric"
        out["avg"] = round(scores.mean(), 2) if len(scores) else None
        first, second = scores.iloc[:midpoint], scores.iloc[midpoint:]
        out["delta"] = (
            round(second.mean() - first.mean(), 2)
            if len(first) and len(second) else None
        )
    return out


def _trend_arrow(delta) -> tuple[str, str]:
    if delta is None:
        return "—", GRAY
    if delta > 0.05:
        return f"▲ +{delta:g}", GREEN
    if delta < -0.05:
        return f"▼ {delta:g}", RED
    return "→ steady", GRAY


def _sparkline(scores: list[float], width=560, height=64) -> str:
    """Inline SVG polyline of per-call overall scores."""
    if len(scores) < 2:
        return ""
    lo, hi = min(scores + [60]), max(scores + [100])
    span = (hi - lo) or 1
    step = width / (len(scores) - 1)
    points = [
        (round(i * step, 1), round(height - (s - lo) / span * (height - 8) - 4, 1))
        for i, s in enumerate(scores)
    ]
    polyline = " ".join(f"{x},{y}" for x, y in points)
    dots = "".join(
        f'<circle cx="{x}" cy="{y}" r="2.5" fill="{BLUE}"/>' for x, y in points
    )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<polyline points="{polyline}" fill="none" stroke="{BLUE}" stroke-width="2"/>'
        f"{dots}</svg>"
    )


def _bar(stat: dict) -> str:
    if stat["avg"] is None:
        return ""
    pct = stat["avg"] if stat["kind"] == "binary" else stat["avg"] / 5 * 100
    color = GREEN if pct >= 80 else AMBER if pct >= 60 else RED
    return (
        f'<div class="bar"><div class="fill" '
        f'style="width:{pct:.0f}%;background:{color}"></div></div>'
    )


def render(df: pd.DataFrame, agent: str, month: str, section_cols: list[str]) -> str:
    overall = pd.to_numeric(df["Overall Score"], errors="coerce").dropna().tolist()
    n = len(overall)
    mean = statistics.fmean(overall)
    std = statistics.stdev(overall) if n > 1 else 0.0
    month_label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    stats = [_section_stats(df, c) for c in section_cols]

    rows = []
    for s in stats:
        arrow, color = _trend_arrow(s["delta"])
        if s["avg"] is None:
            avg_cell = "—"
        elif s["kind"] == "binary":
            avg_cell = f"{s['avg']:g}% Yes"
        else:
            avg_cell = f"{s['avg']:g} / 5"
        na_cell = f'{s["na"]} NA' if s["na"] else ""
        rows.append(
            f"<tr><td>{html.escape(s['name'])}</td>"
            f'<td class="num">{avg_cell}</td>'
            f"<td>{_bar(s)}</td>"
            f'<td class="num" style="color:{color}">{arrow}</td>'
            f'<td class="num muted">{na_cell}</td></tr>'
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(agent)} — QA {month_label}</title>
<style>
  @page {{ size: letter portrait; margin: 0.55in; }}
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ font: 13px/1.45 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
         color: {GRAY}; max-width: 7.4in; margin: 0 auto; padding: 24px 8px; }}
  header {{ background: {NAVY}; color: #fff; border-radius: 10px;
            padding: 18px 22px; display: flex; justify-content: space-between;
            align-items: baseline; }}
  header h1 {{ font-size: 21px; font-weight: 650; }}
  header .brand {{ color: {LIGHT_BLUE}; font-size: 12px; letter-spacing: 0.12em;
                   text-transform: uppercase; }}
  .cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;
            margin: 14px 0; }}
  .card {{ background: {LIGHT_BLUE}; border-radius: 8px; padding: 12px 14px; }}
  .card .v {{ font-size: 26px; font-weight: 700; color: {NAVY};
              font-variant-numeric: tabular-nums; }}
  .card .l {{ font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.08em; }}
  h2 {{ font-size: 12px; color: {NAVY}; text-transform: uppercase;
        letter-spacing: 0.1em; margin: 18px 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 5px 8px; border-bottom: 1px solid #eceff4; }}
  td.num {{ text-align: right; white-space: nowrap;
            font-variant-numeric: tabular-nums; }}
  .muted {{ color: #9aa3af; font-size: 11px; }}
  .bar {{ width: 130px; height: 7px; background: #e8ebf0; border-radius: 4px; }}
  .fill {{ height: 100%; border-radius: 4px; }}
  .assessment {{ border: 1.5px dashed {BLUE}; border-radius: 10px; padding: 14px 16px;
                 margin-top: 16px; color: {NAVY}; background: #fbfcff; }}
  footer {{ margin-top: 18px; font-size: 10.5px; color: #9aa3af; }}
</style></head><body>
<header>
  <div><h1>{html.escape(agent)}</h1>
  <div style="color:{LIGHT_BLUE}">Member Support — QA Performance</div></div>
  <div style="text-align:right"><div class="brand">Landing · QA</div>
  <div style="font-size:15px;font-weight:600">{month_label}</div></div>
</header>

<div class="cards">
  <div class="card"><div class="v">{mean:.1f}</div><div class="l">Avg overall score</div></div>
  <div class="card"><div class="v">{n}</div><div class="l">Calls evaluated</div></div>
  <div class="card"><div class="v">{std:.1f}</div><div class="l">Std deviation</div></div>
  <div class="card"><div class="v">{min(overall):.0f}–{max(overall):.0f}</div><div class="l">Score range</div></div>
</div>

<h2>Score progression — {month_label}</h2>
{_sparkline(overall)}

<h2>Per-section performance</h2>
<table><tbody>
{chr(10).join(rows)}
</tbody></table>

<div class="assessment">
  <strong>AI Assessment</strong><br>
  <span class="muted">Generated at end of month from the evaluation record —
  per-section trends, coaching focus, and progression summary land here
  automatically (arrives with the July close).</span>
</div>

<footer>Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} · Source: Analyst_History
({n} finalized evaluations, {month_label}) · Trend = second-half vs first-half average
· Scores computed by the Landing QA engine</footer>
</body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--agent", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    df, agent, section_cols = _parse(Path(args.csv), args.month, args.agent)
    out = Path(args.out) if args.out else Path(args.csv).parent / (
        f"onepager_{agent.replace(' ', '_')}_{args.month}.html"
    )
    out.write_text(render(df, agent, args.month, section_cols), encoding="utf-8")
    print(f"wrote {out}  ({len(df)} evaluations for {agent}, {args.month})")


if __name__ == "__main__":
    main()
