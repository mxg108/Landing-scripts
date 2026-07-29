"""Shared pytest fixtures + synthetic data factories for the test harness.

Test configs live under ``tests/fixtures/teams/`` and are loaded via the
production Pydantic model. ``load_test_config`` falls through to the
production ``backend/config/teams/`` directory when a name doesn't match
a fixture — so production configs (``sales``, ``member_support``) can
be exercised by the same test surface without copying them into
fixtures.

Schema v2.0 (Phase 2): Analyst_History layout is derived from
``len(sections)`` via ``backend.config.history_layout.HistoryLayout``;
no per-team layout fields exist on the config object anymore.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# Make `backend.*` importable when running pytest from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Make `database.*` (top-level repo package) importable — same
# walk-up-to-repo-root pattern tests/integration/conftest.py uses for
# `database.runner`. (`command_center.*` no longer needs this: it lives
# inside the app root since 2026-07-29 and rides the _REPO_ROOT insert.)
_p = _REPO_ROOT
while _p.parent != _p and not (_p / "database" / "migrations").is_dir():
    _p = _p.parent
if str(_p) not in sys.path:
    sys.path.insert(0, str(_p))

from backend.config import history_layout  # noqa: E402
from backend.config.team_config import TeamConfig, _assemble_rubric_block  # noqa: E402
from backend.services import eval_store  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch):
    """The unit suite must NEVER reach a real Postgres.

    backend.main's load_dotenv puts the production DATABASE_URL into the
    environment at import time; with the cutover's per-request
    scoring_owner lookup, an unguarded route test would silently read the
    LIVE Railway flag and branch on production state (observed 2026-07-05:
    test_approve_writes_audit_row took the postgres path after the MS
    flip). Every test starts with no DSN and no cached pool; tests that
    want a DB monkeypatch eval_store._get_pool with a fake.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(eval_store, "_pool", None)


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "teams"
_PROD_CONFIG_DIR = _REPO_ROOT / "backend" / "config" / "teams"

TEST_CONFIG_NAMES = ["sales", "sales_lite", "sales_decimal"]


def load_test_config(name: str) -> TeamConfig:
    """Load a config straight through TeamConfig (bypasses lru_cache).

    Looks under ``tests/fixtures/teams/`` first, falls through to
    production ``backend/config/teams/``. The fall-through lets tests
    name production teams (``sales``, ``member_support``) without
    duplicating them as fixtures.
    """
    fixture_path = _FIXTURE_DIR / f"{name}.json"
    if fixture_path.exists():
        raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    else:
        prod_path = _PROD_CONFIG_DIR / f"{name}.json"
        if not prod_path.exists():
            raise FileNotFoundError(
                f"No config '{name}' under {_FIXTURE_DIR} or {_PROD_CONFIG_DIR}"
            )
        raw = json.loads(prod_path.read_text(encoding="utf-8"))
    raw["rubric"] = _assemble_rubric_block(raw)
    for legacy_key in ("rubric_version", "sections", "scoring_prompt"):
        raw.pop(legacy_key, None)
    return TeamConfig(**raw)


@pytest.fixture(params=TEST_CONFIG_NAMES)
def config(request) -> TeamConfig:
    """Parametrized fixture — every test that uses it runs once per variant."""
    return load_test_config(request.param)


@pytest.fixture
def sales() -> TeamConfig:
    """Real Sales config from backend/config/teams/sales.json."""
    return load_test_config("sales")


@pytest.fixture
def member_support() -> TeamConfig:
    """Real Member Support config from backend/config/teams/member_support.json."""
    return load_test_config("member_support")


@pytest.fixture
def sales_lite() -> TeamConfig:
    return load_test_config("sales_lite")


@pytest.fixture
def sales_decimal() -> TeamConfig:
    return load_test_config("sales_decimal")


# ---------------------------------------------------------------------------
# Synthetic Gemini scoring response
# ---------------------------------------------------------------------------

