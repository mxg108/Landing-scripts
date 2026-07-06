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
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from backend.models.formula import EvaluationSection, Formula, ModelInfo, ModelsUsed

if TYPE_CHECKING:
    from backend.config.team_config import TeamConfig
    from backend.models.scorecard import ScorecardWithMeta

logger = logging.getLogger(__name__)

_SCORING_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "scoring"

_CONFIDENCE_MAP = {"high": "HIGH", "medium": "MED", "low": "LOW"}
_AUTO_VALUE_BINARY = {"Yes": "Y", "Y": "Y", "No": "N", "N": "N"}

_pool = None
_pool_lock: Optional[asyncio.Lock] = None


# ---------------------------------------------------------------------------
# Per-team scoring truth flag (migration 013 / CutoverDesign §4)
# ---------------------------------------------------------------------------

async def get_scoring_owner(team_id: str) -> str:
    """'sheets' (legacy pipeline is truth) or 'postgres' (engine scores,
    Sheets are projections). Queried per request — the flip is a one-line
    UPDATE and must take effect without a deploy. Any failure or missing
    DB degrades to 'sheets' (the safe, legacy mode)."""
    try:
        pool = await _get_pool()
        if pool is None:
            return "sheets"
        owner = await pool.fetchval(
            "SELECT scoring_owner FROM public.teams WHERE id = $1", team_id
        )
        return owner or "sheets"
    except Exception:
        logger.exception("eval_store: scoring_owner lookup failed for %s — using 'sheets'", team_id)
        return "sheets"


# ---------------------------------------------------------------------------
# §3.14 human-review trigger (Wave 2 Phase 4e / CutoverDesign slice 1)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def _active_formula(team_id: str) -> Optional[Formula]:
    """The team's file-shipped formula (policy source for the trigger —
    §3.14: schema records the outcome, the formula records the policy).
    None when the team has no formula file yet (Sales pre-sales_v2)."""
    path = _SCORING_CONFIG_DIR / team_id / "overall_formula.json"
    if not path.exists():
        return None
    return Formula.model_validate(json.loads(path.read_text(encoding="utf-8")))


def human_review_trigger_fired(formula: Optional[Formula], sections: list[Any]) -> bool:
    """§3.14: any configured section's numeric score ≤ max_score_to_trigger.
    `sections` is any list with .id and .score (scorecard or approval
    payload). Sections without a numeric score (Y/N, NA, unscored) never
    trigger — the thresholds are rating-based by definition."""
    if formula is None or not formula.human_review_triggers:
        return False
    scores = {s.id: s.score for s in sections}
    return any(
        scores.get(t.section_id) is not None
        and scores[t.section_id] <= t.max_score_to_trigger
        for t in formula.human_review_triggers
    )


# ---------------------------------------------------------------------------
# Pure builders — everything testable without a DB
# ---------------------------------------------------------------------------

