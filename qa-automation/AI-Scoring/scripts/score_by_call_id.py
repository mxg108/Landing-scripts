"""
score_by_call_id.py — end-to-end smoke for the Lookup-to-Score pipeline.

Hits the running backend with a real call_id, a real privileged API key,
and a real agent_email, exercising every stage:

  1. POST /api/{team}/score   (audio fetched via Dialpad download_recording)
  2. Poll /api/{team}/score/{job_id} until complete
  3. POST /api/{team}/score/{job_id}/approve (sections passed through
     unchanged — no analyst edits; manual sections submitted as NA / 3)
  4. Stage 5 dispatches a real QA evaluation email via Apps Script

REAL SIDE EFFECTS: this writes to Google Sheets and DISPATCHES THE QA EMAIL
to the configured recipients of the chosen team. The script asks for a "y"
confirmation before the approve step so a typo doesn't accidentally send
an email. Use --score-only to skip approval (handy for repeated dev runs).

Usage:
    python3 scripts/score_by_call_id.py \\
        --team member_support \\
        --call-id 4924792590901248 \\
        --agent-email luis@hellolanding.com \\
        --manager-email you@hellolanding.com \\
        --api-key $API_KEY_PRIVILEGED

    # Or via env (recommended — keeps the key out of shell history):
    AI_SCORING_API_KEY=$API_KEY_PRIVILEGED \\
      python3 scripts/score_by_call_id.py \\
        --team member_support \\
        --call-id 4924792590901248 \\
        --agent-email luis@hellolanding.com \\
        --manager-email you@hellolanding.com

Exit codes:
    0  success (scored, optionally approved)
    1  HTTP / pipeline error
    2  user aborted at the approve confirmation
    3  bad arguments
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx


DEFAULT_BASE_URL = "http://localhost:8000"


def _die(code: int, msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="End-to-end Lookup-to-Score smoke against a running backend.",
    )
    parser.add_argument("--team", required=True, help="Target team_id (e.g. member_support)")
    parser.add_argument("--call-id", required=True, help="Dialpad call ID to score")
    parser.add_argument("--agent-email", required=True, help="Agent's email (for auth + audit)")
    parser.add_argument("--manager-email", required=True, help="Your email (evaluator)")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("AI_SCORING_API_KEY", ""),
        help="Bearer token. Defaults to $AI_SCORING_API_KEY.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"Backend base URL (default: {DEFAULT_BASE_URL}).")
    parser.add_argument("--poll-interval", type=float, default=3.0,
                        help="Seconds between /score/{job_id} polls (default: 3).")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="Max seconds to wait for status=complete (default: 300).")
    parser.add_argument("--score-only", action="store_true",
                        help="Stop after scoring; skip /approve (no email dispatched).")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the approve confirmation prompt (CI use).")
    args = parser.parse_args()
    if not args.api_key:
        _die(3, "missing --api-key (or $AI_SCORING_API_KEY)")
    return args


def _auth(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def submit_score(args, client: httpx.Client) -> str:
    """POST /score without audio_file → backend fetches via download_recording."""
    url = f"{args.base_url}/api/{args.team}/score"
    print(f"→ POST {url}")
    resp = client.post(
        url,
        headers=_auth(args.api_key),
        data={
            "call_id": args.call_id,
            "agent_email": args.agent_email,
            "manager_email": args.manager_email,
        },
    )
    if resp.status_code != 200:
        _die(1, f"/score returned {resp.status_code}: {resp.text}")
    body = resp.json()
    job_id = body["job_id"]
    print(f"  job_id = {job_id}  status = {body['status']}")
    return job_id


def poll_until_complete(args, client: httpx.Client, job_id: str) -> dict:
    url = f"{args.base_url}/api/{args.team}/score/{job_id}"
    deadline = time.monotonic() + args.timeout
    last_status = ""
    while time.monotonic() < deadline:
        resp = client.get(url, headers=_auth(args.api_key))
        if resp.status_code != 200:
            _die(1, f"GET {url} returned {resp.status_code}: {resp.text}")
        data = resp.json()
        status = data.get("status", "?")
        if status != last_status:
            print(f"  status = {status}")
            last_status = status
        if status == "complete":
            return data
        if status == "error":
            _die(1, f"scoring job errored: {data.get('error', '(no detail)')}")
        time.sleep(args.poll_interval)
    _die(1, f"job did not reach 'complete' within {args.timeout}s (last status: {last_status})")
    return {}  # unreachable, makes the type checker happy


def confirm_approve(scorecard: dict) -> bool:
    """Print a summary + ask for confirmation. Returns True if the user types 'y'."""
    avg_numeric = [s.get("score") for s in scorecard.get("sections", [])
                   if s.get("score_type") == "numeric" and s.get("score") is not None]
    avg = sum(avg_numeric) / len(avg_numeric) if avg_numeric else None

    print()
    print("=" * 64)
    print("READY TO APPROVE — this will dispatch the QA evaluation email.")
    print(f"  Agent:      {scorecard.get('agent_name')}")
    print(f"  Call:       {scorecard.get('call_id')}")
    print(f"  Avg score:  {avg:.2f}/5" if avg is not None else "  Avg score:  N/A")
    print(f"  Sections:   {len(scorecard.get('sections', []))}")
    print("=" * 64)
    answer = input("Send the QA email now? [y/N] ").strip().lower()
    return answer == "y"


def build_approval_payload(scorecard: dict, team_sections: list[dict]) -> dict:
    """Pass AI sections through unchanged; fill manual sections with placeholders."""
    ai_by_id = {s["id"]: s for s in scorecard.get("sections", [])}
    sections: list[dict] = []
    for ts in team_sections:
        if ts.get("auto_value"):
            continue
        if ts["score_type"] in ("manual", "manual_yn"):
            is_yn = ts["score_type"] == "manual_yn"
            sections.append({
                "id": ts["id"],
                "name": ts["name"],
                "score": None if is_yn else 3,
                "score_type": ts["score_type"],
                "yn_value": "NA" if is_yn else None,
                "confidence": "manual",
                "reasoning": "Auto-filled by score_by_call_id.py smoke.",
                "audio_dependent": False,
                "flags": [],
            })
        else:
            s = ai_by_id.get(ts["id"])
            if not s:
                continue
            sections.append(s)
    return {
        "sections": sections,
        "key_strengths": scorecard.get("key_strengths", ""),
        "opportunities": scorecard.get("opportunities", ""),
    }


def fetch_team_sections(args, client: httpx.Client) -> list[dict]:
    url = f"{args.base_url}/api/{args.team}/team/sections"
    resp = client.get(url, headers=_auth(args.api_key))
    if resp.status_code != 200:
        _die(1, f"GET {url} returned {resp.status_code}: {resp.text}")
    return resp.json()


def approve(args, client: httpx.Client, job_id: str, payload: dict) -> dict:
    url = f"{args.base_url}/api/{args.team}/score/{job_id}/approve"
    print(f"→ POST {url}")
    resp = client.post(
        url,
        headers={**_auth(args.api_key), "Content-Type": "application/json"},
        content=json.dumps(payload).encode("utf-8"),
        timeout=120.0,
    )
    if resp.status_code != 200:
        _die(1, f"/approve returned {resp.status_code}: {resp.text}")
    return resp.json()


def main() -> int:
    args = parse_args()

    with httpx.Client(timeout=60.0) as client:
        job_id = submit_score(args, client)
        complete = poll_until_complete(args, client, job_id)
        sheets_row = complete.get("sheets_row")
        print(f"  scored. FR-AI row = {sheets_row}")

        if args.score_only:
            print()
            print(f"score-only: stopping before /approve. Job {job_id} parked at 'complete'.")
            return 0

        if not args.yes and not confirm_approve(complete.get("scorecard", {})):
            print("aborted by user.")
            return 2

        team_sections = fetch_team_sections(args, client)
        payload = build_approval_payload(complete.get("scorecard", {}), team_sections)
        result = approve(args, client, job_id, payload)

        print()
        print("APPROVED:")
        for k in ("fr_ai_row", "destination_row", "history_row", "overall_score"):
            if k in result:
                print(f"  {k} = {result[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
