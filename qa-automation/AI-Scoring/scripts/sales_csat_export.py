"""Sales management export — high-survey-CSAT long calls → Google Sheet.

Pulls the Sales contact center's calls via the Dialpad Stats API, keeps
calls where **survey CSAT ≥ threshold AND call duration ≥ 10 minutes**
(Sales' criteria, v2 — the v1 OR-with-MOS filter matched essentially
every healthy call since MOS≥4 is a network-quality norm), newest
first, capped. Fetches each qualifying call's transcript and writes the
table to a dedicated tab of the destination spreadsheet. Duration is
computed from date_connected → date_ended (the CSV duration columns
have ambiguous units; the timestamps don't).

Column notes (probed live 2026-07-31):
- survey CSAT   → stats export stat_type="csat" (`response` per call_id)
- mos_score     → GET /call/{id} (not in any stats export)
- caller email  → GET /call/{id} `contact.email`
- operator email→ stats export stat_type="calls" (`email` column)
- **ai_csat is NOT available on this API surface** — "ai_csat" is not a
  valid stats enum and the call object doesn't carry it; it arrives only
  in the webhook payload (org-subscription-cap blocked, DispositionDesign).
  The column is emitted blank so the sheet shape is final; join it in
  once the webhook flows.

The destination TAB is created (or cleared and rewritten) on each run —
other tabs in the workbook are never touched. Transcript JSON cells are
truncated to fit the 50k-char Sheets cell cap.

Usage (from qa-automation/AI-Scoring, .env loaded):

    .venv/bin/python scripts/sales_csat_export.py
    .venv/bin/python scripts/sales_csat_export.py --days 30 --limit 100
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import gspread  # noqa: E402
import httpx  # noqa: E402
from google.oauth2.service_account import Credentials  # noqa: E402

from backend.services.dialpad_client import build_dialpad_link  # noqa: E402

BASE_URL = "https://dialpad.com/api/v2"
SALES_CALL_CENTER_ID = 5617801893085184
DEFAULT_SHEET_ID = "1kmorDb-G8KAtZResBWprgd1abL8l0I9cAOxBFfHDFs0"
TAB_NAME = "sales_csat_export"
CELL_CAP = 45_000          # Sheets hard cap is 50k chars/cell
POLL_INTERVAL_S = 3
POLL_TIMEOUT_S = 300

HEADER = [
    "operator_email", "timestamp", "dialpad_link", "transcript_json",
    "mos_score", "ai_csat", "survey_csat", "caller_email", "caller_phone",
]


def _headers() -> dict:
    api_key = os.environ.get("DIALPAD_API_KEY", "")
    if not api_key:
        raise RuntimeError("DIALPAD_API_KEY not set")
    return {"Authorization": f"Bearer {api_key}"}


async def _stats_export(client: httpx.AsyncClient, stat_type: str,
                        days: int) -> list[dict]:
    """Initiate → poll → download → parse one stats records export."""
    resp = await client.post(f"{BASE_URL}/stats", json={
        "export_type": "records",
        "stat_type": stat_type,
        "timezone": os.environ.get("CC_STATS_PULL_TZ", "America/Mexico_City"),
        "target_id": SALES_CALL_CENTER_ID,
        "target_type": "callcenter",
        "days_ago_start": 1,
        "days_ago_end": days,
    })
    resp.raise_for_status()
    request_id = resp.json()["request_id"]
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT_S
    while True:
        body = (await client.get(f"{BASE_URL}/stats/{request_id}")).json()
        if body.get("status") == "complete":
            download = await client.get(body["download_url"],
                                        follow_redirects=True)
            download.raise_for_status()
            return list(csv.DictReader(io.StringIO(download.text)))
        if body.get("status") == "failed":
            raise RuntimeError(f"stats export {stat_type} failed: {body}")
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"stats export {stat_type} timed out")
        await asyncio.sleep(POLL_INTERVAL_S)


def _parse_ts(raw: str) -> datetime | None:
    """CSV timestamps look like '2026-07-20 01:35:37.123456' (export TZ,
    same for every column — deltas are safe on naive datetimes)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw.split(".")[0], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _duration_minutes(row: dict) -> float | None:
    start = _parse_ts(row.get("date_connected", ""))
    end = _parse_ts(row.get("date_ended", ""))
    if start is None or end is None or end <= start:
        return None
    return (end - start).total_seconds() / 60


def _survey_scores(csat_rows: list[dict]) -> dict[str, float]:
    """call_id → numeric survey response (highest when multiple rows —
    follow-up questions produce extra rows for the same call)."""
    scores: dict[str, float] = {}
    for row in csat_rows:
        call_id = (row.get("call_id") or "").strip()
        raw = (row.get("response") or "").strip()
        if not call_id or not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        scores[call_id] = max(value, scores.get(call_id, value))
    return scores