def make_gemini_scoring_json(config: TeamConfig, *, score_seed: int = 4) -> dict[str, Any]:
    """Return a dict shaped like ``build_output_schema()`` expects from Gemini.

    Numeric sections get ``score_seed`` (clamped to range), Y/N sections get
    "Y", manual sections are omitted (analyst-filled), ``auto_value`` sections
    are also omitted (writer hardcodes them).
    """
    sections = []
    for sec in config.ai_scored_sections:
        if sec.score_type == "numeric":
            assert sec.score_range is not None, f"numeric section {sec.id} missing score_range"
            lo, hi = sec.score_range
            score = max(lo, min(hi, score_seed))
            sections.append({
                "id": sec.id,
                "name": sec.name,
                "score": score,
                "score_type": "numeric",
                "yn_value": None,
                "confidence": "high",
                "reasoning": f"synthetic reasoning for {sec.id}",
                "audio_dependent": sec.audio_dependent,
                "flags": [],
            })
        else:  # yn
            sections.append({
                "id": sec.id,
                "name": sec.name,
                "score": None,
                "score_type": "yn",
                "yn_value": "Y",
                "confidence": "high",
                "reasoning": f"synthetic reasoning for {sec.id}",
                "audio_dependent": sec.audio_dependent,
                "flags": [],
            })
    return {
        "sections": sections,
        "call_summary": "Synthetic call summary.",
        "key_strengths": "Synthetic strengths.",
        "opportunities": "Synthetic opportunities.",
    }


# ---------------------------------------------------------------------------
# Synthetic Analyst_History rows (new derived layout)
# ---------------------------------------------------------------------------

def _empty_row(width: int) -> list[str]:
    return [""] * width


def make_history_row(
    config: TeamConfig,
    *,
    agent: str,
    when: datetime,
    overall: float,
    section_scores: dict[str, Any] | None = None,
    yn_scores: dict[str, str] | None = None,
    evaluator_email: str = "evaluator@landing.com",
    dialpad_link: str = "",
    source: str = "ai",
    call_started: datetime | None = None,
) -> list[str]:
    """Build one Analyst_History row matching the derived layout.

    `when` is always the eval/approval time the caller wants to assert
    against. The new-shape / old-shape switch is controlled by
    `call_started`:

      * ``call_started=None`` (default) → **old-shape row**: col C
        holds `when` (eval time), `col_eval_approved_at` is left blank.
        Exercises `load_and_clean`'s fallback path. Matches every
        synthetic row that existed before the call-time initiative.

      * ``call_started=<datetime>`` → **new-shape row**: col C holds
        the call's connected time, `col_eval_approved_at` holds the
        eval time (`when`). Matches what `finalize_to_analyst_history`
        writes after the Phase 1 schema shift.

    Reasoning and confidence cells are left blank — production rows
    fill them post-approval.
    """
    L = config.history_layout
    row = _empty_row(L.total_width)

    # Prefix
    row[history_layout.COL_AGENT_NAME] = agent
    row[history_layout.COL_AGENT_EMAIL] = f"{agent.lower().replace(' ', '.')}@landing.com"
    if call_started is None:
        # Old-shape: col C is the eval time.
        row[history_layout.COL_TIMESTAMP] = when.strftime("%m/%d/%Y %H:%M:%S")
    else:
        # New-shape: col C is the call's connected time; eval time
        # lands in the new trailing column below.
        row[history_layout.COL_TIMESTAMP] = call_started.strftime("%m/%d/%Y %H:%M:%S")
    row[history_layout.COL_EVALUATOR_EMAIL] = evaluator_email
    row[history_layout.COL_DIALPAD_LINK] = (
        dialpad_link
        or f"https://dialpad.com/callhistory/callreview/{abs(hash((agent, when))) % 10**16}"
    )
    row[history_layout.COL_OVERALL_SCORE] = str(overall)

    # Section scores in canonical order
    section_scores = section_scores or {}
    yn_scores = yn_scores or {}
    for i, sec in enumerate(config.sections_by_number):
        col = L.col_score(i)
        if sec.auto_value is not None:
            row[col] = sec.auto_value
        elif sec.score_type == "yn":
            row[col] = yn_scores.get(sec.history_id, "Y")
        else:
            # numeric or manual
            if sec.history_id in section_scores:
                row[col] = str(section_scores[sec.history_id])
            elif sec.score_range:
                mid = (sec.score_range[0] + sec.score_range[1]) // 2
                row[col] = str(mid)
            else:
                row[col] = "3"

    # Trailing
    row[L.col_key_strengths] = "syn-strengths"
    row[L.col_opportunities] = "syn-improvements"
    row[L.col_call_summary] = "syn summary"
    row[L.col_caller_name] = "Syn Caller"
    row[L.col_caller_phone] = "+15555550100"
    row[L.col_source] = source
    if call_started is not None:
        row[L.col_eval_approved_at] = when.strftime("%m/%d/%Y %H:%M:%S")

    return row


