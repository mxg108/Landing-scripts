"""Team configuration model and loader.

Wave 2 Phase 1a: `RubricSection` (backend/models/formula.py) replaces the
legacy `SectionDef` — same runtime surface, Ops-signed short section ids.
The loader also assembles a `Rubric` from either the new nested shape (MS)
or the legacy flat shape (Sales, until §10 sign-off closes).

Layout:

    config/teams/{team}.json                  — runtime team config + rubric
    config/scoring/{team}/overall_formula.json — Formula (Phase 3a lands v2 rows)

MS ships in the new nested shape (rubric block, Ops-signed short ids).
Sales keeps the flat shape (top-level sections/scoring_prompt/rubric_version)
until Wave 2 Phase 3c closes the §10 sign-off; the loader detects and adapts.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.config.history_layout import HistoryLayout
from backend.models.formula import (
    Rubric,
    RubricSection,
    ScoringPrompt,
)


# ---------------------------------------------------------------------------
# Sub-models — Sheets/Gemini/Stats runtime config (unchanged from Wave 1)
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
    """Analyst_History tab — finalized evaluations."""
    tab_name: str
    tab_name_legacy: Optional[str] = None


class WeightsReferenceConfig(BaseModel):
    """Reference to a per-team weights tab used by the score formula."""
    tab_name: str
    range: str
    comment: Optional[str] = None


class ScoreDestinationConfig(BaseModel):
    """Per-team score-destination tab where approved scores land and the
    overall-score formula calculates."""
    tab_name: str
    section_score_columns: dict[str, str]
    score_readback_col: str
    arrayformula_buffer_seconds: float = 4.0
    metadata_cols: dict[str, str]
    metadata_cols_note: Optional[str] = None
    weights_reference: Optional[WeightsReferenceConfig] = None


class SheetsConfig(BaseModel):
    """Google Sheets layout for a team."""
    form_responses_ai: FormResponsesAIConfig
    analyst_history: AnalystHistoryConfig
    score_destination: ScoreDestinationConfig
    mails_tab: str


class StatsConfig(BaseModel):
    """Per-team statistical thresholds (unchanged from Wave 1)."""
    min_evals_for_outlier: int = 5
    outlier_z_threshold: float = 3.5
    ewma_span: int = 5
    ewma_min_evals: int = 3
    ewma_trend_delta: float = 3.0
    spc_sigma_multiplier: float = 2.0


class RescoreConfig(BaseModel):
    """ScorecardActionsDesign §4.2 — the auto-rescore knob.

    A finalize landing ``overall_score <= threshold`` on a ``source='ai'``
    row triggers the machine's one retry (once EVER per evaluation —
    the ``auto_rescored_at`` latch). Lives in team JSON beside the stats
    knobs; absent → default 50."""
    threshold: float = 50.0


class HumanReviewConfig(BaseModel):
    """ScorecardActionsDesign §0.3 — the Human-Review pipeline mode.

    ``authoritative`` (default): a fired §3.14 trigger BLOCKS finalize —
    the row holds in draft at scoring_status='flagged_human_review' until
    an analyst resolves it. ``informative``: flagged rows finalize and
    ship like any other; ``human_review_required_at`` stays as the queue
    marker (required_at IS NOT NULL AND completed_at IS NULL) and the
    blocking scoring_status is never set. Designed now so the flip lands
    as team-JSON config, not a redesign."""
    mode: Literal["authoritative", "informative"] = "authoritative"


class HrExportSectionConfig(BaseModel):
    """One HR-visible section: rubric section id + the header HR sees."""
    id: str
    hr_label: str


class HrExportConfig(BaseModel):
    """HR bonus sheet export surface (references/HRBonusSheet.md §3).

    Lives at the top level of the team JSON — NOT inside the rubric block,
    which is content-hashed for version archiving. Aggregation shape
    (numeric mean vs binary %Yes) is derived from the rubric section's
    score_type, never restated here.
    """
    sections: list[HrExportSectionConfig]
    excluded_agents: list[str] = Field(
        default_factory=list,
        description="Raw agent names (case-insensitive) filtered out of the HR export.",
    )


# ---------------------------------------------------------------------------
# TeamConfig
# ---------------------------------------------------------------------------

class TeamConfig(BaseModel):
    """Full configuration for one team's QA scoring setup.

    `rubric` (RubricSection[] + ScoringPrompt) replaces the legacy top-level
    `sections` + `scoring_prompt` + `rubric_version` triplet. The properties
    below preserve the SectionDef-era filtering surface — every downstream
    consumer keeps the same `.numeric_sections`, `.yn_sections`, etc.
    """
    team_id: str
    display_name: str
    company: str
    excluded_test_agents: list[str] = Field(
        default_factory=list,
        description="Canonical agent names to exclude from analytics (e.g. dev/test users).",
    )
    gemini: GeminiConfig
    sheets: SheetsConfig
    rubric: Rubric
    stats: StatsConfig = Field(default_factory=StatsConfig)
    rescore: RescoreConfig = Field(default_factory=RescoreConfig)
    human_review: HumanReviewConfig = Field(default_factory=HumanReviewConfig)
    hr_export: Optional[HrExportConfig] = None
    hr_internal_section_ids: list[str] = Field(
        default_factory=list,
        description="Section ids that must never leave internal surfaces "
                    "(HR export rejects them — HRBonusSheet.md §3).",
    )

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def _validate_hr_export(self) -> "TeamConfig":
        """hr_export sections must be non-empty, unique, rubric-known, and
        never internal-only. This is the mechanical form of the
        "Documentation / Human Review Required never leaves the building"
        guarantee from the HR bonus sheet spec."""
        if self.hr_export is None:
            return self
        if not self.hr_export.sections:
            raise ValueError("hr_export.sections must not be empty")
        known = {s.id for s in self.rubric.sections}
        internal = set(self.hr_internal_section_ids)
        seen: set[str] = set()
        for sec in self.hr_export.sections:
            if sec.id not in known:
                raise ValueError(f"hr_export section '{sec.id}' is not a rubric section")
            if sec.id in internal:
                raise ValueError(
                    f"hr_export section '{sec.id}' is internal-only and must never be exported"
                )
            if sec.id in seen:
                raise ValueError(f"hr_export section '{sec.id}' listed twice")
            seen.add(sec.id)
        return self

    # --- Rubric passthroughs (backward-compat surface) ----------------------

    @property
    def rubric_version(self) -> str:
        return self.rubric.rubric_version

    @property
    def sections(self) -> list[RubricSection]:
        return self.rubric.sections

    @property
    def scoring_prompt(self) -> ScoringPrompt:
        return self.rubric.scoring_prompt

    # --- Section partitions --------------------------------------------------

    @property
    def ai_scored_sections(self) -> list[RubricSection]:
        """Sections sent to the AI scorer. Excludes manual/manual_yn (analyst-
        filled) and `auto_value` sections (writer hardcodes them)."""
        return [
            s for s in self.sections
            if s.score_type not in ("manual", "manual_yn") and s.auto_value is None
        ]

    @property
    def numeric_sections(self) -> list[RubricSection]:
        """Sections whose stored value is a 1-5 score (AI or manual)."""
        return [s for s in self.sections if s.score_type in ("numeric", "manual")]

    @property
    def yn_sections(self) -> list[RubricSection]:
        """Sections whose stored value is Y/N (AI or manual). Includes
        `auto_value` sections (still Y/N data for analytics)."""
        return [s for s in self.sections if s.score_type in ("yn", "manual_yn")]

    @property
    def manual_sections(self) -> list[RubricSection]:
        """Sections filled by the analyst, not the AI scorer."""
        return [s for s in self.sections if s.score_type in ("manual", "manual_yn")]

    @property
    def auto_value_sections(self) -> list[RubricSection]:
        """Sections with a hardcoded value — never AI-scored, never analyst-edited."""
        return [s for s in self.sections if s.auto_value is not None]

    # --- ID/label maps -------------------------------------------------------

    @property
    def yn_history_ids(self) -> list[str]:
        """history_id list for Y/N sections — used as DataFrame column names."""
        return [s.history_id for s in self.yn_sections]

    @property
    def yn_section_labels(self) -> dict[str, str]:
        """history_id -> display name for Y/N sections."""
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
    def progression_sections(self) -> list[RubricSection]:
        """Sections included in progression assessments. Excludes auto_value."""
        return [s for s in self.sections if s.auto_value is None]

    @property
    def section_name_to_history_id(self) -> dict[str, str]:
        """Display name -> history_id for all sections. Used by
        progression_service to resolve Gemini response keys."""
        return {s.name: s.history_id for s in self.sections}

    @property
    def scoring_id_to_section(self) -> dict[str, RubricSection]:
        """Lookup section by canonical scoring ID (Ops-signed short key)."""
        return {s.id: s for s in self.sections}

    @property
    def history_id_to_section(self) -> dict[str, RubricSection]:
        """Lookup section by history ID (legacy Sheets-column identifier)."""
        return {s.history_id: s for s in self.sections}

    @property
    def sections_by_number(self) -> list[RubricSection]:
        """Sections sorted by section_number — the canonical order for the
        derived history/FR-AI layout."""
        return sorted(self.sections, key=lambda s: s.section_number)

    # --- Derived layout ------------------------------------------------------

    @property
    def history_layout(self) -> HistoryLayout:
        """Column layout for Analyst_History and Form Responses AI tabs."""
        return HistoryLayout(n_sections=len(self.sections))


# ---------------------------------------------------------------------------
# Loader — handles both the new nested shape (MS) and the legacy flat shape (Sales)
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(__file__).parent / "teams"


def _normalize_scoring_prompt(raw_prompt: dict[str, Any], sections: list[dict]) -> dict[str, Any]:
    """Convert legacy scoring_prompt fields to the new ScoringPrompt shape.

    Legacy Sales JSON uses `sop_sections: list[int]` (section numbers).
    New shape uses `sop_sections: list[str]` (section ids). We convert by
    resolving section_number → id against the sections array.

    Also adds `audio_dependent_sections` (§8.3a Plan B) by scanning the
    sections' `audio_dependent` flag if the field is missing.
    """
    prompt = dict(raw_prompt)

    number_to_id = {s["section_number"]: s["id"] for s in sections if "section_number" in s and "id" in s}

    sop = prompt.get("sop_sections", [])
    if sop and all(isinstance(v, int) for v in sop):
        prompt["sop_sections"] = [number_to_id[n] for n in sop if n in number_to_id]

    if "audio_dependent_sections" not in prompt:
        prompt["audio_dependent_sections"] = [
            s["id"] for s in sections if s.get("audio_dependent")
        ]

    return prompt


def _assemble_rubric_block(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract a Rubric-shaped dict from either the new or legacy JSON layout.

    New (MS): `raw["rubric"] = {"rubric_version": "...", "sections": [...], "scoring_prompt": {...}}`
    Legacy (Sales): top-level `raw["rubric_version"]`, `raw["sections"]`, `raw["scoring_prompt"]`
    """
    if "rubric" in raw:
        block = dict(raw["rubric"])
    else:
        block = {
            "rubric_version": raw.get("rubric_version", f"{raw.get('team_id', 'unknown')}_v1"),
            "sections": raw["sections"],
            "scoring_prompt": raw["scoring_prompt"],
        }

    block["scoring_prompt"] = _normalize_scoring_prompt(
        block["scoring_prompt"], block["sections"]
    )
    return block


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
    raw["rubric"] = _assemble_rubric_block(raw)
    for legacy_key in ("rubric_version", "sections", "scoring_prompt"):
        raw.pop(legacy_key, None)
    return TeamConfig(**raw)


def get_all_team_ids() -> list[str]:
    """Return sorted list of team_ids with a JSON config on disk."""
    return sorted(p.stem for p in _CONFIG_DIR.glob("*.json"))
