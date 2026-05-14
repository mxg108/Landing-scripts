"""Team configuration model and loader.

Each team's rubric, column layout, and Gemini config live in a JSON file
at backend/config/teams/{team_id}.json.

Schema v2.0 (Phase 2): Analyst_History and Form Responses AI column
layouts are derived by formula from len(sections) — see
backend.config.history_layout. Per-team config only declares tab names
and the per-team `score_destination` block (the legacy form layouts
that ARRAYFORMULAs and Apps Script flows depend on).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from backend.config.history_layout import HistoryLayout


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class GeminiConfig(BaseModel):
    """Gemini model settings for scoring and progression."""
    scoring_model: str
    scoring_temperature: float
    scoring_max_output_tokens: int
    progression_model: str
    progression_temperature: float
    progression_max_output_tokens: int


class FormResponsesAIConfig(BaseModel):
    """Form Responses AI tab — drafts of AI-generated scorecards.

    Layout is derived from len(sections); only the tab name is configured.
    """
    tab_name: str


class AnalystHistoryConfig(BaseModel):
    """Analyst_History tab — finalized evaluations.

    Layout is derived from len(sections); only tab names are configured.
    `tab_name_legacy` is set during the Phase 2 cutover so the migration
    script knows where to read pre-refactor rows from. Sales has no
    legacy tab → field stays null.
    """
    tab_name: str
    tab_name_legacy: Optional[str] = None


class WeightsReferenceConfig(BaseModel):
    """Reference to a per-team weights tab used by the score formula.

    Sheet-only — Python neither reads nor writes these. Recorded for
    documentation and future tooling that may want to inspect or
    rebalance weights from one place.
    """
    tab_name: str
    range: str
    comment: Optional[str] = None


class ScoreDestinationConfig(BaseModel):
    """Per-team score-destination tab where approved scores land and the
    overall-score formula calculates.

    The only place in the system with hardcoded column letters — mirrors
    the legacy form layouts that pre-existing ARRAYFORMULAs and Apps
    Script flows depend on. Pipeline Stage 2 writes section scores here;
    Stage 3 reads back from `score_readback_col`.
    """
    tab_name: str

    section_score_columns: dict[str, str]
    """Map of column letter -> section_id, for the columns where each
    section's score lives in the destination tab. The key set defines
    which columns the writer touches; section_ids absent here are
    skipped (e.g. MS skips no sections; Sales writes all 19)."""

    score_readback_col: str
    """Column letter where the calculated overall score lives (typically
    populated by an ARRAYFORMULA in the sheet itself)."""

    arrayformula_buffer_seconds: float = 4.0
    """Max seconds to wait for the ARRAYFORMULA to evaluate after Stage 2
    writes. Stage 3 polls the readback cell with bounded retries."""

    metadata_cols: dict[str, str]
    """Map of metadata-field name -> column letter for non-section fields
    on the destination tab. Field set varies per team:

    - MS: timestamp, manager_email, agent_name, key_strengths,
      opportunities, dialpad_link
    - Sales: dialpad_link, timestamp, agent_name, evaluator_email,
      feedback_combined (single cell, key_strengths + opportunities
      concatenated)
    """

    weights_reference: Optional[WeightsReferenceConfig] = None


class SheetsConfig(BaseModel):
    """Google Sheets layout for a team."""
    form_responses_ai: FormResponsesAIConfig
    analyst_history: AnalystHistoryConfig
    score_destination: ScoreDestinationConfig
    mails_tab: str


class ScoringPromptConfig(BaseModel):
    """Prompt-level settings for Gemini scoring."""
    system_prompt_template: str
    confidence_levels_note: str
    sop_sections: list[int]                  # section_numbers that need SOP context
    long_call_focus_sections: list[str] = Field(default_factory=list)
    """section_id list that scoring_service.score_call uses to compose the
    long-call attention note (calls over 25 minutes). Each id resolves to
    the section's name + number for the prompt. MS focuses on
    efficiency_call_handling; Sales focuses on hold_usage. Empty list
    skips the attention note."""


class StatsConfig(BaseModel):
    """Per-team statistical thresholds.

    Today every team uses the same defaults — these became magic numbers
    when the engine was MS-only.  Lifted to config so a low-volume team
    (e.g. Sales during ramp) can loosen sensitivity without forking code.

    When 100% Local AI scoring goes live, these defaults will need to
    change again (much higher N → tighter thresholds, longer EWMA spans).
    Kept here so that change is one config edit, not a code release.
    """
    min_evals_for_outlier: int = 5
    outlier_z_threshold: float = 3.5      # Modified-Z cutoff (MAD-based)
    ewma_span: int = 5
    ewma_min_evals: int = 3
    ewma_trend_delta: float = 3.0          # |Δ| to trigger improving/declining
    spc_sigma_multiplier: float = 2.0      # Shewhart control limits


class SectionDef(BaseModel):
    """Definition of a single rubric section."""
    id: str                                  # canonical scoring ID
    history_id: str                          # dashboard/history ID (may differ)
    name: str                                # display name
    section_number: int
    score_type: str                          # "numeric" (1-5 AI), "yn" (Y/N AI),
                                             # "manual" (1-5 analyst input), or
                                             # "manual_yn" (Y/N analyst input —
                                             # AI cannot score, e.g. cases that
                                             # require supervisor verification)
    score_range: Optional[list[int]] = None  # e.g. [1, 5] for numeric
    audio_dependent: bool = False
    rubric_question: Optional[str] = None
    score_descriptions: Optional[dict[str, str]] = None
    na_applicable: bool = False
    confidence_cap: Optional[str] = None
    special_reasoning_instructions: Optional[str] = None

    auto_value: Optional[str] = None
    """When set, the section is never AI-scored: the prompt builder skips
    it, the writer hardcodes this value at the score column, and
    reasoning + confidence cells stay blank. Used for sections with a
    fixed answer (e.g. Sales Q18 screen_recording = 'Yes' for outbound)."""

    manual_default_value: Optional[str] = None
    """Default value Stage 2 writes to the score destination if a manual
    section has no analyst-entered value at approval time. MS sets this
    to '1' for documentation (legacy behavior — the FR1 score formula
    needs a value at L for the calculation to work). Sales leaves it
    null for pb_creation / mc_call_notes (the formula treats blank as
    N/A and excludes from the weighted average)."""

    deprecated_at: Optional[str] = None
    """ISO date. Section stays in the layout for historical row stability
    but is excluded from new evaluations. Reserved — not used at launch."""


# ---------------------------------------------------------------------------
# TeamConfig
# ---------------------------------------------------------------------------

class TeamConfig(BaseModel):
    """Full configuration for one team's QA scoring setup."""
    team_id: str
    display_name: str
    company: str
    rubric_version: str = "1.0"
    excluded_test_agents: list[str] = Field(
        default_factory=list,
        description="Canonical agent names to exclude from analytics (e.g. dev/test users).",
    )
    gemini: GeminiConfig
    sheets: SheetsConfig
    sections: list[SectionDef]
    scoring_prompt: ScoringPromptConfig
    stats: StatsConfig = Field(default_factory=StatsConfig)

    # --- Section partitions --------------------------------------------------

    @property
    def ai_scored_sections(self) -> list[SectionDef]:
        """Sections actually sent to Gemini.

        Excludes manual + manual_yn sections (analyst-only) and
        `auto_value` sections (writer hardcodes them — Gemini never sees
        the question).
        """
        return [
            s for s in self.sections
            if s.score_type not in ("manual", "manual_yn") and s.auto_value is None
        ]

    @property
    def numeric_sections(self) -> list[SectionDef]:
        """Sections whose stored value is a 1-5 score (AI or manual)."""
        return [s for s in self.sections if s.score_type in ("numeric", "manual")]

    @property
    def yn_sections(self) -> list[SectionDef]:
        """Sections whose stored value is Y/N (AI or manual). Includes
        `auto_value` sections (still Y/N data for analytics)."""
        return [s for s in self.sections if s.score_type in ("yn", "manual_yn")]

    @property
    def manual_sections(self) -> list[SectionDef]:
        """Sections filled by the analyst, not Gemini — either shape."""
        return [s for s in self.sections if s.score_type in ("manual", "manual_yn")]

    @property
    def auto_value_sections(self) -> list[SectionDef]:
        """Sections with a hardcoded value — never AI-scored, never analyst-edited."""
        return [s for s in self.sections if s.auto_value is not None]

    # --- ID/label maps -------------------------------------------------------

    @property
    def yn_history_ids(self) -> list[str]:
        """history_id list for Y/N sections — used as DataFrame column names."""
        return [s.history_id for s in self.yn_sections]

    @property
    def yn_section_labels(self) -> dict[str, str]:
        """history_id -> display name for Y/N sections.

        Used by the dashboard to title binary-stats KPI cards and
        roster-table columns.
        """
        return {s.history_id: s.name for s in self.yn_sections}

    @property
    def numeric_history_ids(self) -> list[str]:
        """history_id list for numeric sections (includes manual)."""
        return [s.history_id for s in self.numeric_sections]

    @property
    def section_labels(self) -> dict[str, str]:
        """history_id -> display name for numeric sections."""
        return {s.history_id: s.name for s in self.numeric_sections}

    @property
    def progression_sections(self) -> list[SectionDef]:
        """Sections included in progression assessments.

        Excludes auto_value sections — there's no signal in tracking a
        constant value over time.
        """
        return [s for s in self.sections if s.auto_value is None]

    @property
    def section_name_to_history_id(self) -> dict[str, str]:
        """Display name -> history_id for all sections.

        Used by progression_service to resolve Gemini response keys.
        """
        return {s.name: s.history_id for s in self.sections}

    @property
    def scoring_id_to_section(self) -> dict[str, SectionDef]:
        """Lookup section by canonical scoring ID."""
        return {s.id: s for s in self.sections}

    @property
    def history_id_to_section(self) -> dict[str, SectionDef]:
        """Lookup section by history ID."""
        return {s.history_id: s for s in self.sections}

    @property
    def sections_by_number(self) -> list[SectionDef]:
        """Sections sorted by section_number — the canonical order for the
        derived history/FR-AI layout. section_number is the stable
        identifier; once a team's rubric publishes it must never shift."""
        return sorted(self.sections, key=lambda s: s.section_number)

    # --- Derived layout ------------------------------------------------------

    @property
    def history_layout(self) -> HistoryLayout:
        """Column layout for Analyst_History and Form Responses AI tabs.

        Both tabs share this shape; section index follows section_number
        order (use `sections_by_number` to walk in lockstep).
        """
        return HistoryLayout(n_sections=len(self.sections))


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(__file__).parent / "teams"


@lru_cache(maxsize=8)
def get_team_config(team_id: str = "member_support") -> TeamConfig:
    """Load and cache a team's JSON config.

    Raises FileNotFoundError if the JSON file doesn't exist.
    Raises pydantic.ValidationError if the JSON is malformed.
    """
    path = _CONFIG_DIR / f"{team_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No config for team '{team_id}': {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return TeamConfig(**raw)
