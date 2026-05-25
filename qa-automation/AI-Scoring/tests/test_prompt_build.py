"""Exercise scoring + progression prompt builders against each variant.

xfail-marked tests document failure points that survive into Phase 2.
They become passing tests once the relevant prompt or scoring service
is generalized.
"""

from __future__ import annotations

import pytest

from backend.config.team_config import TeamConfig
from backend.prompts.qa_scoring_prompt import (
    build_output_schema,
    build_prompt,
    build_scoring_rubric,
    build_system_prompt,
)
from backend.prompts.progression_prompt import build_progression_prompt


# ---------------------------------------------------------------------------
# Things that work today
# ---------------------------------------------------------------------------

def test_system_prompt_renders_company(config: TeamConfig):
    out = build_system_prompt(config)
    assert config.company in out


def test_system_prompt_appends_landing_general_rules(config: TeamConfig):
    """Every team's system prompt must carry the Landing-wide rule block —
    name-consistency in particular, since multiple agents may share a first
    name within a team and use different greeting names by design."""
    out = build_system_prompt(config)
    assert "GENERAL LANDING RULES" in out
    # Spot-check the load-bearing rule rather than the whole block — text
    # may shift over time but this clause must remain expressible.
    assert "consistency within the call" in out
    assert "Dialpad-recorded name is internal" in out


def test_full_prompt_audio_note_mentions_spanish_and_sop_handling(config: TeamConfig):
    p = build_prompt(
        config,
        transcript_text="Speaker A: hola",
        sop_title="",
        sop_content="",
    )
    # Audio is primary; transcript secondary.
    assert "audio is primary" in p.lower() or "transcript is secondary" in p.lower()
    # Spanish escape hatch.
    assert "SPANISH" in p
    # When no SOP is provided, the model must not mention its absence.
    assert "DO NOT mention a lack of SOP" in p


def test_scoring_rubric_includes_every_section(config: TeamConfig):
    out = build_scoring_rubric(config)
    for sec in config.sections:
        # auto_value sections never reach the prompt — writer hardcodes them
        if sec.auto_value is not None:
            continue
        assert sec.name in out, f"missing section '{sec.name}' in rubric"


def test_output_schema_includes_every_ai_scored_section(config: TeamConfig):
    out = build_output_schema(config)
    for sec in config.ai_scored_sections:
        assert f'"id": "{sec.id}"' in out, f"missing id '{sec.id}' in output schema"
    # Manual sections must NOT appear in the schema (Gemini doesn't score them).
    for sec in config.manual_sections:
        assert f'"id": "{sec.id}"' not in out
    # auto_value sections must NOT appear either — writer hardcodes them.
    for sec in config.auto_value_sections:
        assert f'"id": "{sec.id}"' not in out


def test_output_schema_score_range_uses_section_range(config: TeamConfig):
    out = build_output_schema(config)
    for sec in config.ai_scored_sections:
        if sec.score_type == "numeric":
            assert sec.score_range is not None, (
                f"numeric section {sec.id} missing score_range"
            )
            lo, hi = sec.score_range
            # Either the plain integer token or the NA-augmented token must
            # appear, depending on na_applicable.
            plain = f"<{lo}-{hi} integer>"
            na_aug = f"<{lo}-{hi} integer, or null if NA>"
            assert (plain in out) or (na_aug in out), (
                f"output schema for '{sec.id}' did not render expected "
                f"range token (plain={plain!r} or na_aug={na_aug!r})"
            )


# ---------------------------------------------------------------------------
# N/A handling — numeric + na_applicable
# ---------------------------------------------------------------------------

