#!/usr/bin/env bash
# =============================================================================
# push.sh — Safe deployment wrapper for Landing Scripts
#
# Usage:  ./push.sh <project> [--dry-run]
#         ./push.sh --list
#
# Projects are registered in ./push.projects (tab-separated).  The wrapper
# shows the target Script ID, current branch/commit, and working-tree state
# before requiring explicit "yes" confirmation.  Successful pushes are
# appended to ./.push-log for local auditability.
#
# ⚠️  Projects flagged `live` in push.projects get a louder warning.
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$REPO_ROOT/push.projects"
LOG_FILE="$REPO_ROOT/.push-log"

DRY_RUN=0
ACTION=""

# ── Arg parsing ───────────────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --list)    ACTION="list" ;;
    -h|--help) ACTION="help" ;;
    -*)
      echo "Unknown flag: $arg" >&2
      exit 2
      ;;
    *)
      if [[ -z "${TARGET:-}" ]]; then
        TARGET="$arg"
      else
        echo "Unexpected extra argument: $arg" >&2
        exit 2
      fi
      ;;
  esac
done

# ── Manifest loader ───────────────────────────────────────────────────────────
if [[ ! -f "$MANIFEST" ]]; then
  echo "Error: manifest not found at $MANIFEST" >&2
  exit 1
fi

# Bash 3.2 (macOS default) has no associative arrays — use parallel indexed
# arrays and lookup-by-iteration helpers.
PROJECT_NAMES=()
PROJECT_DIRS=()
PROJECT_LIVES=()
PROJECT_DESCS=()

