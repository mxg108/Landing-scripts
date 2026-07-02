"""Formula/rubric version shipping — Wave 2 Phase 3a.

Implements the two archive write paths:

- **Formulas (§3.12, file-source-of-truth):** at FastAPI startup,
  `ship_active_formulas()` walks `config/scoring/<team>/overall_formula.json`,
  validates each against its archived rubric, and hash-compares against
  `qa.formula_versions`. New version → INSERT + close the team's prior active
  row, one transaction. "Dropping a revised JSON into config and restarting
  FastAPI is the entire ship-a-new-formula-version ceremony."
- **Rubrics (§3.19.2, DB-as-source):** `ship_rubric()` is the programmatic
  write path (CLI: `python -m backend.scripts.ship_rubric`). Validates the
  §3.19.3 invariant — the new rubric must cover every section the team's
  *active formula* references — then INSERT + close prior, one transaction.

Immutability guard (both paths): a version id that already exists in the
archive with DIFFERENT content raises `ImmutableVersionError`. Shipped
versions are never edited in place — bump the version id instead. Content
comparison is canonical (parse through the Pydantic model, dump sorted), so
whitespace/key-order noise never trips it.

First-ship runbook for a team (order matters — the formula references its
rubric_version):

    1. python -m backend.scripts.ship_rubric --team <team>
    2. restart FastAPI (Railway deploy) → formula ships on startup

Startup failure semantics: file/model validation errors always raise (§3.8:
formula bugs become startup failures — they're catchable without a DB). DB
ship errors are logged and skipped by default so a missing DATABASE_URL or
an unshipped rubric never blocks boot; set QA_VERSION_SHIP_STRICT=1 to make
them fatal once the ceremony is part of normal deploys.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from backend.models.formula import (
    Formula,
    Rubric,
    validate_formula_against_rubric,
)

logger = logging.getLogger(__name__)

_SCORING_DIR = Path(__file__).resolve().parent.parent / "config" / "scoring"
_ACTIVE_FORMULA_FILENAME = "overall_formula.json"


class VersionShipError(ValueError):
    """Base for archive-write failures."""


class ImmutableVersionError(VersionShipError):
    """A version id already archived with different content — §3.12/§3.8
    point 5: the archive is load-bearing; bump the version, never edit."""

    def __init__(self, table: str, version: str) -> None:
        super().__init__(
            f"{table} already holds {version!r} with different content — "
            f"shipped versions are immutable; bump the version id"
        )


class RubricNotArchivedError(VersionShipError):
    """The formula references a rubric_version with no archive row.
    Run `python -m backend.scripts.ship_rubric` first (see module runbook)."""

    def __init__(self, formula_id: str, rubric_version: str) -> None:
        super().__init__(
            f"formula {formula_id!r} references rubric {rubric_version!r} "
            f"which is not in qa.rubric_versions — ship the rubric first"
        )


class ShipOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_id: str
    version: str
    table: str
    action: str  # "inserted" | "unchanged"


# ---------------------------------------------------------------------------
# Canonical content hashing
# ---------------------------------------------------------------------------

def _canonical_json(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(by_alias=True, mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )


def _content_hash(model: BaseModel) -> str:
    return hashlib.sha256(_canonical_json(model).encode()).hexdigest()


def _jsonb(value: Any) -> dict:
    return json.loads(value) if isinstance(value, str) else value


# ---------------------------------------------------------------------------
# Formula ship (§3.12) — file source of truth, DB archive
# ---------------------------------------------------------------------------

async def ship_formula(conn: Any, team_id: str, formula: Formula) -> ShipOutcome:
    existing = await conn.fetchrow(
        "SELECT formula_json FROM qa.formula_versions WHERE formula_version = $1",
        formula.formula_id,
    )
    if existing is not None:
        archived = Formula.model_validate(_jsonb(existing["formula_json"]))
        if _content_hash(archived) == _content_hash(formula):
            return ShipOutcome(team_id=team_id, version=formula.formula_id,
                               table="qa.formula_versions", action="unchanged")
        raise ImmutableVersionError("qa.formula_versions", formula.formula_id)

    rubric_row = await conn.fetchrow(
        "SELECT rubric_json FROM qa.rubric_versions WHERE rubric_version = $1",
        formula.rubric_version,
    )
    if rubric_row is None:
        raise RubricNotArchivedError(formula.formula_id, formula.rubric_version)
    validate_formula_against_rubric(formula, Rubric.model_validate(_jsonb(rubric_row["rubric_json"])))

    async with conn.transaction():
        await conn.execute(
            "UPDATE qa.formula_versions SET effective_until = NOW() "
            "WHERE team_id = $1 AND effective_until IS NULL",
            team_id,
        )
        await conn.execute(
            "INSERT INTO qa.formula_versions "
            "(formula_version, team_id, formula_json, effective_from) "
            "VALUES ($1, $2, $3::jsonb, NOW())",
            formula.formula_id, team_id, _canonical_json(formula),
        )
    return ShipOutcome(team_id=team_id, version=formula.formula_id,
                       table="qa.formula_versions", action="inserted")


def load_active_formula_files(scoring_dir: Optional[Path] = None) -> dict[str, Formula]:
    """Parse every team's overall_formula.json. Pure validation — raises on
    malformed content even when no DB is configured (§3.8 point 3). Ignores
    sibling files like v0_sheet.json (historic archives ship via backfill B1,
    never via startup — they must not close the active version)."""
    scoring_dir = scoring_dir or _SCORING_DIR
    formulas: dict[str, Formula] = {}
    for team_dir in sorted(p for p in scoring_dir.iterdir() if p.is_dir()):
        path = team_dir / _ACTIVE_FORMULA_FILENAME
        if not path.exists():
            continue  # Sales until §10 sign-off lands its file
        formulas[team_dir.name] = Formula.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
    return formulas


async def ship_active_formulas(conn: Any, scoring_dir: Optional[Path] = None) -> list[ShipOutcome]:
    return [
        await ship_formula(conn, team_id, formula)
        for team_id, formula in load_active_formula_files(scoring_dir).items()
    ]


# ---------------------------------------------------------------------------
# Rubric ship (§3.19.2 programmatic path) — DB source of truth
# ---------------------------------------------------------------------------

async def ship_rubric(conn: Any, team_id: str, rubric: Rubric) -> ShipOutcome:
    existing = await conn.fetchrow(
        "SELECT rubric_json FROM qa.rubric_versions WHERE rubric_version = $1",
        rubric.rubric_version,
    )
    if existing is not None:
        archived = Rubric.model_validate(_jsonb(existing["rubric_json"]))
        if _content_hash(archived) == _content_hash(rubric):
            return ShipOutcome(team_id=team_id, version=rubric.rubric_version,
                               table="qa.rubric_versions", action="unchanged")
        raise ImmutableVersionError("qa.rubric_versions", rubric.rubric_version)

    # §3.19.3 — the team's ACTIVE formula must survive this rubric. On a
    # team's first ship there is no active formula and the check is vacuous.
    active = await conn.fetchrow(
        "SELECT formula_json FROM qa.formula_versions "
        "WHERE team_id = $1 AND effective_until IS NULL "
        "ORDER BY effective_from DESC LIMIT 1",
        team_id,
    )
    if active is not None:
        validate_formula_against_rubric(
            Formula.model_validate(_jsonb(active["formula_json"])), rubric
        )

    async with conn.transaction():
        await conn.execute(
            "UPDATE qa.rubric_versions SET effective_until = NOW() "
            "WHERE team_id = $1 AND effective_until IS NULL",
            team_id,
        )
        await conn.execute(
            "INSERT INTO qa.rubric_versions "
            "(rubric_version, team_id, rubric_json, effective_from) "
            "VALUES ($1, $2, $3::jsonb, NOW())",
            rubric.rubric_version, team_id, _canonical_json(rubric),
        )
    return ShipOutcome(team_id=team_id, version=rubric.rubric_version,
                       table="qa.rubric_versions", action="inserted")


# ---------------------------------------------------------------------------
# Startup hook
# ---------------------------------------------------------------------------

async def run_startup_ship(database_url: Optional[str], strict: bool = False) -> list[ShipOutcome]:
    """Called from the FastAPI lifespan. File validation always runs and
    always raises on bad content; the DB leg degrades to a logged error
    unless `strict` (see module docstring)."""
    formulas = load_active_formula_files()  # raises on malformed JSON — §3.8

    if not database_url:
        logger.warning("version ship: DATABASE_URL not set — validated %d formula file(s), skipping archive", len(formulas))
        return []

    import asyncpg

    try:
        conn = await asyncpg.connect(database_url, timeout=5)
    except Exception:
        if strict:
            raise
        logger.exception("version ship: could not connect — skipping archive")
        return []
    try:
        outcomes = []
        for team_id, formula in formulas.items():
            try:
                outcome = await ship_formula(conn, team_id, formula)
            except VersionShipError:
                if strict:
                    raise
                logger.exception("version ship: %s failed — skipping", team_id)
                continue
            logger.info("version ship: %s %s (%s)", outcome.version, outcome.action, team_id)
            outcomes.append(outcome)
        return outcomes
    finally:
        await conn.close()


__all__ = [
    "ship_formula",
    "ship_rubric",
    "ship_active_formulas",
    "load_active_formula_files",
    "run_startup_ship",
    "ShipOutcome",
    "VersionShipError",
    "ImmutableVersionError",
    "RubricNotArchivedError",
]