def build_draft_row(scorecard: "ScorecardWithMeta", config: "TeamConfig") -> dict[str, Any]:
    """qa.evaluations column dict for a Stage-1 draft (§3.2 row shape)."""
    models_used = ModelsUsed(
        text=ModelInfo(provider="gemini", model=scorecard.model)
    )
    duration_ms = int(scorecard.duration_ms) if scorecard.duration_ms else None
    # §3.14 gate beats long-call telemetry when both apply — the human-review
    # pause is load-bearing post-flip; flagged_long_call is informational.
    flagged_review = human_review_trigger_fired(
        _active_formula(config.team_id), scorecard.sections
    )
    if flagged_review:
        scoring_status = "flagged_human_review"
    elif scorecard.flagged_long_call:
        scoring_status = "flagged_long_call"
    else:
        scoring_status = "complete"
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
        "scoring_status": scoring_status,
        "human_review_required_at": datetime.now(timezone.utc) if flagged_review else None,
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
    scorecard: "ScorecardWithMeta", config: "TeamConfig", strict: bool = False
) -> Optional[int]:
    """Write the Stage-1 draft to Postgres. Returns the qa.evaluations id,
    or None when dual-write is off or anything failed (logged, swallowed).
    `strict` is the §7.3 Phase C mode (scoring_owner='postgres'): the DB row
    is truth, so failures raise instead."""
    try:
        pool = await _get_pool()
        if pool is None:
            if strict:
                raise RuntimeError(
                    f"scoring_owner='postgres' for {config.team_id} but no DATABASE_URL"
                )
            return None
        row = build_draft_row(scorecard, config)
        sections = build_draft_sections(scorecard, config)
        async with pool.acquire() as conn:
            return await _upsert_draft(conn, row, sections)
    except Exception:
        if strict:
            raise
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
# Stages 1.5 + 2 + 3 + 4 — the approve transition (Wave 2 Phase 4b)
# ---------------------------------------------------------------------------
#
# The /approve endpoint runs Stage 1.5 (analyst edits) through Stage 4
# (Analyst_History) in one request, so Postgres records the combined
# transition once, after Stage 4 succeeds:
#
#   - sections replaced with the analyst-approved payload; a section whose
#     value changed vs. the Stage-1 draft becomes score_source='manual'
#     (no ai_provider/confidence — §3.5 says those are AI-only); unchanged
#     sections keep their AI provenance.
#   - evaluations: evaluator_email, approved_at, key_strengths/opportunities,
#     agent_email when known, source ai→ai_reviewed if anything changed
#     (§3.2 Stage 1.5), overall_score from the Stage-3 ARRAYFORMULA readback
#     (pre-cutover truth), state='finalized' — or 'approved' when the
#     readback produced no number (the §3.4.3 CHECK requires a score to
#     finalize).
#   - formula_version/rubric_version stay NULL: the readback value is the
#     sheet's, not the engine's — stamping starts at cutover (§3.6).

def build_approval_sections(
    approved: list[Any],
    draft_sections: list[dict[str, Any]],
    config: "TeamConfig",
    model: str,
) -> tuple[list[EvaluationSection], bool]:
    """Approved payload -> section rows + whether anything changed vs draft.

    `approved` is the ApprovalRequest.sections list; `draft_sections` is the
    Stage-1 scorecard dump (job['scorecard']['sections'])."""
    approved_by_id = {s.id: s for s in approved}
    draft_by_id = {s["id"]: s for s in draft_sections}
    rows: list[EvaluationSection] = []
    any_changed = False

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

        section = approved_by_id.get(sec.id)
        if section is None:
            continue  # not in the payload -> nothing to record

        is_manual = sec.score_type in ("manual", "manual_yn")
        is_binary = sec.score_type in ("yn", "manual_yn")
        is_na = section.yn_value == "NA"

        if is_manual and is_na:
            # untouched na_default section — the Stage-1 draft never carries
            # manual sections, and NA is their default state, not an edit.
            rows.append(EvaluationSection(
                section_id=sec.id,
                section_number=sec.section_number,
                score_type="manual_numeric" if sec.score_type == "manual" else "manual_binary",
                binary_value="NA",
                score_source="manual_default",
            ))
            continue

        if is_manual:
            changed = True  # analyst actively scored a manual section
        else:
            draft = draft_by_id.get(sec.id)
            changed = draft is None or (
                (draft.get("score"), draft.get("yn_value") or None)
                != (section.score, section.yn_value or None)
            )
        any_changed = any_changed or changed

        if is_manual or changed:
            rows.append(EvaluationSection(
                section_id=sec.id,
                section_number=sec.section_number,
                score_type=(
                    ("manual_binary" if is_manual else "binary") if is_binary
                    else ("manual_numeric" if is_manual else "numeric")
                ),
                numeric_score=None if is_na else (None if is_binary else section.score),
                binary_value=(section.yn_value if is_binary or is_na else None),
                score_source="manual",
                reasoning=section.reasoning or None,
            ))
        else:
            rows.append(EvaluationSection(
                section_id=sec.id,
                section_number=sec.section_number,
                score_type="binary" if is_binary else "numeric",
                numeric_score=None if is_na else (None if is_binary else section.score),
                binary_value=(section.yn_value if is_binary or is_na else None),
                score_source="ai",
                ai_provider="gemini",
                model=model,
                confidence=_CONFIDENCE_MAP.get((section.confidence or "").lower()),
                reasoning=section.reasoning or None,
            ))

    return rows, any_changed


