#!/usr/bin/env python3
"""Landing-branded agent one-pager — print-ready HTML (JulyR2R3 §3).

Sources qa.evaluations through the post-flip row-source (same bucket-TZ
month semantics as the dashboard) and fills the AI-Assessment slot from
qa.assessments — generating + persisting a calendar-month assessment via
the R3 writer when none exists (``--no-generate`` skips that for
cost-free re-renders). One HTML per agent, print-to-PDF ready (Letter
portrait).

Usage:
    cd qa-automation/AI-Scoring
    python3 scripts/export_onepager.py --team member_support --month 2026-06 \\
        [--agent "Name"] [--no-generate] [--include-departed] \\
        [--out-dir ../../database/eom_exports]
"""

from __future__ import annotations

import argparse
import asyncio
import html
import statistics
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

_AI_SCORING = Path(__file__).resolve().parent.parent
_DEFAULT_OUT = _AI_SCORING.parent.parent / "database" / "eom_exports"
load_dotenv(_AI_SCORING / ".env")
if str(_AI_SCORING) not in sys.path:
    sys.path.insert(0, str(_AI_SCORING))

# Landing brand palette (qa-automation/teams/*/Branding.js)
NAVY = "#15192D"
BLUE = "#1A61D9"
LIGHT_BLUE = "#E7EFFB"
GREEN = "#28A745"
AMBER = "#E8A317"
RED = "#D9534F"
GRAY = "#4A4A4A"

_YN_VALUES = {"Yes": 1.0, "Y": 1.0, "No": 0.0, "N": 0.0}


def frame_to_render(df_month: pd.DataFrame, agent: str, config):
    """One agent's month slice of the analytics frame → the render shape
    (display-label columns, canonical section order). The frame's yn cells
    are already the Y/N/NA/'' vocabulary _section_stats understands;
    numeric NAs arrive as NaN (the frame collapses the sheet-era
    'Not Applicable' string — the NA count column reflects yn sections
    only, a known fidelity trade)."""
    g = df_month[df_month["agent"] == agent].sort_values("timestamp")
    out = pd.DataFrame({
        "Agent Name": g["agent"].values,
        "_ts": g["timestamp"].values,
        "Overall Score": g["overall_score"].values,
    })
    section_cols = []
    for sec in config.sections_by_number:
        h = sec.history_id
        if h in g.columns:
            out[sec.name] = g[h].values
            section_cols.append(sec.name)
    return out, section_cols


def assessment_html(assessment: dict | None) -> str:
    """The AI-Assessment slot: the persisted qa.assessments row, or the
    reserved-slot placeholder when none exists."""
    if assessment is None:
        return (
            '<div class="assessment"><strong>AI Assessment</strong><br>'
            '<span class="muted">No persisted assessment for this window — '
            "run without --no-generate to create one.</span></div>"
        )
    lines = []
    for s in assessment.get("sections", []):
        arrow = {"improving": ("▲", GREEN), "declining": ("▼", RED)}.get(
            s["trend"], ("→", GRAY))
        lines.append(
            f'<div class="a-sec"><span style="color:{arrow[1]}">{arrow[0]}</span> '
            f"<strong>{html.escape(s['section_name'])}</strong> — "
            f"{html.escape(s['coaching_tip'])}</div>"
        )
    gen = assessment.get("generated_at")
    stamp = gen.strftime("%Y-%m-%d") if hasattr(gen, "strftime") else ""
    return (
        '<div class="assessment"><strong>AI Assessment</strong> '
        f'<span class="muted">({assessment.get("evaluations_included", "?")} evaluations'
        f'{" · " + stamp if stamp else ""} · {html.escape(str(assessment.get("rubric_version", "")))})</span>'
        f'<p style="margin:6px 0 8px">{html.escape(assessment["overall_assessment"])}</p>'
        f"{''.join(lines)}</div>"
    )


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


def render(df: pd.DataFrame, agent: str, month: str, section_cols: list[str],
           assessment: dict | None = None) -> str:
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
  .a-sec {{ font-size: 11.5px; margin: 2px 0; }}
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

{assessment_html(assessment)}

<footer>Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} · Source: qa.evaluations
({n} finalized evaluations, {month_label}) · Trend = second-half vs first-half average
· Scores computed by the Landing QA engine</footer>
</body></html>"""


async def run(args) -> int:
    from backend.config.team_config import get_team_config
    from backend.services.assessment_store import get_or_generate_month_assessment
    from backend.services.team_source import fetch_history_frame
    from backend.services.team_stats import _months_in_bucket_tz

    config = get_team_config(args.team)
    df = await fetch_history_frame(config)
    dm = df[_months_in_bucket_tz(df["timestamp"]) == args.month] if not df.empty else df
    if not args.include_departed and not dm.empty:
        dm = dm[dm["is_active"]]
    if dm.empty:
        print(f"[onepager:{args.team}] no rows for {args.month}")
        return 2

    agents = [args.agent] if args.agent else sorted(dm["agent"].unique())
    out_dir = Path(args.out_dir) / args.team / args.month / "onepagers"
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for agent in agents:
        rdf, section_cols = frame_to_render(dm, agent, config)
        if rdf.empty:
            print(f"  {agent}: no rows — skipped")
            continue
        assessment = await get_or_generate_month_assessment(
            config, agent, args.month, generate=not args.no_generate)
        slug = "_".join(agent.replace("/", " ").split())
        out = out_dir / f"onepager_{slug}_{args.month}.html"
        out.write_text(render(rdf, agent, args.month, section_cols, assessment),
                       encoding="utf-8")
        written += 1
        print(f"  {agent}: {len(rdf)} evals"
              + (", assessment ✓" if assessment else ", assessment —")
              + f" → {out.name}")

    print(f"[onepager:{args.team}] {args.month}: {written} page(s) → {out_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team", "--team-id", dest="team", required=True)
    ap.add_argument("--month", required=True, help="YYYY-MM (bucket-TZ month)")
    ap.add_argument("--agent", default=None, help="one agent; omit for all")
    ap.add_argument("--no-generate", action="store_true",
                    help="never call Gemini — reserved slot shows a note when "
                         "no assessment is persisted")
    ap.add_argument("--include-departed", action="store_true")
    ap.add_argument("--out-dir", default=str(_DEFAULT_OUT))
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
