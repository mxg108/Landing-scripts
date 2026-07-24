"""Sanity: every test fixture loads through the production Pydantic schema.

Phase 2 schema (v2.0): the ad-hoc ``analyst_history.section_columns`` /
``yn_columns`` / ``extended_columns`` dicts are gone — Analyst_History
layout is derived from ``len(sections)`` via
``backend.config.history_layout.HistoryLayout``. The only hardcoded
column letters live in ``score_destination.section_score_columns`` and
``score_destination.metadata_cols``.
"""

from __future__ import annotations

from backend.config.team_config import TeamConfig, get_all_team_ids


def test_config_loads_and_exposes_derived_props(config: TeamConfig):
    """All variants validate cleanly and the derived properties are
    consistent with the raw section list.

    Four score_types exist today: ``numeric`` and ``yn`` (AI-scored),
    plus ``manual`` and ``manual_yn`` (analyst-entered 1-5 score / Y-N
    value, respectively). ``auto_value`` is an orthogonal axis — any
    score_type can have a hardcoded value the writer injects.
    """
    assert len(config.sections) >= 1
    assert config.team_id

    n_numeric = sum(1 for s in config.sections if s.score_type == "numeric")
    n_yn = sum(1 for s in config.sections if s.score_type == "yn")
    n_manual = sum(1 for s in config.sections if s.score_type == "manual")
    n_manual_yn = sum(1 for s in config.sections if s.score_type == "manual_yn")
    n_auto_value = sum(1 for s in config.sections if s.auto_value is not None)
    assert n_numeric + n_yn + n_manual + n_manual_yn == len(config.sections)

    # Rollup partitions (see TeamConfig section-partition properties):
    #   numeric_sections = numeric + manual         (any 1-5 stored value)
    #   yn_sections      = yn      + manual_yn      (any Y/N stored value)
    #   manual_sections  = manual  + manual_yn      (any analyst-entered value)
    # ai_scored_sections excludes both manual variants AND auto_value (Gemini
    # sees neither — manual is analyst-filled, auto_value is writer-hardcoded).
    assert len(config.numeric_sections) == n_numeric + n_manual
    assert len(config.yn_sections) == n_yn + n_manual_yn
    assert len(config.manual_sections) == n_manual + n_manual_yn
    assert len(config.auto_value_sections) == n_auto_value
    assert len(config.ai_scored_sections) == (n_numeric + n_yn) - n_auto_value

    # history_id keys roundtrip.
    assert set(config.history_id_to_section.keys()) == {
        s.history_id for s in config.sections
    }

    # progression_sections excludes auto_value (no signal in tracking a constant).
    assert len(config.progression_sections) == len(config.sections) - n_auto_value


def test_score_destination_columns_reference_real_sections(config: TeamConfig):
    """Every value in score_destination.section_score_columns must be a
    real section_id from this team's sections list."""
    sd = config.sheets.score_destination
    known_ids = {s.id for s in config.sections}
    for letter, sec_id in sd.section_score_columns.items():
        assert sec_id in known_ids, (
            f"score_destination.section_score_columns['{letter}'] "
            f"references unknown section_id '{sec_id}'"
        )


def test_history_layout_width_matches_formula(config: TeamConfig):
    """Derived layout width follows the canonical 6 + 3N + 10 formula.

    Trailing width grew 6 → 7 with the call-time initiative
    (`col_eval_approved_at`, references/CallTimeOnAnalystHistory.md) and
    7 → 10 with the call-metadata columns (disposition, ai_csat,
    sop_references — DispositionDesign §5 / PulpoConnection §4.2).
    """
    L = config.history_layout
    n = len(config.sections)
    assert L.total_width == 6 + 3 * n + 10
    # Range invariants: scores → reasoning → confidence are contiguous, each width N
    assert L.scores_end - L.scores_start == n
    assert L.reasoning_end - L.reasoning_start == n
    assert L.confidence_end - L.confidence_start == n
    assert L.scores_end == L.reasoning_start
    assert L.reasoning_end == L.confidence_start
    # Trailing fixed cells immediately follow confidence range
    assert L.col_key_strengths == L.confidence_end
    assert L.col_source == L.confidence_end + 5
    assert L.col_eval_approved_at == L.confidence_end + 6
    # Call-metadata trailing columns (append-only — positions of every
    # earlier cell must never shift)
    assert L.col_disposition == L.confidence_end + 7
    assert L.col_ai_csat == L.confidence_end + 8
    assert L.col_sop_references == L.confidence_end + 9
    # sop_references is the LAST trailing column — total_width math
    # would silently break if a future trailing cell landed without
    # bumping TRAILING_WIDTH here.
    assert L.col_sop_references == L.total_width - 1


def test_history_layout_eval_approved_at_does_not_collide_with_existing_trailing_cells(
    config: TeamConfig,
):
    """Regression guard: col_eval_approved_at must not overlap any of the
    six prior trailing cells. Catches an accidental re-numbering."""
    L = config.history_layout
    prior_trailing = {
        L.col_key_strengths,
        L.col_opportunities,
        L.col_call_summary,
        L.col_caller_name,
        L.col_caller_phone,
        L.col_source,
    }
    assert L.col_eval_approved_at not in prior_trailing


def test_history_layout_eval_approved_at_indices_at_n_boundaries():
    """N=1 (smallest legal), N=10 (member_support shape), N=19 (sales
    shape). Asserts col_eval_approved_at lands at the documented offset
    relative to confidence_end for each."""
    from backend.config.history_layout import HistoryLayout

    for n in (1, 10, 19):
        L = HistoryLayout(n)
        # confidence_end = 6 + 3n; col_eval_approved_at = confidence_end + 6
        expected = 6 + 3 * n + 6
        assert L.col_eval_approved_at == expected, (
            f"N={n}: col_eval_approved_at expected at index {expected}, "
            f"got {L.col_eval_approved_at}"
        )
        # Three call-metadata columns follow it; sop_references is last.
        assert L.total_width == L.col_sop_references + 1


def test_section_numbers_are_unique(config: TeamConfig):
    """section_number is the stable identifier — must be unique per team
    so the derived layout is deterministic."""
    numbers = [s.section_number for s in config.sections]
    assert len(numbers) == len(set(numbers)), (
        f"duplicate section_numbers in {config.team_id}: {sorted(numbers)}"
    )


def test_score_destination_readback_col_outside_section_range(config: TeamConfig):
    """The readback col must not collide with any section score column —
    otherwise the formula would overwrite a section value."""
    sd = config.sheets.score_destination
    section_letters = set(sd.section_score_columns.keys())
    assert sd.score_readback_col not in section_letters, (
        f"{config.team_id}: score_readback_col '{sd.score_readback_col}' "
        f"collides with a section_score_columns letter"
    )


def test_get_all_team_ids_returns_configured_teams():
    """Discovery helper used by resolve_team_for_agent must list every
    team whose JSON config exists in backend/config/teams/."""
    ids = get_all_team_ids()
    assert "member_support" in ids
    assert "sales" in ids
    assert ids == sorted(ids)