def test_numeric_na_applicable_section_schema_allows_na(sales: TeamConfig):
    """A numeric+na_applicable section's schema must let the model return NA.

    Sales has no numeric+na_applicable section today (all numeric sections
    are na_applicable=false). We mutate a copy of the config to exercise the
    branch and assert the prompt surfaces NA correctly.
    """
    numeric_sec = next(
        s for s in sales.ai_scored_sections if s.score_type == "numeric"
    )
    numeric_sec.na_applicable = True
    try:
        out = build_output_schema(sales)
        lo, hi = numeric_sec.score_range  # type: ignore[misc]
        # Surrounding section id ensures we're inspecting the right block.
        idx = out.find(f'"id": "{numeric_sec.id}"')
        assert idx != -1, f"section {numeric_sec.id} missing from output schema"
        block = out[idx:idx + 600]
        assert f"<{lo}-{hi} integer, or null if NA>" in block
        assert '"yn_value": "<null or NA>"' in block

        rubric = build_scoring_rubric(sales)
        # The NA note should now appear within this section's rubric block.
        r_idx = rubric.find(numeric_sec.name)
        assert r_idx != -1
        r_block = rubric[r_idx:r_idx + 1500]
        assert "Mark NA if not applicable" in r_block
    finally:
        numeric_sec.na_applicable = False


def test_numeric_non_na_section_schema_unchanged(sales: TeamConfig):
    """Regression: a numeric section with na_applicable=false must NOT mention NA."""
    numeric_sec = next(
        s for s in sales.ai_scored_sections
        if s.score_type == "numeric" and not s.na_applicable
    )
    out = build_output_schema(sales)
    idx = out.find(f'"id": "{numeric_sec.id}"')
    assert idx != -1
    block = out[idx:idx + 600]
    assert "or null if NA" not in block, (
        f"numeric section {numeric_sec.id} with na_applicable=false leaked "
        f"the NA token into the schema"
    )
    assert '"yn_value": null' in block


def test_full_prompt_assembles_without_error(config: TeamConfig):
    p = build_prompt(
        config,
        transcript_text="Speaker A: hello\nSpeaker B: hi",
        sop_title="",
        sop_content="",
        agent_name="Test Rep",
    )
    assert "No SOP context loaded" in p
    for n in config.scoring_prompt.sop_sections:
        assert str(n) in p


# ---------------------------------------------------------------------------
# Known failure points — xfail until fixed
# ---------------------------------------------------------------------------

def test_long_call_note_derives_focus_sections_from_config():
    """FAILURE POINT #1 fixed in Phase B (a): long-call note now reads
    section names from ``config.scoring_prompt.long_call_focus_sections``.
    Guard against regression of the hardcoded literal."""
    import inspect
    from backend.services import scoring_service
    src = inspect.getsource(scoring_service.score_call)
    assert "Efficiency & Call Handling" not in src, (
        "long-call note re-introduced the MS-only 'Efficiency & Call Handling' "
        "literal — derive it from config.scoring_prompt.long_call_focus_sections"
    )


@pytest.mark.xfail(
    reason=(
        "FAILURE POINT #2 — build_progression_prompt hardcodes the literal "
        "string 'Numeric sections use a 1-5 scale (higher is better)'. Lies "
        "to Gemini for any team whose score_range != [1, 5]. See "
        "backend/prompts/progression_prompt.py:73."
    ),
    strict=False,
)
def test_progression_prompt_does_not_hardcode_1_5_scale(sales_decimal: TeamConfig):
    p = build_progression_prompt(
        sales_decimal,
        agent_name="Test Rep",
        evaluations_json="[]",
        time_range_days=30,
    )
    assert "1-5 scale" not in p, (
        "progression prompt told Gemini sections use 1-5 scale, but this "
        "config uses 1-10"
    )


@pytest.mark.xfail(
    reason=(
        "FAILURE POINT #3 — build_scoring_rubric computes the cap level as "
        "capped[0].confidence_cap, silently dropping any other distinct cap. "
        "See backend/prompts/qa_scoring_prompt.py:78-86."
    ),
    strict=False,
)
def test_mixed_confidence_caps_all_appear_in_rubric(sales_decimal: TeamConfig):
    out = build_scoring_rubric(sales_decimal)
    distinct_caps = {
        s.confidence_cap for s in sales_decimal.ai_scored_sections if s.confidence_cap
    }
    assert len(distinct_caps) > 1, "fixture must have mixed caps to exercise this"
    cap_line = next(
        (line for line in out.splitlines() if "cap confidence at" in line.lower()),
        "",
    )
    assert cap_line, "no 'cap confidence at' instruction found in rubric"
    for cap in distinct_caps:
        assert cap in cap_line, (
            f"rubric cap-line did not surface '{cap}': {cap_line!r} — mixed "
            f"caps were collapsed to capped[0].confidence_cap"
        )
