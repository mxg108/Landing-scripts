"""compute_overall_score — Wave 2 Phase 2b.

The DB-touching wrapper around the pure rule engine (SQLMigration.md §3.6,
§3.19.4). Loads the evaluation row, its pinned formula/rubric versions from
the immutable archives (qa.formula_versions §3.12 / qa.rubric_versions §3.19),
converts the row's qa.evaluation_sections into engine answers, and returns
the score as NUMERIC(5,1).

Reproducibility contract: the evaluation id is the only input needed — row +
both archives + section rows uniquely determine the score, forever, with no
dependency on the currently-active config. The historic-compliance sweep
(§3.13) recomputes under a *different* formula via
compute_overall_score_with_overrides(), keeping the row's rubric pinned
(§3.6: "you don't re-score under a rubric the analyst didn't actually
evaluate under") unless explicitly overridden.

READ-ONLY: this module never writes. Stamping formula_version/rubric_version
and persisting overall_score onto qa.evaluations is the Stage 2 dual-write
(Phase 4); sweep persistence into qa.formula_compliance_sweeps is Phase 6.

External signals (MS frequent_caller) are not yet persisted per-evaluation —
Command Center ships in Wave 3, and until then the signal is false-by-default
(Wave2Plan). Callers may pass live `signals`; omitting them evaluates every
signal-gated rule as not-fired, which is today's production behavior.

`conn` is duck-typed: any asyncpg-style object with fetchrow()/fetch()
(a Connection, a Pool, or a test stub).
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from backend.models.formula import (
    Formula,
    Rubric,
    validate_formula_against_rubric,
)
from backend.services.rule_engine import (
    ScoreResult,
    SectionAnswer,
    evaluate_formula,
)

_SCORE_QUANTUM = Decimal("0.1")  # qa.evaluations.overall_score is NUMERIC(5,1)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ScoreComputeError(ValueError):
    """Base for compute-path failures."""


class EvaluationNotFoundError(ScoreComputeError):
    def __init__(self, evaluation_id: int) -> None:
        self.evaluation_id = evaluation_id
        super().__init__(f"no qa.evaluations row with id={evaluation_id}")


class MissingVersionStampError(ScoreComputeError):
    """The row has no formula_version/rubric_version and no override was
    given. Pre-cutover and backfilled rows are unstamped by design (§3.4);
    score them via compute_overall_score_with_overrides()."""

    def __init__(self, evaluation_id: int, which: str) -> None:
        self.evaluation_id = evaluation_id
        super().__init__(
            f"evaluation {evaluation_id}: {which} is NULL and no override was "
            f"provided — pre-cutover rows must be scored with explicit versions"
        )


class VersionNotArchivedError(ScoreComputeError):
    """A referenced version has no archive row — the reproducibility
    invariant (§3.12/§3.19) is broken; do not fall back to file config."""

    def __init__(self, table: str, version: str) -> None:
        self.table = table
        self.version = version
        super().__init__(f"no {table} row for version {version!r}")


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

class ActiveVersions(BaseModel):
    """The currently-live version pair for a team (effective_until IS NULL)."""
    model_config = ConfigDict(extra="forbid")
    formula_version: str
    rubric_version: str


class ScoreComputation(BaseModel):
    """Full output of one compute run — what the §3.13 sweep persists and
    what a parity investigation reads. `overall_score` is the NUMERIC(5,1)
    projection of result.final_score."""
    model_config = ConfigDict(extra="forbid")

    evaluation_id: int
    team_id: str
    formula_version: str
    rubric_version: str
    overall_score: Decimal
    result: ScoreResult


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def compute_overall_score(
    conn: Any,
    evaluation_id: int,
    signals: Optional[dict[str, bool]] = None,
) -> Decimal:
    """§3.6 signature: evaluation id in, NUMERIC(5,1) out, versions read
    from the row itself."""
    detail = await compute_score_detail(conn, evaluation_id, signals=signals)
    return detail.overall_score


async def compute_overall_score_with_overrides(
    conn: Any,
    evaluation_id: int,
    formula_version: Optional[str] = None,
    rubric_version: Optional[str] = None,
    signals: Optional[dict[str, bool]] = None,
) -> Decimal:
    """§3.6 sweep escape hatch — same pure computation, explicit versions.
    Also the path for scoring rows that predate version stamping."""
    detail = await compute_score_detail(
        conn,
        evaluation_id,
        formula_version=formula_version,
        rubric_version=rubric_version,
        signals=signals,
    )
    return detail.overall_score


async def compute_score_detail(
    conn: Any,
    evaluation_id: int,
    formula_version: Optional[str] = None,
    rubric_version: Optional[str] = None,
    signals: Optional[dict[str, bool]] = None,
) -> ScoreComputation:
    """The full pipeline: fetch row + archives + sections, cross-validate
    (§3.19.3), run the engine, quantize. Overrides beat row stamps."""
    row = await conn.fetchrow(
        "SELECT id, team_id, formula_version, rubric_version "
        "FROM qa.evaluations WHERE id = $1",
        evaluation_id,
    )
    if row is None:
        raise EvaluationNotFoundError(evaluation_id)

    fv = formula_version or row["formula_version"]
    rv = rubric_version or row["rubric_version"]
    if fv is None:
        raise MissingVersionStampError(evaluation_id, "formula_version")
    if rv is None:
        raise MissingVersionStampError(evaluation_id, "rubric_version")

    formula_row = await conn.fetchrow(
        "SELECT formula_json FROM qa.formula_versions WHERE formula_version = $1",
        fv,
    )
    if formula_row is None:
        raise VersionNotArchivedError("qa.formula_versions", fv)
    rubric_row = await conn.fetchrow(
        "SELECT rubric_json FROM qa.rubric_versions WHERE rubric_version = $1",
        rv,
    )
    if rubric_row is None:
        raise VersionNotArchivedError("qa.rubric_versions", rv)

    formula = Formula.model_validate(_jsonb(formula_row["formula_json"]))
    rubric = Rubric.model_validate(_jsonb(rubric_row["rubric_json"]))
    validate_formula_against_rubric(formula, rubric)  # §3.19.3, at eval time

    section_rows = await conn.fetch(
        "SELECT section_id, numeric_score, binary_value "
        "FROM qa.evaluation_sections WHERE evaluation_id = $1",
        evaluation_id,
    )
    answers = build_answers(section_rows)

    result = evaluate_formula(formula, answers, signals=signals)
    return ScoreComputation(
        evaluation_id=evaluation_id,
        team_id=row["team_id"],
        formula_version=fv,
        rubric_version=rv,
        overall_score=_quantize(result.final_score),
        result=result,
    )


async def get_active_versions(conn: Any, team_id: str) -> ActiveVersions:
    """Resolve the live version pair for a team — what Stage 2 (Phase 4)
    stamps onto a new evaluation before computing its score."""
    formula_row = await conn.fetchrow(
        "SELECT formula_version FROM qa.formula_versions "
        "WHERE team_id = $1 AND effective_until IS NULL "
        "ORDER BY effective_from DESC LIMIT 1",
        team_id,
    )
    if formula_row is None:
        raise VersionNotArchivedError("qa.formula_versions", f"<active for {team_id}>")
    rubric_row = await conn.fetchrow(
        "SELECT rubric_version FROM qa.rubric_versions "
        "WHERE team_id = $1 AND effective_until IS NULL "
        "ORDER BY effective_from DESC LIMIT 1",
        team_id,
    )
    if rubric_row is None:
        raise VersionNotArchivedError("qa.rubric_versions", f"<active for {team_id}>")
    return ActiveVersions(
        formula_version=formula_row["formula_version"],
        rubric_version=rubric_row["rubric_version"],
    )


# ---------------------------------------------------------------------------
# Row → answer conversion
# ---------------------------------------------------------------------------

def build_answers(section_rows: Any) -> dict[str, SectionAnswer]:
    """qa.evaluation_sections rows → engine answers, keyed by section_id.

    binary_value wins when present — it carries Y/N for binary sections AND
    the 'NA' marker for unscored na_default numeric sections (migration 012 /
    §3.8 point 7). Otherwise numeric_score is the rating.

    No key remapping happens here: version pinning (§3.19.4) guarantees the
    row's sections use the same ids as its archived formula/rubric, and the
    engine's strict answer validation raises if they don't.
    """
    answers: dict[str, SectionAnswer] = {}
    for row in section_rows:
        section_id = row["section_id"]
        binary_value = row["binary_value"]
        numeric_score = row["numeric_score"]
        if binary_value is not None:
            answers[section_id] = binary_value
        elif numeric_score is not None:
            answers[section_id] = int(numeric_score)
        else:
            raise ScoreComputeError(
                f"section {section_id!r}: neither numeric_score nor "
                f"binary_value populated — violates the §3.5 CHECK"
            )
    return answers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jsonb(value: Any) -> dict:
    """asyncpg returns JSONB as str unless a codec is registered; accept both."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def _quantize(score: float) -> Decimal:
    """float → NUMERIC(5,1). Half-up, matching how the sheet displays scores
    (banker's rounding would turn 78.05 into 78.0)."""
    return Decimal(str(score)).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP)


__all__ = [
    "compute_overall_score",
    "compute_overall_score_with_overrides",
    "compute_score_detail",
    "get_active_versions",
    "build_answers",
    "ActiveVersions",
    "ScoreComputation",
    "ScoreComputeError",
    "EvaluationNotFoundError",
    "MissingVersionStampError",
    "VersionNotArchivedError",
]
