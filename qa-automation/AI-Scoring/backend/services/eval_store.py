"""Postgres dual-write for the scoring pipeline — Wave 2 Phase 4a.

Stage 1 (SQLMigration §3.2): when `write_draft_to_fr_ai` lands a draft
scorecard on the sheet, `record_draft_evaluation()` lands the same content
as one `qa.evaluations` row (state='draft') + bulk `qa.evaluation_sections`.

Failure semantics are §7.3 Phase A: **the Sheets path is truth and must
never notice Postgres**. Every DB failure here is logged and swallowed —
`record_draft_evaluation()` returns None instead of raising. Phase C flips
this to hard errors; that flip is a deliberate future edit, not a flag.

Idempotency mirrors the sheet: Stage 1 re-scores overwrite by dialpad_link
(§3.4.1 partial UNIQUE (team_id, dialpad_link)), so an existing draft row is
UPDATEd and its section rows replaced, in one transaction.

Stage-1 column decisions (each traces to the migration doc):

- `formula_version` / `rubric_version` stay NULL — stamped at score-compute
  time in Stage 2 post-cutover (§3.6), never at draft time.
- Manual sections with `na_applicable` land as the §3.8-point-7 NA shape
  (`binary_value='NA'`, `score_source='manual_default'` — migration 012).
  Manual sections without NA support get no Stage-1 row (the analyst fills
  them at Stage 1.5/2; the §3.5 CHECK requires a populated value).
- AI sections missing from the model output get no row (same reason).
- `dialpad_agent_id` eager resolution (§3.11) is deferred to backfill B3 —
  qa.agents is empty until then; `agent_name_raw` carries identity for now.

The pool is lazy (first write creates it) and closed by the FastAPI
lifespan teardown. No DATABASE_URL → dual-write is off, silently: local
dev and tests never need a DB (Wave2Plan Phase 1 deliverable).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Optional

from backend.models.formula import EvaluationSection, ModelInfo, ModelsUsed

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig
    from backend.models.scorecard import ScorecardWithMeta

logger = logging.getLogger(__name__)

_CONFIDENCE_MAP = {"high": "HIGH", "medium": "MED", "low": "LOW"}
_AUTO_VALUE_BINARY = {"Yes": "Y", "Y": "Y", "No": "N", "N": "N"}

_pool = None
_pool_lock: Optional[asyncio.Lock] = None


# ---------------------------------------------------------------------------
# Pure builders — everything testable without a DB
# ---------------------------------------------------------------------------

def build_draft_row(scorecard: "ScorecardWithMeta", config: "TeamConfig") -> dict[str, Any]:
    """qa.evaluations column dict for a Stage-1 draft (§3.2 row shape)."""
    models_used = ModelsUsed(
        text=ModelInfo(provider="gemini", model=scorecard.model)
    )
    duration_ms = int(scorecard.duration_ms) if scorecard.duration_ms else None
    return {
        "team_id": config.team_id,
        "agent_name_raw": scorecard.agent_name or "",
        "state": "draft",
        "source": "ai",
        "call_connected_at": scorecard.call_started_at_utc,
        "call_started_at": scorecard.call_started_at_utc,
        "call_ended_at": scorecard.call_ended_at_utc,
        "call_duration_ms": duration_ms,
        "dialpad_call_id": scorecard.call_id,
        "dialpad_link": scorecard.dialpad_link,
        "caller_name": scorecard.caller_name,
        "caller_phone": scorecard.caller_phone,
        "call_summary": scorecard.call_summary or None,
        "key_strengths": scorecard.key_strengths or None,
        "opportunities": scorecard.opportunities or None,
        "models_used": json.dumps(models_used.model_dump(exclude_none=True)),
        "ai_provider_primary": "gemini",
        "scoring_status": "flagged_long_call" if scorecard.flagged_long_call else "complete",
        "dialpad_call_metadata": json.dumps({
            "sop_used": scorecard.sop_used,
            "stage1_flags": sorted({f for s in scorecard.sections for f in s.flags}),
        }),
    }


def build_draft_sections(
    scorecard: "ScorecardWithMeta", config: "TeamConfig"
) -> list[EvaluationSection]:
    """qa.evaluation_sections rows for a Stage-1 draft, Pydantic-validated
    at the writer boundary (§3.5)."""
    ai_by_id = {s.id: s for s in scorecard.sections}
    rows: list[EvaluationSection] = []

    for sec in config.sections_by_number:
        if sec.auto_value is not None:
            rows.append(EvaluationSection(
                section_id=sec.id,
                section_number=sec.section_number,
                score_type="auto_value",
                binary_value=_AUTO_VALUE_BINARY.get(sec.auto_value, "Y"),
                score_source="auto_value",
            ))
            continue

        if sec.score_type in ("manual", "manual_yn"):
            if sec.na_applicable:
                rows.append(EvaluationSection(
                    section_id=sec.id,
                    section_number=sec.section_number,
                    score_type="manual_numeric" if sec.score_type == "manual" else "manual_binary",
                    binary_value="NA",
                    score_source="manual_default",
                ))
            # no NA support → no draft row; analyst fills at Stage 1.5/2
            continue

        ai_section = ai_by_id.get(sec.id)
        if ai_section is None:
            logger.warning("eval_store: no AI output for section %r — no draft row", sec.id)
            continue

        is_na = ai_section.yn_value == "NA"
        is_binary = sec.score_type == "yn"
        rows.append(EvaluationSection(
            section_id=sec.id,
            section_number=sec.section_number,
            score_type="binary" if is_binary else "numeric",
            numeric_score=None if is_na else (None if is_binary else ai_section.score),
            binary_value=(ai_section.yn_value if is_binary or is_na else None),
            score_source="ai",
            ai_provider="gemini",
            model=scorecard.model,
            confidence=_CONFIDENCE_MAP.get((ai_section.confidence or "").lower()),
            reasoning=ai_section.reasoning or None,
        ))

    return rows


# ---------------------------------------------------------------------------
# Dual-write entry point — never raises (§7.3 Phase A)
# ---------------------------------------------------------------------------

async def record_draft_evaluation(
    scorecard: "ScorecardWithMeta", config: "TeamConfig"
) -> Optional[int]:
    """Write the Stage-1 draft to Postgres. Returns the qa.evaluations id,
    or None when dual-write is off or anything failed (logged, swallowed)."""
    try:
        pool = await _get_pool()
        if pool is None:
            return None
        row = build_draft_row(scorecard, config)
        sections = build_draft_sections(scorecard, config)
        async with pool.acquire() as conn:
            return await _upsert_draft(conn, row, sections)
    except Exception:
        logger.exception(
            "eval_store: Stage 1 dual-write failed for team=%s call=%s — "
            "swallowed per §7.3 Phase A",
            config.team_id, scorecard.call_id,
        )
        return None


async def _upsert_draft(conn: Any, row: dict[str, Any], sections: list[EvaluationSection]) -> int:
    """INSERT the draft, or UPDATE the existing row matched by
    (team_id, dialpad_link) — the Stage-1 re-score overwrite. Section rows
    are replaced wholesale either way, in one transaction."""
    async with conn.transaction():
        evaluation_id = None
        if row["dialpad_link"]:
            evaluation_id = await conn.fetchval(
                "SELECT id FROM qa.evaluations WHERE team_id = $1 AND dialpad_link = $2",
                row["team_id"], row["dialpad_link"],
            )

        columns = list(row)
        values = [row[c] for c in columns]
        if evaluation_id is None:
            placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
            evaluation_id = await conn.fetchval(
                f"INSERT INTO qa.evaluations ({', '.join(columns)}) "
                f"VALUES ({placeholders}) RETURNING id",
                *values,
            )
        else:
            assignments = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(columns))
            await conn.execute(
                f"UPDATE qa.evaluations SET {assignments} WHERE id = $1",
                evaluation_id, *values,
            )
            await conn.execute(
                "DELETE FROM qa.evaluation_sections WHERE evaluation_id = $1",
                evaluation_id,
            )

        for section in sections:
            await conn.execute(
                "INSERT INTO qa.evaluation_sections "
                "(evaluation_id, section_id, section_number, score_type, "
                " numeric_score, binary_value, score_source, ai_provider, "
                " model, confidence, reasoning) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                evaluation_id, section.section_id, section.section_number,
                section.score_type, section.numeric_score, section.binary_value,
                section.score_source, section.ai_provider, section.model,
                section.confidence, section.reasoning,
            )
    return evaluation_id


# ---------------------------------------------------------------------------
# Pool lifecycle
# ---------------------------------------------------------------------------

async def _get_pool():
    global _pool, _pool_lock
    if _pool is not None:
        return _pool
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        return None
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    async with _pool_lock:
        if _pool is None:
            import asyncpg
            _pool = await asyncpg.create_pool(dsn, min_size=0, max_size=4, timeout=5)
    return _pool


async def close_pool() -> None:
    """Lifespan teardown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


__all__ = [
    "record_draft_evaluation",
    "build_draft_row",
    "build_draft_sections",
    "close_pool",
]