def make_history_sheet(config: TeamConfig) -> list[list[str]]:
    """Generate a header + ~30 rows of synthetic history.

    Same agent/pattern mix as before:
      * Star Rep — high & consistent, 1 intentional 60 outlier
      * Decline Rep — drifts from ~90s to ~60s
      * Improve Rep — climbs from ~60s to ~90s
      * Steady Rep — flat 78-82
      * Junior Rep — bottom score on first numeric section
      * Test Agent — overall=0 (excluded only when in excluded_test_agents)
    """
    L = config.history_layout
    header = _empty_row(L.total_width)
    header[history_layout.COL_AGENT_NAME] = "Agent Name"
    rows: list[list[str]] = [header]

    base = datetime(2026, 4, 1, 9, 0, 0)
    numeric_ids = [s.history_id for s in config.numeric_sections]
    yn_ids = config.yn_history_ids

    def score_dict(numeric_val: float) -> dict[str, float]:
        """Map a 0-100 'overall' into per-section scores in each section's range."""
        out: dict[str, float] = {}
        for hid in numeric_ids:
            sec = config.history_id_to_section.get(hid)
            if sec and sec.score_range:
                lo, hi = sec.score_range
                v = lo + (hi - lo) * (numeric_val / 100.0)
                out[hid] = round(max(lo, min(hi, v)), 1)
            else:
                out[hid] = round(numeric_val / 20.0, 1)
        return out

    # Star Rep — strong & consistent, with one bad call
    for i in range(7):
        overall = 96 + (i % 3)
        if i == 4:
            overall = 60  # intentional outlier
        rows.append(make_history_row(
            config, agent="Star Rep", when=base + timedelta(days=i * 2),
            overall=overall, section_scores=score_dict(overall),
            yn_scores={y: "Y" for y in yn_ids},
        ))

    # Decline Rep — high to low
    for i, overall in enumerate([92, 90, 87, 80, 72, 65, 58]):
        rows.append(make_history_row(
            config, agent="Decline Rep", when=base + timedelta(days=i * 2 + 1),
            overall=overall, section_scores=score_dict(overall),
            yn_scores={y: ("Y" if i < 4 else "N") for y in yn_ids},
        ))

    # Improve Rep — low to high
    for i, overall in enumerate([62, 68, 74, 80, 85, 90, 94]):
        rows.append(make_history_row(
            config, agent="Improve Rep", when=base + timedelta(days=i * 2),
            overall=overall, section_scores=score_dict(overall),
            yn_scores={y: ("N" if i < 3 else "Y") for y in yn_ids},
        ))

    # Steady Rep — flat
    for i in range(6):
        overall = 78 + (i % 5)
        rows.append(make_history_row(
            config, agent="Steady Rep", when=base + timedelta(days=i * 2),
            overall=overall, section_scores=score_dict(overall),
            yn_scores={y: "Y" for y in yn_ids},
        ))

    # Junior Rep — low scores on first numeric section
    for i in range(6):
        overall = 72 + (i % 4)
        scores = score_dict(overall)
        if numeric_ids:
            sec = config.history_id_to_section.get(numeric_ids[0])
            if sec and sec.score_range:
                scores[numeric_ids[0]] = sec.score_range[0]  # rock-bottom
        rows.append(make_history_row(
            config, agent="Junior Rep", when=base + timedelta(days=i * 2 + 1),
            overall=overall, section_scores=scores,
            yn_scores={y: "Y" for y in yn_ids},
        ))

    # Test Agent — overall=0 sentinel; excluded only when in excluded_test_agents
    rows.append(make_history_row(
        config, agent="Test Agent", when=base + timedelta(days=20),
        overall=0, section_scores=score_dict(0),
        yn_scores={y: "NA" for y in yn_ids},
    ))

    return rows


def make_mails_sheet(active: list[str], inactive: list[str] | None = None) -> list[list[str]]:
    """Generate a Mails-tab fixture.

    Cols: A=name, B=email, C=supervisor, D=canonical name (often blank).
    """
    header = ["Agent Name", "Email", "Supervisor", "Canonical Name"]
    rows = [header]
    for name in active:
        rows.append([
            name,
            f"{name.lower().replace(' ', '.')}@landing.com",
            "Sup A" if "Star" in name or "Steady" in name else "Sup B",
            "",
        ])
    for name in inactive or []:
        rows.append([name, "", "", ""])  # name only — won't be in active set
    return rows