while IFS=$'\t' read -r name dir live desc || [[ -n "$name" ]]; do
  # Skip comments and blanks
  [[ -z "$name" || "$name" =~ ^[[:space:]]*# ]] && continue
  PROJECT_NAMES+=("$name")
  PROJECT_DIRS+=("$dir")
  PROJECT_LIVES+=("$live")
  PROJECT_DESCS+=("$desc")
done < "$MANIFEST"

# Look up the index of a project by name; sets REPLY to the index or -1.
find_project() {
  local needle="$1"
  local i
  for i in "${!PROJECT_NAMES[@]}"; do
    if [[ "${PROJECT_NAMES[$i]}" == "$needle" ]]; then
      REPLY="$i"
      return 0
    fi
  done
  REPLY="-1"
  return 1
}

# ── Help / list ───────────────────────────────────────────────────────────────
print_usage() {
  echo ""
  echo "  Usage: ./push.sh <project> [--dry-run]"
  echo "         ./push.sh --list"
  echo ""
  echo "  Projects registered in push.projects:"
  local i marker
  for i in "${!PROJECT_NAMES[@]}"; do
    marker=""
    [[ "${PROJECT_LIVES[$i]}" == "yes" ]] && marker="  ⚠️  LIVE"
    printf "    %-22s %s%s\n" "${PROJECT_NAMES[$i]}" "${PROJECT_DESCS[$i]}" "$marker"
  done
  echo ""
}

if [[ "$ACTION" == "help" || ( -z "${TARGET:-}" && "$ACTION" != "list" ) ]]; then
  print_usage
  [[ "$ACTION" == "help" ]] && exit 0 || exit 1
fi

if [[ "$ACTION" == "list" ]]; then
  print_usage
  exit 0
fi

# ── Validate target against manifest ──────────────────────────────────────────
if ! find_project "$TARGET"; then
  echo "Error: unknown project '$TARGET'." >&2
  echo "       Run './push.sh --list' to see registered projects." >&2
  exit 1
fi
IDX="$REPLY"
CLASP_DIR="${PROJECT_DIRS[$IDX]}"
LIVE="${PROJECT_LIVES[$IDX]}"
DESC="${PROJECT_DESCS[$IDX]}"

if [[ ! -d "$REPO_ROOT/$CLASP_DIR" ]]; then
  echo "Error: directory '$CLASP_DIR' (from manifest) does not exist." >&2
  exit 1
fi

if [[ ! -f "$REPO_ROOT/$CLASP_DIR/.clasp.json" ]]; then
  echo "Error: $CLASP_DIR/.clasp.json not found." >&2
  exit 1
fi

# ── Extract Script ID from .clasp.json (no jq dependency) ────────────────────
SCRIPT_ID=$(grep -o '"scriptId"[[:space:]]*:[[:space:]]*"[^"]*"' "$REPO_ROOT/$CLASP_DIR/.clasp.json" \
            | head -1 \
            | sed 's/.*"\([^"]*\)"[[:space:]]*$/\1/')

# ── Detect multi-team overlay layout ─────────────────────────────────────────
# Convention: if clasp_dir matches "*/teams/*", treat as a multi-team project.
# At push time we stage <project_root>/src/* into <project_root>/.build/<team>/
# and overlay <project_root>/teams/<team>/* on top, then push from there.
# This lets multiple teams share logic files while overriding Config.js (and
# any other file they need) without forking the whole tree.
BUILD_DIR=""
BASE_DIR=""
PUSH_FROM="$CLASP_DIR"
if [[ "$CLASP_DIR" =~ ^(.*)/teams/([^/]+)$ ]]; then
  PROJECT_ROOT="${BASH_REMATCH[1]}"
  TEAM="${BASH_REMATCH[2]}"
  BASE_DIR="$PROJECT_ROOT/src"
  BUILD_DIR="$PROJECT_ROOT/.build/$TEAM"
  PUSH_FROM="$BUILD_DIR"

  if [[ ! -d "$REPO_ROOT/$BASE_DIR" ]]; then
    echo "Error: shared base '$BASE_DIR' not found (required for multi-team layout)." >&2
    exit 1
  fi
fi

# ── Git pre-flight ────────────────────────────────────────────────────────────
cd "$REPO_ROOT"

BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "(not a git repo)")
SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
SUBJECT=$(git log -1 --format='%s' 2>/dev/null || echo "")

DIRTY=""
if git status --porcelain 2>/dev/null | grep -q .; then
  DIRTY="yes"
fi

# ── Confirmation prompt ───────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  CLASP PUSH — CONFIRM DEPLOYMENT"
[[ "$DRY_RUN" == "1" ]] && echo "  ── DRY RUN (no clasp push will execute) ──"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "  Project     : $TARGET"
echo "  Description : $DESC"
echo "  Directory   : $CLASP_DIR"
if [[ -n "$BUILD_DIR" ]]; then
  echo "  Layout      : multi-team overlay (shared base + team overrides)"
  echo "  Base        : $BASE_DIR"
  echo "  Build dir   : $BUILD_DIR"
fi
echo "  Script ID   : $SCRIPT_ID"
echo "  Branch      : $BRANCH  ($SHA)"
echo "  Last commit : $SUBJECT"
if [[ -n "$DIRTY" ]]; then
  echo ""
  echo "  ⚠️  WORKING TREE IS DIRTY — uncommitted changes may be pushed live."
  git status --short | sed 's/^/       /'
fi
echo ""

# ── Pre-push manifest preview ─────────────────────────────────────────────────
# Ask clasp (or count staging inputs) what this push will actually deploy.
# Catches the rootDir silent-no-op failure mode: a misconfigured .clasp.json
# pointing at a non-existent dir lets `clasp push` exit 0 with zero files
# transferred, so .push-log records "success" while nothing reaches GAS.
echo "  Files clasp will push:"
if [[ -n "$BUILD_DIR" ]]; then
  # Multi-team: BUILD_DIR is created post-confirmation (staging step), so
  # clasp status can't be queried here. Fall back to staging-input counts.
  _count_pushable() {
    find "$1" -type f \
      \( -name '*.gs' -o -name '*.js' -o -name '*.html' -o -name '*.json' \) \
      2>/dev/null | wc -l | tr -d ' '
  }
  BASE_FILES=$(_count_pushable "$REPO_ROOT/$BASE_DIR")
  OVERLAY_FILES=$(_count_pushable "$REPO_ROOT/$CLASP_DIR")
  echo "    (multi-team — exact manifest available only after staging)"
  echo "    Base files     : $BASE_FILES  (from $BASE_DIR)"
  echo "    Overlay files  : $OVERLAY_FILES  (from $CLASP_DIR)"
else
  TRACKED=$(cd "$REPO_ROOT/$CLASP_DIR" && clasp status 2>/dev/null \
            | awk '/^Tracked files:/{f=1; next} /^Untracked files:/{f=0} f && NF{print}')
  TRACKED_COUNT=$(echo "$TRACKED" | awk 'NF{c++} END{print c+0}')
  if (( TRACKED_COUNT == 0 )); then
    echo ""
    echo "  ❌ clasp sees zero tracked files in $CLASP_DIR."
    echo "     Check rootDir in $CLASP_DIR/.clasp.json — a misconfigured"
    echo "     rootDir makes clasp push silently no-op."
    echo ""
    exit 1
  fi
  echo "$TRACKED" | sed 's/^/    /'
  echo "    ──"
  echo "    Total: $TRACKED_COUNT file(s)"
fi
echo ""

if [[ "$LIVE" == "yes" ]]; then
  echo "  ┌───────────────────────────────────────────────┐"
  echo "  │  ⚠️  $TARGET is LIVE                          "
  echo "  │  Changes take effect immediately.             "
  echo "  └───────────────────────────────────────────────┘"
  echo ""
fi

if [[ "$DRY_RUN" == "1" ]]; then
  if [[ -n "$BUILD_DIR" ]]; then
    echo "  [dry-run] Would stage:   $BASE_DIR/* + $CLASP_DIR/* -> $BUILD_DIR/"
    echo "  [dry-run] Would execute: (cd $BUILD_DIR && clasp push)"
  else
    echo "  [dry-run] Would execute: (cd $CLASP_DIR && clasp push)"
  fi
  echo ""
  exit 0
fi

read -rp "  Type 'yes' to confirm push: " CONFIRM
echo ""

if [[ "$CONFIRM" != "yes" ]]; then
  echo "  Push cancelled."
  echo ""
  exit 0
fi

# ── Push ──────────────────────────────────────────────────────────────────────
echo "  Pushing $TARGET..."
echo ""
PUSH_START=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# For multi-team projects, stage shared base + team overlay into the build
# dir before pushing. Hermetic: rm -rf the build dir first so a stale leftover
# can't contaminate the next push.
if [[ -n "$BUILD_DIR" ]]; then
  echo "  Staging $BASE_DIR/ + $CLASP_DIR/ -> $BUILD_DIR/ ..."
  rm -rf "$BUILD_DIR"
  mkdir -p "$BUILD_DIR"
  cp -R "$BASE_DIR/." "$BUILD_DIR/"
  cp -R "$CLASP_DIR/." "$BUILD_DIR/"

  # Files land flat at the build-dir root after overlay, so rootDir must be
  # "." here. The team's source .clasp.json deliberately keeps a different
  # rootDir (e.g. "./src") as a foot-gun guard: a stray `clasp push` from
  # teams/<team>/ then resolves to a non-existent dir and no-ops safely
  # instead of partially deploying only Config.js. We rewrite rootDir for
  # the build dir specifically so clasp finds the staged files here.
  cat > "$BUILD_DIR/.clasp.json" <<EOF
{
  "scriptId": "$SCRIPT_ID",
  "rootDir": "."
}
EOF
fi

(cd "$PUSH_FROM" && clasp push)
PUSH_END=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo ""

# ── Append to push log (local, gitignored) ───────────────────────────────────
WHOAMI=$(git config user.email 2>/dev/null || whoami)
printf "%s\tproject=%s\tscriptId=%s\tbranch=%s\tsha=%s\tdirty=%s\tuser=%s\n" \
  "$PUSH_END" "$TARGET" "$SCRIPT_ID" "$BRANCH" "$SHA" "${DIRTY:-no}" "$WHOAMI" \
  >> "$LOG_FILE"

echo "  Done. Logged to .push-log ($PUSH_START → $PUSH_END)."
echo ""
