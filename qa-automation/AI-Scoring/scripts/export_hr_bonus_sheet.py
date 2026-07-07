#!/usr/bin/env python3
"""HR bonus spreadsheet export — mockup (2026-07-07).

Builds the HR-facing bonus workbook from an Analyst_History-shaped CSV:

- One summary tab per month ("June 2026"): one row per agent —
  Agent Name | Agent Email | Monthly Avg | the 9 HR-visible sections
  (numeric sections as avg /5, binary as %Yes). Documentation / Human
  Review Required is internal-only and never exported.
- One tab per agent: individual evaluations (Period, Date, Overall,
  sections, Evaluator, Dialpad link), newest first.

Top-up rule (mockup only, not bonus-driving): agents with ANY evaluation
in the target month are included; below --min-evals (20), their most
recent prior-month evaluations are pulled in until the floor is met or
history runs out. The Period column makes top-ups self-evident.

The production version of this surface is the GAS suite that pulls from
Postgres on the 1st of each month — this script freezes the layout HR
signs off on.

Usage:
    cd qa-automation/AI-Scoring
    python scripts/export_hr_bonus_sheet.py \
        --csv ../../database/analyst_history_member_support.csv \
        --month 2026-06 --spreadsheet <ID or URL> [--min-evals 20] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# CSV column -> HR-facing header, in HR's required order. Documentation is
# deliberately absent (internal-only).
HR_SECTIONS = [
    ("Greeting", "Greeting"),
    ("Caller Identity Validation", "Caller ID"),
    ("Purpose of the Call", "Purpose of the call"),
    ("Matching the Moment", "Matching the moment"),
    ("Process Adherence", "Process Adherence"),
    ("Call Resolution", "Call Resolution"),
    ("Communication", "Communication"),
    ("Efficiency & Call Handling", "Efficiency & Call Handling"),
    ("Customer Resolution Indicator", "CRI"),
]
BINARY_COLS = {"Caller Identity Validation", "Customer Resolution Indicator"}
_YN = {"Y": 1.0, "Yes": 1.0, "N": 0.0, "No": 0.0}
_DISPLAY = {"Y": "Yes", "Yes": "Yes", "N": "No", "No": "No",
            "Not Applicable": "N/A", "NA": "N/A"}
EXCLUDED_AGENTS = {"maximiliano perez", "maximiliano.perez"}


def load_rows(csv_path: Path, month: str, min_evals: int) -> dict[str, pd.DataFrame]:
    df = pd.read_csv(csv_path)
    df.columns = [c.strip() for c in df.columns]
    df["_ts"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    df = df[df["_ts"].notna()].sort_values("_ts", ascending=False)
    df["_agent"] = df["Agent Name"].str.strip()
    df = df[~df["_agent"].str.lower().isin(EXCLUDED_AGENTS)]
    df["_period"] = df["_ts"].dt.strftime("%Y-%m")

    in_month = df[df["_period"] == month]
    per_agent: dict[str, pd.DataFrame] = {}
    for agent in sorted(in_month["_agent"].unique(), key=str.lower):
        rows = in_month[in_month["_agent"] == agent]
        if len(rows) < min_evals:
            prior = df[(df["_agent"] == agent) & (df["_period"] < month)]
            rows = pd.concat([rows, prior.head(min_evals - len(rows))])
        per_agent[agent] = rows
    return per_agent


def _summary_row(agent: str, rows: pd.DataFrame) -> list:
    email = rows["Agent Email"].dropna().astype(str).str.strip()
    email = next((e for e in email if e and e.lower() != "nan"), "")
    overall = pd.to_numeric(rows["Overall Score"], errors="coerce").dropna()
    out = [agent, email, round(overall.mean(), 1) if len(overall) else ""]
    for col, _ in HR_SECTIONS:
        values = rows[col].astype(str).str.strip()
        if col in BINARY_COLS:
            mapped = values.map(_YN).dropna()
            out.append(f"{mapped.mean() * 100:.0f}%" if len(mapped) else "")
        else:
            scores = pd.to_numeric(values, errors="coerce").dropna()
            out.append(round(scores.mean(), 2) if len(scores) else "")
    return out


def _detail_rows(rows: pd.DataFrame) -> list[list]:
    header = (["Period", "Date", "Overall Score"]
              + [label for _, label in HR_SECTIONS]
              + ["Evaluator", "Dialpad Link"])
    body = []
    for _, r in rows.iterrows():
        cells = [r["_period"], r["_ts"].strftime("%m/%d/%Y %H:%M"),
                 r["Overall Score"]]
        for col, _ in HR_SECTIONS:
            raw = str(r[col]).strip()
            cells.append(_DISPLAY.get(raw, raw))
        cells.append(str(r.get("Evaluator Email", "") or ""))
        cells.append(str(r.get("Dialpad Link", "") or ""))
        body.append(cells)
    return [header] + body


def _paced(fn, *args, **kwargs):
    """Sheets caps write requests per minute; pace every write and back off
    on 429 so a 23-tab export completes in one run."""
    import time

    import gspread

    for attempt in range(4):
        try:
            result = fn(*args, **kwargs)
            time.sleep(1.3)
            return result
        except gspread.exceptions.APIError as e:
            if "429" not in str(e) or attempt == 3:
                raise
            print("  … write quota hit, backing off 65s")
            time.sleep(65)


def export(per_agent: dict[str, pd.DataFrame], month: str, spreadsheet_ref: str) -> None:
    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(raw) if raw.strip().startswith("{") else json.load(open(raw))
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", spreadsheet_ref)
    spreadsheet = client.open_by_key(match.group(1) if match else spreadsheet_ref)

    month_label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    existing = {ws.title: ws for ws in spreadsheet.worksheets()}

    # Summary tab first.
    summary = ([["Agent Name", "Agent Email", "Monthly Avg"]
                + [label for _, label in HR_SECTIONS]]
               + [_summary_row(agent, rows) for agent, rows in per_agent.items()])
    ws = existing.get(month_label) or _paced(
        spreadsheet.add_worksheet, month_label,
        rows=len(summary) + 5, cols=len(summary[0]) + 2, index=0,
    )
    _paced(ws.clear)
    _paced(ws.update, "A1", summary, value_input_option="USER_ENTERED")
    _paced(ws.freeze, rows=1)
    print(f"summary tab '{month_label}': {len(summary) - 1} agents")

    for agent, rows in per_agent.items():
        detail = _detail_rows(rows)
        tab = existing.get(agent) or _paced(
            spreadsheet.add_worksheet, agent,
            rows=len(detail) + 5, cols=len(detail[0]) + 2,
        )
        _paced(tab.clear)
        _paced(tab.update, "A1", detail, value_input_option="USER_ENTERED")
        _paced(tab.freeze, rows=1)
        print(f"  {agent}: {len(detail) - 1} evaluations")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument(
        "--spreadsheet",
        default=os.environ.get("GOOGLE_SHEETS_QA_BONUS_ID", ""),
        help="Spreadsheet ID or URL (defaults to GOOGLE_SHEETS_QA_BONUS_ID from .env)",
    )
    ap.add_argument("--min-evals", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    per_agent = load_rows(Path(args.csv), args.month, args.min_evals)
    total = sum(len(r) for r in per_agent.values())
    print(f"{len(per_agent)} agents, {total} evaluations "
          f"(month {args.month}, floor {args.min_evals})")
    if args.dry_run or not args.spreadsheet:
        for agent, rows in per_agent.items():
            topups = (rows["_period"] != args.month).sum()
            print(f"  {agent:28s} {len(rows):3d} rows"
                  + (f"  ({topups} topped up from prior months)" if topups else ""))
        if not args.spreadsheet:
            print("\nno --spreadsheet given — nothing written")
        return
    export(per_agent, args.month, args.spreadsheet)


if __name__ == "__main__":
    main()
