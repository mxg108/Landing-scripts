#!/usr/bin/env python3
"""NightWatch tier-0 poller — single-shot, state-file-based (NightWatch.md).

One invocation blocks until the next NOTABLE event or ~9 minutes elapse,
prints exactly one JSON event line to stdout, and exits 0. The tier-1
watcher (a small-model agent) loops it and applies the decision table; all
SQL mechanics live here so the watcher never composes a query.

  python3 scripts/night_poll.py --state /tmp/nightwatch.json \
      --pull-date 2026-09-01 --baseline 2026-09-01T20:23 [--window-start 0555]

Events (one per invocation):
  idle_wait        before --window-start (UTC HHMM); quiet pre-window sleep
  heartbeat        nothing changed within the invocation budget
  sweep_row        qa_disposition_pulls status changed (pending/fetching)
  sweep_completed  pull done — carries the sweep report brief
  sweep_error      pull errored — carries the report
  evals_progress   scored count or queue depth changed (leaked delta = 0)
  leak_detail      leaked count ROSE — carries per-eval offending docs
  stall            >=15 min no progress, queue>0, nothing in flight
  stall_persistent >=75 min no progress (a pump tick passed without rescue)
  deferred_stuck   finalized evals carrying sop_skipped_reason
                   'deferred_to_trigger' (trigger-time resolution skipped)
  drained          pull completed + queue empty + stable — final summary
  query_error      sandy.py db query failed (consecutive count included)

Read-only: every statement is a SELECT."""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time

SANDY = os.path.expanduser("~/.claude/commands/scripts/sandy.py")
APP = "a2cc5b5a-df29-4ae7-9dbb-e270052015e7"
INVOCATION_BUDGET_S = 9 * 60
CHECK_EVERY_S = 75
STALL_MIN = 15
STALL_PERSISTENT_MIN = 75

ap = argparse.ArgumentParser()
ap.add_argument("--state", required=True)
ap.add_argument("--pull-date", required=True)
ap.add_argument("--baseline", required=True, help="eval created_at floor (deploy ts)")
ap.add_argument("--window-start", default="0555", help="UTC HHMM to start watching")
args = ap.parse_args()

POLL_SQL = (
    "SELECT status, report FROM qa_disposition_pulls "
    f"WHERE team_id='member_support' AND pull_date='{args.pull_date}'; "
    "SELECT COUNT(*) n, SUM(CASE WHEN dialpad_call_metadata LIKE '%system:sofia%' "
    "THEN 1 ELSE 0 END) leaked, SUM(CASE WHEN state='finalized' AND "
    "json_extract(dialpad_call_metadata,'$.sop_skipped_reason')='deferred_to_trigger' "
    "THEN 1 ELSE 0 END) deferred_stuck FROM qa_evaluations WHERE id>=10000000 "
    "AND team_id IN ('member_support','sales') AND created_at >= "
    f"'{args.baseline}' AND source IN ('ai','ai_reviewed'); "
    "SELECT SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) queued, "
    "SUM(CASE WHEN status IN ('triggering','running') THEN 1 ELSE 0 END) inflight "
    "FROM qa_score_queue"
)
LEAK_SQL = (
    "SELECT id, team_id, overall_score, "
    "json_extract(dialpad_call_metadata,'$.pulpo_docs') pd "
    "FROM qa_evaluations WHERE id>=10000000 AND team_id IN "
    f"('member_support','sales') AND created_at >= '{args.baseline}' "
    "AND dialpad_call_metadata LIKE '%system:sofia%'"
)
FINAL_SQL = (
    "SELECT team_id, COUNT(*) n, "
    "SUM(CASE WHEN dialpad_call_metadata LIKE '%system:sofia%' THEN 1 ELSE 0 END) sysofia, "
    "ROUND(AVG(overall_score),1) avg_score FROM qa_evaluations WHERE id>=10000000 "
    f"AND created_at >= '{args.baseline}' AND source IN ('ai','ai_reviewed') "
    "GROUP BY team_id; "
    "SELECT COALESCE(json_extract(dialpad_call_metadata,'$.sop_skipped_reason'),'(retrieved)') r, "
    "COUNT(*) n FROM qa_evaluations WHERE id>=10000000 AND team_id='member_support' "
    f"AND created_at >= '{args.baseline}' AND source IN ('ai','ai_reviewed') GROUP BY r"
)


def emit(event: str, **kw) -> None:
    print(json.dumps({"event": event, "at": dt.datetime.now(dt.timezone.utc)
                     .strftime("%H:%M:%SZ"), **kw}))
    sys.exit(0)


def q(sql: str):
    out = subprocess.run(["python3", SANDY, "db", "query", APP, sql],
                         capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout)[:200])
    return [b["results"] for b in json.loads(out.stdout)["data"]]