async def _get_call(client: httpx.AsyncClient, call_id: str) -> dict:
    resp = await client.get(f"{BASE_URL}/call/{call_id}")
    resp.raise_for_status()
    return resp.json()


async def _get_transcript_json(client: httpx.AsyncClient, call_id: str) -> str:
    resp = await client.get(f"{BASE_URL}/transcripts/{call_id}")
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    text = json.dumps(resp.json(), ensure_ascii=False)
    if len(text) > CELL_CAP:
        text = text[:CELL_CAP] + "…[truncated]"
    return text


def _write_sheet(sheet_id: str, rows: list[list[str]]) -> str:
    creds_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not creds_env:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if creds_env.strip().startswith("{"):
        creds = Credentials.from_service_account_info(
            json.loads(creds_env), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(creds_env, scopes=scopes)
    client = gspread.authorize(creds)
    client.set_timeout(120)
    spreadsheet = client.open_by_key(sheet_id)
    try:
        tab = spreadsheet.worksheet(TAB_NAME)
        tab.clear()
    except gspread.WorksheetNotFound:
        tab = spreadsheet.add_worksheet(
            TAB_NAME, rows=len(rows) + 10, cols=len(HEADER))
    tab.update(f"A1:{chr(ord('A') + len(HEADER) - 1)}{len(rows) + 1}",
               [HEADER] + rows, value_input_option="RAW")
    return tab.url


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    parser.add_argument("--days", type=int, default=30,
                        help="lookback window (days_ago_end)")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--min-score", type=float, default=4.0,
                        help="survey CSAT threshold")
    parser.add_argument("--min-minutes", type=float, default=10.0,
                        help="minimum connected call duration")
    args = parser.parse_args()

    async with httpx.AsyncClient(headers=_headers(), timeout=30) as client:
        print(f"→ calls + csat exports for Sales CC ({args.days}d) …")
        calls_rows, csat_rows = await asyncio.gather(
            _stats_export(client, "calls", args.days),
            _stats_export(client, "csat", args.days),
        )
        survey = _survey_scores(csat_rows)
        print(f"  {len(calls_rows)} call rows, "
              f"{len(survey)} calls with survey responses")

        # Both criteria come straight from the CSVs, so qualification is
        # free — per-call GETs (mos + caller contact) happen only for
        # calls already known to qualify. Newest first.
        candidates = []
        for row in calls_rows:
            call_id = (row.get("call_id") or "").strip()
            if not call_id:
                continue
            survey_score = survey.get(call_id)
            if survey_score is None or survey_score < args.min_score:
                continue
            minutes = _duration_minutes(row)
            if minutes is None or minutes < args.min_minutes:
                continue
            candidates.append((row, survey_score, minutes))
        candidates.sort(key=lambda c: c[0].get("date_started") or "",
                        reverse=True)
        print(f"  {len(candidates)} calls meet survey≥{args.min_score} "
              f"AND duration≥{args.min_minutes:g}min")

        out_rows: list[list[str]] = []
        checked = 0
        for row, survey_score, minutes in candidates:
            if len(out_rows) >= args.limit:
                break
            call_id = row["call_id"].strip()
            checked += 1

            try:
                call = await _get_call(client, call_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    await asyncio.sleep(10)
                    call = await _get_call(client, call_id)
                else:
                    print(f"  !! call {call_id}: HTTP "
                          f"{exc.response.status_code} — skipped")
                    continue
            mos = call.get("mos_score")

            transcript = await _get_transcript_json(client, call_id)
            contact = call.get("contact") or {}
            timestamp = (row.get("date_connected")
                         or row.get("date_started") or "")
            out_rows.append([
                (row.get("email") or "").strip(),
                timestamp,
                build_dialpad_link(
                    call_id, (row.get("entry_point_call_id") or "").strip()),
                transcript,
                str(mos) if mos is not None else "",
                "",   # ai_csat — webhook-only, see module docstring
                str(survey_score) if survey_score is not None else "",
                (contact.get("email") or "").strip(),
                (contact.get("phone") or row.get("external_number") or "").strip(),
            ])
            if len(out_rows) % 10 == 0:
                print(f"  {len(out_rows)}/{args.limit} collected "
                      f"({checked} candidates checked)")
            await asyncio.sleep(0.15)

    print(f"→ writing {len(out_rows)} rows to tab {TAB_NAME!r} …")
    url = _write_sheet(args.sheet_id, out_rows)
    print(f"✓ done — {len(out_rows)} calls ({checked} candidates checked): {url}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
