"""Team configuration model and loader.

Each team's rubric, column layout, and Gemini config live in a JSON file
at backend/config/teams/{team_id}.json.  This module provides:

  - Pydantic models for type-safe access to every config field
  - get_team_config(team_id) to load and cache a team's config
  - Computed properties that replace the hardcoded constants scattered
    across services (NUMERIC_SECTIONS, SECTION_LABELS, COLUMN_MAP, etc.)
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


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
    """Column layout for the Form Responses AI tab."""
    tab_name_default: str
    column_map: dict[str, str]              # col letter -> field id (A-P)
    scored_section_columns: dict[str, str]   # col letter -> section id (D-K, M)
    reasoning_start_col: str                 # first reasoning column (Q is source, R starts pairs)
    caller_metadata_cols: dict[str, str]     # field -> col letter (AJ-AL)


class AnalystHistoryConfig(BaseModel):
    """Column layout for the Analyst_History tab."""
    tab_name_default: str
    col_agent_name: int
    col_agent_email: int
    col_timestamp: int
    col_overall_score: int
    section_columns: dict[str, int]          # history_id -> col index (numeric 1-5)
    yn_columns: dict[str, int]               # history_id -> col index (Y/N)
    col_manager_email: int
    col_dialpad_link: int
    col_key_strengths: int
    col_improvements: int
    col_source: int
    extended_columns: dict[str, list[int]]   # history_id -> [confidence_idx, reasoning_idx]
    col_call_summary: int
    col_caller_name: int
    col_caller_phone: int


class SheetsConfig(BaseModel):
    """Google Sheets layout for a team."""
    form_responses_ai: FormResponsesAIConfig
    analyst_history: AnalystHistoryConfig
    form_responses_1_tab: str                # tab name for approved scores
    mails_tab: str                           # tab name for agent roster


class ScoringPromptConfig(BaseModel):
    """Prompt-level settings for Gemini scoring."""
    system_prompt_template: str
    confidence_levels_note: str
    sop_sections: list[int]                  # section_numbers that need SOP context


class SectionDef(BaseModel):
    """Definition of a single rubric section."""
    id: str                                  # canonical scoring ID
    history_id: str                          # dashboard/history ID (may differ)
    name: str                                # display name
    section_number: int
    score_type: str                          # "numeric", "yn", or "manual"
    score_range: Optional[list[int]] = None  # e.g. [1, 5] for numeric
    audio_dependent: bool = False
    rubric_question: Optional[str] = None
    score_descriptions: Optional[dict[str, str]] = None
    na_applicable: bool = False
    confidence_cap: Optional[str] = None
    special_reasoning_instructions: Optional[str] = None


# ---------------------------------------------------------------------------
# TeamConfig
# ---------------------------------------------------------------------------

class TeamConfig(BaseModel):
    """Full configuration for one team's QA scoring setup."""
    team_id: str
    display_name: str
    company: str
    gemini: GeminiConfig
    sheets: SheetsConfig
    sections: list[SectionDef]
    scoring_prompt: ScoringPromptConfig

    # --- Computed properties ------------------------------------------------

    @property
    def ai_scored_sections(self) -> list[SectionDef]:
        """Sections scored by Gemini (excludes manual-only like Documentation)."""
        return [s for s in self.sections if s.score_type != "manual"]

    @property
    def numeric_sections(self) -> list[SectionDef]:
        """Sections with 1-5 numeric scores (includes manual like Documentation)."""
        return [s for s in self.sections if s.score_type in ("numeric", "manual")]

    @property
    def yn_sections(self) -> list[SectionDef]:
        """Binary Y/N sections."""
        return [s for s in self.sections if s.score_type == "yn"]

    @property
    def numeric_history_ids(self) -> list[str]:
        """history_id list for numeric sections.

        Replaces team_stats.NUMERIC_SECTIONS.  Includes Documentation
        because the history sheet stores it as a numeric column.
        """
        return [s.history_id for s in self.numeric_sections]

    @property
    def section_labels(self) -> dict[str, str]:
        """history_id -> display name for numeric sections.

        Replaces team_stats.SECTION_LABELS.
        """
        return {s.history_id: s.name for s in self.numeric_sections}

    @property
    def section_name_to_history_id(self) -> dict[str, str]:
        """Display name -> history_id for AI-scored sections.

        Replaces progression_service._SECTION_NAME_TO_KEY.
        """
        return {s.name: s.history_id for s in self.ai_scored_sections}

    @property
    def scoring_id_to_section(self) -> dict[str, SectionDef]:
        """Lookup section by canonical scoring ID."""
        return {s.id: s for s in self.sections}

    @property
    def history_id_to_section(self) -> dict[str, SectionDef]:
        """Lookup section by history ID."""
        return {s.history_id: s for s in self.sections}


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
