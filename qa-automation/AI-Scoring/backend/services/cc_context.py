"""Command Center call-context for /score grounding — DispositionDesign §5.

At Stage 1, after the transcript fetch: match the eval's call against
`command_center.calls` by the triple key (entry-point id first — it's
what the eval usually carries — then per-leg call id, then master), pull
the verified facts (disposition pair, AI-CSAT, hold cycles), and render
the EMPHASIZED `Call context (verified system data)` block that goes
ahead of the transcript in the scoring prompt.

Rollout gate — `CC_GROUNDING_MODE` env var, read per call:

  off      no CC lookup at all
  shadow   (default) lookup + stamp qa.evaluations, LOG the block that
           would have been injected, but leave the prompt untouched —
           the C3 shadow week's log-only compare
  on       lookup + stamp + inject

Language rule (owner, 2026-07-19 / v2.1): for Spanish calls the AUDIO is
the source of truth (Dialpad transcription is English-biased). Every
sentence referencing transcript evidence branches on the eval's
language; when the language is not yet known at prompt-build time (the
Stage-1 reality — Gemini detects it in-flight), the wording carries both
alternatives.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from backend.services.eval_store import get_pool

logger = logging.getLogger(__name__)


def grounding_mode() -> str:
    """`off` | `shadow` | `on` (unknown values collapse to `shadow`)."""
    mode = os.environ.get("CC_GROUNDING_MODE", "shadow").strip().lower()
    return mode if mode in ("off", "shadow", "on") else "shadow"


@dataclass(frozen=True)
class HoldCycle:
    started_at: datetime
    ended_at: datetime
    seconds: int


@dataclass(frozen=True)
class CallContext:
    """Verified per-call facts pulled from command_center.* (§5 step 2)."""
    cc_call_id: int
    matched_by: str                      # 'entry_point' | 'call_id' | 'master'
    disposition_category: Optional[str]
    disposition: Optional[str]
    ai_csat: Optional[float]
    total_hold_seconds: int
    connected_at: Optional[datetime]
    started_at: Optional[datetime]
    holds: list[HoldCycle] = field(default_factory=list)
    # Only webhook-observed calls carry hold truth. A stats_pull-created
    # row has total_hold_seconds=0 by schema DEFAULT — rendering that as
    # "verified: no holds" would be a FALSE verified claim (the
    # dispositions export carries no hold data).
    has_hold_truth: bool = True


_MATCH_KEYS = (
    ("entry_point", "dialpad_entry_point_call_id"),
    ("call_id", "dialpad_call_id"),
    ("master", "dialpad_master_call_id"),
)


async def fetch_call_context(
    team_id: str,
    *,
    entry_point_call_id: str = "",
    dialpad_call_id: str = "",
    master_call_id: str = "",
) -> Optional[CallContext]:
    """Triple-key match against command_center.calls; None when the CC has
    never seen the call (webhook era predates it) or no DB is configured.
    Read-only and non-fatal: any failure logs and returns None — grounding
    is an enhancement, never a scoring blocker."""
    ids = {
        "entry_point": (entry_point_call_id or "").strip(),
        "call_id": (dialpad_call_id or "").strip(),
        "master": (master_call_id or "").strip(),
    }
    try:
        pool = await get_pool()
        if pool is None:
            return None
        async with pool.acquire() as conn:
            row, matched_by = None, ""
            for key, column in _MATCH_KEYS:
                if not ids[key]:
                    continue
                row = await conn.fetchrow(
                    f"""
                    SELECT id, disposition_category, disposition, ai_csat,
                           total_hold_seconds, connected_at, started_at,
                           seen_via
                    FROM command_center.calls
                    WHERE team_id = $1 AND {column} = $2
                    """,
                    team_id, ids[key],
                )
                if row is not None:
                    matched_by = key
                    break
            if row is None:
                return None
            hold_rows = await conn.fetch(
                "SELECT started_at, ended_at, seconds "
                "FROM command_center.hold_intervals "
                "WHERE call_id = $1 ORDER BY started_at",
                row["id"],
            )
    except Exception:
        logger.exception(
            "cc_context: lookup failed for team=%s call=%s — scoring proceeds ungrounded",
            team_id, ids["call_id"] or ids["entry_point"],
        )
        return None

    return CallContext(
        cc_call_id=row["id"],
        matched_by=matched_by,
        disposition_category=row["disposition_category"],
        disposition=row["disposition"],
        ai_csat=float(row["ai_csat"]) if row["ai_csat"] is not None else None,
        total_hold_seconds=row["total_hold_seconds"] or 0,
        connected_at=row["connected_at"],
        started_at=row["started_at"],
        holds=[
            HoldCycle(r["started_at"], r["ended_at"], r["seconds"])
            for r in hold_rows
        ],
        has_hold_truth=row["seen_via"] == "webhook",
    )


# ---------------------------------------------------------------------------
# Pure rendering — the EMPHASIZED block (§5 step 3)
# ---------------------------------------------------------------------------


def _mmss(total_seconds: int) -> str:
    mins, secs = divmod(max(0, int(total_seconds)), 60)
    return f"{mins}:{secs:02d}"


def _absence_sentence(language: Optional[str]) -> str:
    if language == "es":
        return (
            "No disposition was captured for this call (back-to-back "
            "handling) — score on the AUDIO content: for Spanish calls the "
            "audio is the source of truth and the transcript is unreliable."
        )
    if language == "en":
        return (
            "No disposition was captured for this call (back-to-back "
            "handling) — score on transcript evidence alone."
        )
    return (
        "No disposition was captured for this call (back-to-back "
        "handling) — score on transcript evidence alone, OR on the audio "
        "content if the call is in Spanish (for Spanish calls the audio is "
        "the source of truth)."
    )


def build_call_context_block(
    ctx: Optional[CallContext], language: Optional[str] = None
) -> str:
    """Render the grounding block; "" when the CC never saw the call
    (nothing verified to say — the prompt stays as it is today)."""
    if ctx is None:
        return ""

    lines = [
        "",
        "=== CALL CONTEXT (VERIFIED SYSTEM DATA) ===",
        "IMPORTANT: the facts below are verified Dialpad system records for "
        "THIS call. They are ground truth — do not second-guess them from "
        "the transcript or audio.",
        "",
    ]

    if ctx.disposition_category:
        label = ctx.disposition_category + (
            f" — {ctx.disposition}" if ctx.disposition else ""
        )
        lines.append(
            f"- The agent classified this call as: {label}. Score the call "
            "within reason FOR that disposition — the expectations of each "
            "section apply as they pertain to this call type."
        )
    else:
        lines.append(f"- {_absence_sentence(language)}")

    anchor = ctx.connected_at or ctx.started_at
    if not ctx.has_hold_truth:
        # Stats-era row: no hold data exists either way — forbid
        # fabrication without asserting absence.
        lines.append(
            "- No verified hold record is available for this call. Do NOT "
            "state specific hold counts or durations as verified fact."
        )
    elif ctx.holds:
        described = []
        for hold in ctx.holds:
            duration = _mmss(hold.seconds)
            if anchor is not None:
                offset = _mmss(int((hold.started_at - anchor).total_seconds()))
                described.append(f"{duration} at {offset}")
            else:
                described.append(duration)
        n = len(ctx.holds)
        lines.append(
            f"- Verified hold record: {n} hold{'s' if n != 1 else ''} "
            f"({', '.join(described)}). Do NOT infer or report holds beyond "
            "this record."
        )
    elif ctx.total_hold_seconds > 0:
        # Backfill-era rows can carry the rollup without per-cycle detail.
        lines.append(
            f"- Verified total hold time: {_mmss(ctx.total_hold_seconds)}. "
            "Do NOT infer additional holds beyond this total."
        )
    else:
        lines.append(
            "- Verified: no holds occurred on this call. Do NOT report or "
            "penalize hold time."
        )

    if ctx.ai_csat is not None:
        lines.append(
            f"- Dialpad Ai CSAT estimate for this call: {ctx.ai_csat:g}/5 "
            "(context only — do not let it anchor your section scores)."
        )

    lines.append("")
    return "\n".join(lines)
