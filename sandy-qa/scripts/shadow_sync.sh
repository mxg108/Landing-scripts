#!/bin/bash
# Cron wrapper for shadow_sync.py (interim pull-based shadow double-write).
# Every 30 min: full qa_* re-sync + qa_events publication. Replaced by the
# Railway push once Engineering provisions the App Service Token.
#
# Single-flight + hard timeout (2026-08-19 incident): a sync hung on a
# network call for 25h while cron kept stacking new runs on top; concurrent
# wipe+reimport cycles shredded the D1 mirror to a half-imported 1178 evals
# (Railway untouched). A stale-lock check + a 20-min kill cap make overlap
# impossible; an overlapped tick just logs SKIP.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOG="$ROOT/sandy-qa/scripts/shadow_sync.log"
LOCK="$ROOT/sandy-qa/scripts/.shadow_sync.lock"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
TIMEOUT_SECS=1200

if [ -f "$LOCK" ]; then
  LPID="$(cat "$LOCK" 2>/dev/null)"
  if [ -n "$LPID" ] && kill -0 "$LPID" 2>/dev/null; then
    echo "[$STAMP] SKIP: previous sync (pid $LPID) still running" >>"$LOG"
    exit 0
  fi
  echo "[$STAMP] stale lock (pid ${LPID:-?}) — clearing" >>"$LOG"
  rm -f "$LOCK"
fi
echo $$ >"$LOCK"
trap 'rm -f "$LOCK"' EXIT

cd "$ROOT/qa-automation/AI-Scoring" || exit 1
# Hard cap: python runs in the background; we wait with a deadline and kill
# the whole process group on overrun (no `timeout` binary on stock macOS).
.venv/bin/python "$ROOT/sandy-qa/scripts/shadow_sync.py" >"$LOCK.out" 2>&1 &
PY=$!
WAITED=0
while kill -0 "$PY" 2>/dev/null; do
  if [ "$WAITED" -ge "$TIMEOUT_SECS" ]; then
    kill -TERM "$PY" 2>/dev/null; sleep 3; kill -KILL "$PY" 2>/dev/null
    echo "[$STAMP] FAIL: killed after ${TIMEOUT_SECS}s (hung network call?)" >>"$LOG"
    tail -3 "$LOCK.out" >>"$LOG"
    rm -f "$LOCK.out"
    /usr/bin/osascript -e 'display notification "shadow sync HUNG — killed; see shadow_sync.log" with title "QA Sandy shadow"' 2>/dev/null
    exit 1
  fi
  sleep 5; WAITED=$((WAITED + 5))
done
wait "$PY"; RC=$?
OUT="$(cat "$LOCK.out")"; rm -f "$LOCK.out"
if [ "$RC" -eq 0 ]; then
  echo "[$STAMP] $(echo "$OUT" | tail -1)" >>"$LOG"
else
  echo "[$STAMP] FAIL:" >>"$LOG"
  echo "$OUT" | tail -5 >>"$LOG"
  /usr/bin/osascript -e 'display notification "shadow sync failed — see shadow_sync.log" with title "QA Sandy shadow"' 2>/dev/null
  exit 1
fi
