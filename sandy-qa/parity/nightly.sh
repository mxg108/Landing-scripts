#!/bin/bash
# Nightly golden-fixture parity: Railway Postgres oracle vs the Sandy D1 port.
# Runs until Railway disconnect (SandyMigration Phase 5 exit checklist feeds
# on this log). Installed via crontab — see PortManifest §11.3.
#
# Re-pins every run to D1's current max finalized eval id, recaptures the
# Python fixture, reruns the worker's modules against D1, appends one verdict
# line per run to parity.log, and raises a macOS notification on any failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP_DIR="$ROOT/qa-automation/AI-Scoring"
PAR="$ROOT/sandy-qa/parity"
LOG="$PAR/parity.log"
PY3="/opt/homebrew/bin/python3"   # cron's PATH lacks homebrew; sandy.py needs 3.10+
SANDY="$HOME/.claude/commands/scripts/sandy.py"
APP_ID="a2cc5b5a-df29-4ae7-9dbb-e270052015e7"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"

notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"QA Sandy parity\"" 2>/dev/null; }

PIN=$("$PY3" "$SANDY" db query "$APP_ID" \
  "SELECT COALESCE(max(id),0) AS m FROM qa_evaluations WHERE state='finalized'" \
  2>>"$LOG" | "$PY3" -c "import json,sys; print(json.load(sys.stdin)['data'][0]['results'][0]['m'])" 2>>"$LOG")
if [ -z "${PIN:-}" ]; then
  echo "[$STAMP] FAIL: pin query — Sandy token expired? run: python3 $SANDY login --start" >>"$LOG"
  notify "pin query failed — Sandy token expired?"
  exit 1
fi

cd "$APP_DIR" || exit 1
if ! .venv/bin/python "$PAR/capture_fixture.py" --max-eval-id "$PIN" --out "$PAR/fixture.json" >>"$LOG" 2>&1; then
  echo "[$STAMP] FAIL: fixture capture (Railway PG reachable?)" >>"$LOG"
  notify "fixture capture failed"
  exit 1
fi

cd "$ROOT/sandy-qa" || exit 1
node_modules/wrangler/node_modules/esbuild/bin/esbuild parity/entry.ts \
  --bundle --format=esm --outfile=parity/lib.mjs --platform=neutral >/dev/null 2>&1
if node parity/run_parity.mjs parity/fixture.json >>"$LOG" 2>&1; then
  echo "[$STAMP] OK: FULL PARITY (pin=$PIN)" >>"$LOG"
else
  echo "[$STAMP] FAIL: PARITY MISMATCH (pin=$PIN) — diffs above" >>"$LOG"
  notify "PARITY MISMATCH — check sandy-qa/parity/parity.log"
  exit 2
fi