def _parse_overall(raw: Any):
    try:
        value = float(str(raw).strip().replace("%", ""))
    except (TypeError, ValueError):
        return None
    return round(value, 1)


def _shadow_engine_compare(
    formula: Optional[Formula],
    approved: list[Any],
    sheet_overall: Any,
    trigger_fired: bool,
    team_id: str,
) -> None:
    """Cutover shadow week (CutoverDesign §7): compute the engine score
    alongside the sheet readback and log both. Verifies compute + version
    resolution + trigger checks on live traffic — NOT that the numbers
    match (the sheet still runs the legacy formula; deltas are by design).
    Never raises, never persists."""
    if formula is None:
        return
    try:
        from backend.services.rule_engine import evaluate_formula

        by_id = {s.id: s for s in approved}
        answers: dict[str, Any] = {}
        for section in formula.sections:
            payload = by_id.get(section.key)
            if payload is None:
                logger.debug("shadow: no payload for %r — skipping compare", section.key)
                return
            if payload.yn_value:
                answers[section.key] = payload.yn_value
            elif payload.score is not None:
                answers[section.key] = payload.score
            else:
                return  # unscored slot — engine would rightly refuse
        result = evaluate_formula(formula, answers)
        logger.info(
            "shadow: team=%s engine=%.1f sheet=%s formula=%s trigger_fired=%s",
            team_id, result.final_score, sheet_overall, formula.formula_id, trigger_fired,
        )
    except Exception:
        logger.exception("shadow: engine compare failed for team=%s", team_id)