def load_state() -> dict:
    try:
        with open(args.state) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"n": -1, "leaked": 0, "pull": None, "last_change": time.time(),
                "qerr": 0, "stall_emitted": 0, "stable": 0, "deferred_emitted": 0}


def save_state(st: dict) -> None:
    with open(args.state, "w") as f:
        json.dump(st, f)


st = load_state()
deadline = time.time() + INVOCATION_BUDGET_S

now = dt.datetime.now(dt.timezone.utc)
if now.hour * 100 + now.minute < int(args.window_start):
    wake = now.replace(hour=int(args.window_start[:2]),
                       minute=int(args.window_start[2:]), second=0)
    time.sleep(min(INVOCATION_BUDGET_S, max(1, (wake - now).total_seconds())))
    emit("idle_wait", window_start=args.window_start)

while True:
    try:
        pulls, evals, queue = q(POLL_SQL)
        st["qerr"] = 0
    except Exception as e:  # noqa: BLE001
        st["qerr"] = st.get("qerr", 0) + 1
        save_state(st)
        emit("query_error", error=str(e)[:200], consecutive=st["qerr"])

    queued = (queue[0].get("queued") or 0) if queue else 0
    inflight = (queue[0].get("inflight") or 0) if queue else 0
    n = evals[0]["n"] or 0
    leaked = evals[0]["leaked"] or 0
    deferred = evals[0]["deferred_stuck"] or 0
    pull = pulls[0]["status"] if pulls else None

    if deferred and not st.get("deferred_emitted"):
        st["deferred_emitted"] = 1
        save_state(st)
        emit("deferred_stuck", finalized_with_marker=deferred)

    if leaked > st.get("leaked", 0):
        st["leaked"] = leaked
        st["last_change"] = time.time()
        save_state(st)
        detail = []
        try:
            for r in q(LEAK_SQL)[0]:
                docs = json.loads(r["pd"]) if r["pd"] else []
                bad = [f"{d.get('title','?')}{d.get('tags')}" for d in docs
                       if "system:sofia" in (d.get("tags") or [])]
                detail.append({"eval": r["id"], "team": r["team_id"],
                               "score": r["overall_score"], "docs": bad})
        except Exception as e:  # noqa: BLE001
            detail = [{"detail_error": str(e)[:150]}]
        emit("leak_detail", leaked=leaked, rows=detail)

    if pull != st.get("pull"):
        st["pull"] = pull
        st["last_change"] = time.time()
        save_state(st)
        if pull == "completed":
            rep = {}
            try:
                rep = json.loads(pulls[0]["report"] or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            keys = ("rows_in_export", "with_disposition", "agents_matched",
                    "eligible", "selected", "enqueued", "errors")
            emit("sweep_completed", report={k: rep.get(k) for k in keys},
                 scored_so_far=n, queued=queued)
        elif pull == "error":
            emit("sweep_error", report=str(pulls[0].get("report"))[:300])
        elif pull is not None:
            emit("sweep_row", status=pull)

    if n != st.get("n", -1):
        delta = n - max(st.get("n", 0), 0)
        st["n"] = n
        st["last_change"] = time.time()
        st["stall_emitted"] = 0
        st["stable"] = 0
        save_state(st)
        emit("evals_progress", total=n, delta=delta, clean=n - leaked,
             leaked=leaked, queued=queued, inflight=inflight)

    quiet_min = (time.time() - st.get("last_change", time.time())) / 60
    if pull == "completed" and queued == 0 and inflight == 0:
        st["stable"] = st.get("stable", 0) + 1
        save_state(st)
        if st["stable"] >= 2:
            try:
                teams, skips = q(FINAL_SQL)
            except Exception as e:  # noqa: BLE001
                emit("drained", summary_error=str(e)[:200])
            emit("drained",
                 teams=[{k: r[k] for k in ("team_id", "n", "sysofia", "avg_score")}
                        for r in teams],
                 ms_skip_histogram={r["r"]: r["n"] for r in skips})
    elif queued > 0 and inflight == 0 and quiet_min >= STALL_PERSISTENT_MIN:
        if st.get("stall_emitted", 0) < 2:
            st["stall_emitted"] = 2
            save_state(st)
            emit("stall_persistent", minutes=int(quiet_min), queued=queued)
    elif queued > 0 and inflight == 0 and quiet_min >= STALL_MIN:
        if st.get("stall_emitted", 0) < 1:
            st["stall_emitted"] = 1
            save_state(st)
            emit("stall", minutes=int(quiet_min), queued=queued)

    if time.time() + CHECK_EVERY_S > deadline:
        save_state(st)
        emit("heartbeat", total=n, clean=n - leaked, leaked=leaked,
             queued=queued, inflight=inflight, pull=pull,
             quiet_min=int(quiet_min))
    time.sleep(CHECK_EVERY_S)
