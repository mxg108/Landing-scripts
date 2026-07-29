"""One-time operator script: create the Dialpad call-event subscription.

DispositionDesign §4 — creates (1) a webhook endpoint pointing at the
Railway app and (2) a call-event subscription for the MS call center,
signed with a fresh secret.

Usage (from qa-automation/AI-Scoring, where the package now lives):

    python -m command_center.scripts.create_subscription \
        --url https://<railway-host>/api/webhooks/dialpad \
        [--call-center-id 4716644561813504] \
        [--secret <existing-secret>]

Requires DIALPAD_API_KEY in the environment (or AI-Scoring/.env). After
a successful run, record in qa-automation/AI-Scoring/.env:

    DIALPAD_WEBHOOK_SECRET=<printed secret>
    # webhook_id / subscription_id printed for the .env comment trail

At the Sandy re-platform, re-run with the new --url (the only
Dialpad-side change) and delete the old webhook in the admin console.
"""

from __future__ import annotations

import argparse
import os
import secrets as _secrets
import sys
from pathlib import Path

import httpx

BASE_URL = "https://dialpad.com/api/v2"

# §4.1.1 monitored call states.
CALL_STATES = [
    "ringing",
    "connected",
    "hold",
    "hangup",
    "recording",
    "call_transcription",
    "recap_summary",
]

MS_CALL_CENTER_ID = "4716644561813504"


def _api_key() -> str:
    key = os.environ.get("DIALPAD_API_KEY", "")
    if not key:
        # parents[2] = the AI-Scoring app root (this package lives at
        # <app-root>/command_center/ since the 2026-07-29 move).
        env_path = Path(__file__).resolve().parents[2] / ".env"
        try:
            from dotenv import load_dotenv
            if env_path.exists():
                load_dotenv(env_path)
                key = os.environ.get("DIALPAD_API_KEY", "")
        except ImportError:
            pass
    if not key:
        sys.exit("ERROR: DIALPAD_API_KEY not set (env or AI-Scoring/.env).")
    return key


def _check(resp: httpx.Response, doing: str) -> dict:
    """raise_for_status, but PRINT the response body first — Dialpad's 4xx
    bodies name the offending field, and losing them costs a debug cycle."""
    if resp.is_error:
        print(f"\nERROR while {doing}: HTTP {resp.status_code}", file=sys.stderr)
        print(resp.text, file=sys.stderr)
        resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url",
                        help="Public receiver URL (…/api/webhooks/dialpad)")
    parser.add_argument("--call-center-id", default=MS_CALL_CENTER_ID,
                        help=f"Dialpad call center id (default: MS {MS_CALL_CENTER_ID})")
    parser.add_argument("--secret", default=None,
                        help="Signing secret; generated when omitted")
    parser.add_argument("--webhook-id", default=None,
                        help="Reuse an existing webhook (whose secret you "
                             "have) instead of creating a new one")
    parser.add_argument("--delete-webhook", default=None, metavar="ID",
                        help="Delete a webhook by id and exit (cleanup for "
                             "orphaned/lost-secret webhooks)")
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {_api_key()}"}

    if args.delete_webhook:
        with httpx.Client(headers=headers, timeout=20) as client:
            resp = client.delete(f"{BASE_URL}/webhooks/{args.delete_webhook}")
            _check(resp, f"deleting webhook {args.delete_webhook}")
        print(f"webhook {args.delete_webhook} deleted")
        return

    if not args.url and not args.webhook_id:
        parser.error("--url is required (unless --delete-webhook)")
    if args.webhook_id and not args.secret:
        parser.error("--webhook-id requires --secret (the webhook's existing secret)")

    secret = args.secret or _secrets.token_urlsafe(32)
    # Printed BEFORE any API call: if a later step fails, the webhook that
    # was already created is still usable — a lost secret orphans it.
    print(f"DIALPAD_WEBHOOK_SECRET={secret}")

    with httpx.Client(headers=headers, timeout=20) as client:
        if args.webhook_id:
            webhook_id = args.webhook_id
            print(f"webhook_id: {webhook_id} (reused)")
        else:
            webhook = _check(
                client.post(
                    f"{BASE_URL}/webhooks",
                    json={"hook_url": args.url, "secret": secret},
                ),
                "creating webhook",
            )
            webhook_id = webhook.get("id")
            print(f"webhook_id: {webhook_id}")

        sub = _check(
            client.post(
                f"{BASE_URL}/subscriptions/call",
                json={
                    "webhook_id": webhook_id,
                    "target_id": int(args.call_center_id),
                    "target_type": "callcenter",
                    "call_states": CALL_STATES,
                },
            ),
            "creating call-event subscription",
        )
        print(f"subscription_id: {sub.get('id')}")

    print()
    print("Add to qa-automation/AI-Scoring/.env (and Railway env):")
    print(f"  DIALPAD_WEBHOOK_SECRET={secret}")
    print(f"  # dialpad webhook_id={webhook_id} subscription_id={sub.get('id')}")


if __name__ == "__main__":
    main()
