#!/bin/bash
# Cron wrapper for shadow_sync.py (interim pull-based shadow double-write).
# Every 30 min: full qa_* re-sync + qa_events publication. Replaced by the
# Railway push once Engineering provisions the App Service Token.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOG="$ROOT/sandy-qa/scripts/shadow_sync.log"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
cd "$ROOT/qa-automation/AI-Scoring" || exit 1
if OUT=$(.venv/bin/python "$ROOT/sandy-qa/scripts/shadow_sync.py" 2>&1); then
  echo "[$STAMP] $(echo "$OUT" | tail -1)" >>"$LOG"
else
  echo "[$STAMP] FAIL:" >>"$LOG"
  echo "$OUT" | tail -5 >>"$LOG"
  /usr/bin/osascript -e 'display notification "shadow sync failed — see shadow_sync.log" with title "QA Sandy shadow"' 2>/dev/null
  exit 1
fi