async def record_approval(
    config: "TeamConfig",
    *,
    evaluation_id: Optional[int],
    dialpad_link: Optional[str],
    evaluator_email: str,
    approved_sections: list[Any],
    draft_sections: list[dict[str, Any]],
    key_strengths: str,
    opportunities: str,
    overall_score_raw: Any,
    agent_email: Optional[str] = None,
    model: str = "gemini-2.5-flash",
    strict: bool = False,
    resolving_review: bool = False,
) -> Optional[int]:
    """Record the approve transition. Never raises (§7.3 Phase A) unless
    `strict` (Phase C). `resolving_review` marks a §3.14 step-4 resolution:
    the reviewer has assessed the flagged call, so the flag clears and
    `human_review_completed_at` is stamped even if the triggering scores
    remain in range — their judgment IS the resolution."""
    try:
        pool = await _get_pool()
        if pool is None:
            if strict:
                raise RuntimeError(
                    f"scoring_owner='postgres' for {config.team_id} but no DATABASE_URL"
                )
            return None
        sections, any_changed = build_approval_sections(
            approved_sections, draft_sections, config, model
        )
        overall = _parse_overall(overall_score_raw)
        # §3.14 recompute at approval: analyst edits can fire or clear the
        # flag — unless the reviewer is explicitly resolving it.
        formula = _active_formula(config.team_id)
        flagged_review = (
            False if resolving_review
            else human_review_trigger_fired(formula, approved_sections)
        )
        _shadow_engine_compare(
            formula, approved_sections, overall_score_raw, flagged_review, config.team_id
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                if evaluation_id is None and dialpad_link:
                    evaluation_id = await conn.fetchval(
                        "SELECT id FROM qa.evaluations WHERE team_id = $1 AND dialpad_link = $2",
                        config.team_id, dialpad_link,
                    )
                if evaluation_id is None:
                    logger.warning(
                        "eval_store: approve for team=%s link=%s has no draft row — "
                        "Stage 1 dual-write missed it; skipping",
                        config.team_id, dialpad_link,
                    )
                    return None
                # human-review timestamps must respect the 009 pair CHECK
                # (completed_at requires required_at): a §3.14 resolution
                # PRESERVES required_at (the record of when it flagged) and
                # stamps completed_at; a recompute-fire keeps/sets
                # required_at; only a recompute-clear (edits lifted the
                # scores, never reviewed) NULLs it.
                await conn.execute(
                    "UPDATE qa.evaluations SET "
                    "  evaluator_email = $2, "
                    "  agent_email = COALESCE($3, agent_email), "
                    "  key_strengths = $4, "
                    "  opportunities = $5, "
                    "  overall_score = $6, "
                    "  source = $7, "
                    "  state = $8, "
                    "  scoring_status = $9, "
                    "  human_review_required_at = CASE "
                    "    WHEN $10::boolean THEN COALESCE(human_review_required_at, NOW()) "
                    "    WHEN $11::boolean THEN human_review_required_at "
                    "    ELSE NULL END, "
                    "  human_review_completed_at = CASE "
                    "    WHEN $11::boolean THEN NOW() ELSE human_review_completed_at END, "
                    "  approved_at = NOW(), "
                    "  finalized_at = CASE WHEN $8 = 'finalized' THEN NOW() ELSE finalized_at END "
                    "WHERE id = $1",
                    evaluation_id,
                    evaluator_email,
                    agent_email,
                    key_strengths or None,
                    opportunities or None,
                    overall,
                    "ai_reviewed" if any_changed else "ai",
                    "finalized" if overall is not None else "approved",
                    "flagged_human_review" if flagged_review else "complete",
                    flagged_review,
                    resolving_review,
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
    except Exception:
        if strict:
            raise
        logger.exception(
            "eval_store: approve dual-write failed for team=%s — swallowed per §7.3 Phase A",
            config.team_id,
        )
        return None


# ---------------------------------------------------------------------------
# Post-flip finalize — engine score + version stamping (CutoverDesign §2)
# ---------------------------------------------------------------------------

async def stamp_and_finalize(
    evaluation_id: int,
    config: "TeamConfig",
    evaluator_email: str,
) -> Any:
    """The post-cutover Stage 2 (§3.6): resolve the team's active
    formula/rubric versions, stamp them on the row, compute the engine
    score, and finalize — one transaction. Always strict (Phase C): this
    only runs when scoring_owner='postgres', where the DB row is truth.

    `evaluator_email` is the analyst on a flagged-call resolution, or the
    operator who triggered scoring on the auto-flow (the §3.4.3 CHECK
    requires an evaluator + approved_at to leave 'draft').

    Returns the ScoreComputation (engine trace included) for the caller to
    surface/project."""
    from backend.services.score_compute import (
        compute_score_detail,
        get_active_versions,
    )

    pool = await _get_pool()
    if pool is None:
        raise RuntimeError(
            f"scoring_owner='postgres' for {config.team_id} but no DATABASE_URL"
        )
    async with pool.acquire() as conn:
        versions = await get_active_versions(conn, config.team_id)
        detail = await compute_score_detail(
            conn,
            evaluation_id,
            formula_version=versions.formula_version,
            rubric_version=versions.rubric_version,
        )
        async with conn.transaction():
            await conn.execute(
                "UPDATE qa.evaluations SET "
                "  overall_score = $2, "
                "  formula_version = $3, "
                "  rubric_version = $4, "
                "  evaluator_email = COALESCE(evaluator_email, $5), "
                "  state = 'finalized', "
                "  scoring_status = 'complete', "
                "  approved_at = COALESCE(approved_at, NOW()), "
                "  finalized_at = NOW() "
                "WHERE id = $1",
                evaluation_id,
                detail.overall_score,
                versions.formula_version,
                versions.rubric_version,
                evaluator_email,
            )
    logger.info(
        "eval_store: finalized eval %s under %s/%s — engine score %s",
        evaluation_id, versions.formula_version, versions.rubric_version,
        detail.overall_score,
    )
    return detail


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


async def get_pool():
    """Public accessor for co-located DB consumers (sheets_projection)."""
    return await _get_pool()


async def close_pool() -> None:
    """Lifespan teardown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


__all__ = [
    "record_draft_evaluation",
    "record_approval",
    "stamp_and_finalize",
    "get_scoring_owner",
    "get_pool",
    "build_draft_row",
    "build_draft_sections",
    "build_approval_sections",
    "human_review_trigger_fired",
    "close_pool",
]
