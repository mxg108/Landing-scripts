#!/usr/bin/env bash
# =============================================================================
# push.sh — Safe deployment wrapper for Landing Scripts
#
# Usage:  ./push.sh [mass-notifications | qa-automation]
#
# Prevents accidentally pushing to the wrong Google Apps Script project by
# showing the target Script ID and requiring explicit confirmation before
# calling `clasp push`.
#
# ⚠️  qa-automation is LIVE — its warning is intentionally louder.
# =============================================================================

set -euo pipefail

SCRIPT="${1:-}"

# ── Usage guard ───────────────────────────────────────────────────────────────
if [[ -z "$SCRIPT" ]]; then
  echo ""
  echo "  Usage: ./push.sh [mass-notifications | qa-automation]"
  echo ""
  echo "  mass-notifications   Push the Mass Notifications script (dev/staging)"
  echo "  qa-automation        Push the QA Automation script  ⚠️  LIVE"
  echo ""
  exit 1
fi

if [[ ! -d "$SCRIPT" ]]; then
  echo "Error: directory '$SCRIPT' not found."
  exit 1
fi

if [[ ! -f "$SCRIPT/.clasp.json" ]]; then
  echo "Error: $SCRIPT/.clasp.json not found."
  exit 1
fi

# ── Extract Script ID from .clasp.json (no Python/jq required) ───────────────
SCRIPT_ID=$(grep -o '"scriptId"[[:space:]]*:[[:space:]]*"[^"]*"' "$SCRIPT/.clasp.json" \
            | head -1 \
            | sed 's/.*"\([^"]*\)"[[:space:]]*$/\1/')

# ── Confirmation prompt ───────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo "  CLASP PUSH — CONFIRM DEPLOYMENT"
echo "════════════════════════════════════════"
echo ""
echo "  Project   : $SCRIPT"
echo "  Script ID : $SCRIPT_ID"
echo ""

if [[ "$SCRIPT" == "qa-automation" ]]; then
  echo "  ┌─────────────────────────────────────┐"
  echo "  │  ⚠️  WARNING: QA Automation is LIVE  │"
  echo "  │  Changes take effect immediately.   │"
  echo "  └─────────────────────────────────────┘"
  echo ""
fi

read -rp "  Type 'yes' to confirm push: " CONFIRM
echo ""

if [[ "$CONFIRM" != "yes" ]]; then
  echo "  Push cancelled."
  echo ""
  exit 0
fi

# ── Push ──────────────────────────────────────────────────────────────────────
echo "  Pushing $SCRIPT..."
echo ""
(cd "$SCRIPT" && clasp push)
echo ""
echo "  Done."
echo ""
