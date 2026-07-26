"""Pydantic models for the Postgres-owned scoring engine.

Wave 2 Phase 1a: the shared schema every downstream write validates against.
Covers the Ops-signed formula/rubric shape (member_support_scoring_migration.md §8,
sales_scoring_migration.md §8) plus the DB-side row shapes from SQLMigration.md
§3.4 / §3.5 / §3.19 / §3.20 / §3.21 / §8.1 / §8.2.

Model summary:

    Formula ─── §8 canonical config (formula_id, scale, normalization,
                sections, rules, evaluation_order, triggers, external_signals)
    Rule    ─── discriminated union of 6 types (score_override, weight_transfer,
                weight_redistribution, score_scale, flag, na_redistribution)
    Rubric  ─── §3.19.1 (rubric_version + sections + scoring_prompt)
    RubricSection ── the full runtime shape (replaces legacy SectionDef)
    ScoringPrompt ── Gemini prompt config, archived with the rubric

    EvaluationSection ── §3.5 per-section row shape
    AssessmentSection ── §3.21 progression-assessment section snapshot
    ModelsUsed / AnnotatedTranscript / RecordingUrls ── JSONB shapes on
                qa.evaluations (§8.1, §8.2, §3.4.2)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


WEIGHT_SUM_TOLERANCE = 0.01  # weights are floats; permit rounding noise
RATING_MIN = 1
RATING_MAX = 5


# ---------------------------------------------------------------------------
# Formula scaffolding
# ---------------------------------------------------------------------------

class ScoreScale(BaseModel):
    """Output range of the formula (0-100 for both teams today)."""
    model_config = ConfigDict(extra="forbid")
    min: float
    max: float


class RatingCurve(BaseModel):
    """Linear rating→frac curve. Both teams use (rating-1)/4 as the assumed
    curve; Sales §10 flags it for confirmation."""
    model_config = ConfigDict(extra="forbid")
    type: Literal["linear"] = "linear"
    input_min: int = RATING_MIN
    input_max: int = RATING_MAX
    output: list[float] = Field(default_factory=lambda: [0.0, 1.0])


class BinaryMap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    Y: float = 1.0
    N: float = 0.0


NaPolicy = Literal["redistribute_per_rules", "full_credit"]
"""Per-formula NA switch (Wave 2 Plan §8 critical risk #3).

    MS   → redistribute_per_rules  (rules move the NA weight elsewhere)
    Sales → full_credit             (NA section keeps its weight at frac=1.0)
"""


class Normalization(BaseModel):
    """Normalization block from Ops-signed §8. `na` is the load-bearing switch."""
    model_config = ConfigDict(extra="forbid")
    rating_1_5: RatingCurve = Field(default_factory=RatingCurve)
    binary_yn: BinaryMap = Field(default_factory=BinaryMap)
    na: NaPolicy


# Ops-signed section score types. `_na` suffix = section may return NA.
# The DB-side qa.evaluation_sections.score_type is a coarser projection
# (numeric / binary / manual_numeric / manual_binary / auto_value — §3.5)
# derived at write time.
FormulaSectionScoreType = Literal[
    "rating_1_5",
    "rating_1_5_na",
    "binary_yn",
    "binary_yn_na",
    "manual_yn",
    "auto_value",
]


SectionTrigger = Literal["active", "candidate"]


class FormulaSection(BaseModel):
    """A section entry inside a Formula. Ops-signed §8 shape.

    Redundant with Rubric.sections on purpose: Formula is archived in
    qa.formula_versions independently of the Rubric archive (qa.rubric_versions)
    so it must be standalone-loadable. Cross-consistency is enforced at eval time
    by compute_overall_score() and by the §3.19.3 rubric-vs-active-formula check.
    """
    model_config = ConfigDict(extra="forbid")

    key: str
    """Ops-signed short section id — matches Rubric.sections[].id."""
    label: str
    score_type: FormulaSectionScoreType
    weight: float
    trigger: Optional[SectionTrigger] = None
    category: Optional[str] = None
    """Optional grouping label (Sales carries it from the source PDF)."""
    binary_map: Optional[BinaryMap] = None
    """Override for the normalization's binary_map on a per-section basis.
    Currently unused; reserved per SQLMigration §3.8 point 3."""
    na_default: bool = False
    """Presentational hint (§3.8 point 7): writer sets binary_value='NA' on
    Stage 1 creation. `human_review_required` is the only section with this
    today."""


# ---------------------------------------------------------------------------
# Rule library (6 types via discriminated union on `type`)
# ---------------------------------------------------------------------------

class RuleWhenSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: str
    equals: Optional[str] = None
    frac_lte: Optional[float] = None
    frac_gte: Optional[float] = None


class RuleWhenSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal: str


class RuleWhenAny(BaseModel):
    model_config = ConfigDict(extra="forbid")
    any: list[Union[RuleWhenSection, RuleWhenSignal]]


class RuleWhenAll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    all: list[Union[RuleWhenSection, RuleWhenSignal]]


RuleWhen = Union[RuleWhenSection, RuleWhenSignal, RuleWhenAny, RuleWhenAll]


class ScoreOverrideEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    set_score: float


class ScoreOverrideRule(BaseModel):
    """MS `hard_zero` — sets final score = 0 when `caller_id = N`.
    Disabled for MS per §5; kept for other teams (collections/billing/legal)."""
    model_config = ConfigDict(extra="forbid")
    id: str
    type: Literal["score_override"]
    enabled: bool
    when: RuleWhen
    effect: ScoreOverrideEffect
    note: Optional[str] = None


class WeightTransferEffect(BaseModel):
    """`amount: "all"` moves the whole weight; `fraction: N.M` moves a share.
    MS's `frequent_caller_shift` uses fraction=0.5."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: str = Field(alias="from")
    to: Union[str, dict[str, float]]
    amount: Optional[Literal["all"]] = None
    fraction: Optional[float] = None
    target_configurable: bool = False

    @model_validator(mode="after")
    def _one_of_amount_or_fraction(self) -> "WeightTransferEffect":
        if (self.amount is None) == (self.fraction is None):
            raise ValueError(
                "weight_transfer effect: exactly one of `amount` or `fraction` required"
            )
        if self.fraction is not None and not (0 < self.fraction <= 1):
            raise ValueError(
                f"weight_transfer fraction must be in (0, 1], got {self.fraction}"
            )
        return self


class WeightTransferRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: Literal["weight_transfer"]
    enabled: bool
    when: RuleWhen
    effect: WeightTransferEffect
    note: Optional[str] = None


WeightRedistributionMethod = Literal["equal_additive", "proportional"]


class WeightRedistributionEffect(BaseModel):
    """Ops-signed `hrr_na_spread` uses method=equal_additive, to=active_sections."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    method: WeightRedistributionMethod
    from_: str = Field(alias="from")
    to: Union[Literal["active_sections", "remaining"], list[str]]


class WeightRedistributionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: Literal["weight_redistribution"]
    enabled: bool
    when: RuleWhen
    effect: WeightRedistributionEffect
    note: Optional[str] = None


class ScoreScaleEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    multiply: float


class ScoreScaleRule(BaseModel):
    """MS `caller_id_scale_half` — post-sum × 0.5 when caller_id=N."""
    model_config = ConfigDict(extra="forbid")
    id: str
    type: Literal["score_scale"]
    enabled: bool
    when: RuleWhen
    effect: ScoreScaleEffect
    note: Optional[str] = None


class FlagEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emit_event: str
    route: Optional[str] = None


class FlagRule(BaseModel):
    """MS `escalation_flag` — emits event, does NOT touch the score."""
    model_config = ConfigDict(extra="forbid")
    id: str
    type: Literal["flag"]
    enabled: bool
    when: RuleWhen
    effect: FlagEffect
    note: Optional[str] = None


class NaRedistributionRule(BaseModel):
    """SQLMigration §3.8's proportional NA redistribution. Distinct from
    weight_redistribution (equal_additive): here the missing weight is spread
    across `targets` proportional to their existing weights."""
    model_config = ConfigDict(extra="forbid")
    id: str
    type: Literal["na_redistribution"]
    enabled: bool
    when: RuleWhen
    mode: Literal["proportional"] = "proportional"
    targets: Union[Literal["remaining"], list[str]] = "remaining"
    note: Optional[str] = None


Rule = Annotated[
    Union[
        ScoreOverrideRule,
        WeightTransferRule,
        WeightRedistributionRule,
        ScoreScaleRule,
        FlagRule,
        NaRedistributionRule,
    ],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Triggers, signals, human review
# ---------------------------------------------------------------------------

class TriggerThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["lte", "lt"] = "lte"
    frac: float = 0.5
    ratings: Optional[list[int]] = None


class TriggersConfig(BaseModel):
    """Ops-signed shape for section-level escalation triggers.

    Distinct from §3.14 `human_review_triggers` (score-threshold on a
    section triggers the human-review queue); both surfaces can coexist —
    `active` here identifies which sections are candidate-for-escalation,
    §3.14 is the pipeline-level gate. Sales' triggers block is empty."""
    model_config = ConfigDict(extra="forbid")
    threshold: Optional[TriggerThreshold] = None
    active: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    routes_to: Optional[str] = None


class ExternalSignal(BaseModel):
    """MS's `frequent_caller` signal (looker_snapshot via command_center).
    Wave 2 wires signals as `false` by default until CC ships (Wave 3)."""
    model_config = ConfigDict(extra="forbid")
    source: str
    via: Optional[str] = None


class HumanReviewTrigger(BaseModel):
    """§3.14 pipeline trigger. Distinct from TriggersConfig's active list —
    this one gates the human-review queue by a numeric_score threshold."""
    model_config = ConfigDict(extra="forbid")
    section_id: str
    max_score_to_trigger: int = Field(ge=RATING_MIN, le=RATING_MAX)


# ---------------------------------------------------------------------------
# Formula (top-level)
# ---------------------------------------------------------------------------

WEIGHTED_SUM = "weighted_sum"
"""Sentinel token used in evaluation_order to mark where Σ(weight×frac) runs."""


class Formula(BaseModel):
    """Ops-signed §8 canonical config. Archived as qa.formula_versions.formula_json."""
    model_config = ConfigDict(extra="forbid")

    formula_id: str
    rubric_version: str
    supersedes: Optional[str] = None
    scale: ScoreScale
    normalization: Normalization
    sections: list[FormulaSection]
    rules: list[Rule] = Field(default_factory=list)
    evaluation_order: list[str]
    triggers: TriggersConfig = Field(default_factory=TriggersConfig)
    external_signals: dict[str, ExternalSignal] = Field(default_factory=dict)
    deprecated_sections: list[str] = Field(default_factory=list)
    human_review_triggers: list[HumanReviewTrigger] = Field(default_factory=list)

    @model_validator(mode="after")
    def _weights_sum_to_scale_max(self) -> "Formula":
        total = sum(s.weight for s in self.sections)
        target = self.scale.max
        if abs(total - target) > WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                f"formula {self.formula_id!r}: section weights sum to {total} "
                f"but scale.max is {target} (tolerance {WEIGHT_SUM_TOLERANCE})"
            )
        return self

    @model_validator(mode="after")
    def _unique_section_keys(self) -> "Formula":
        keys = [s.key for s in self.sections]
        if len(set(keys)) != len(keys):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            raise ValueError(
                f"formula {self.formula_id!r}: duplicate section keys {dupes}"
            )
        return self

    @model_validator(mode="after")
    def _unique_rule_ids(self) -> "Formula":
        ids = [r.id for r in self.rules]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                f"formula {self.formula_id!r}: duplicate rule ids {dupes}"
            )
        return self

    @model_validator(mode="after")
    def _evaluation_order_matches_rules(self) -> "Formula":
        rule_ids = {r.id for r in self.rules}
        allowed = rule_ids | {WEIGHTED_SUM}
        unknown = [t for t in self.evaluation_order if t not in allowed]
        if unknown:
            raise ValueError(
                f"formula {self.formula_id!r}: evaluation_order references "
                f"unknown tokens {unknown} (allowed: rule ids + {WEIGHTED_SUM!r})"
            )
        if WEIGHTED_SUM not in self.evaluation_order:
            raise ValueError(
                f"formula {self.formula_id!r}: evaluation_order must include "
                f"the {WEIGHTED_SUM!r} sentinel"
            )
        listed_rules = [t for t in self.evaluation_order if t in rule_ids]
        if len(set(listed_rules)) != len(listed_rules):
            raise ValueError(
                f"formula {self.formula_id!r}: evaluation_order lists a rule "
                f"more than once"
            )
        missing = rule_ids - set(listed_rules)
        if missing:
            raise ValueError(
                f"formula {self.formula_id!r}: rules {sorted(missing)} defined "
                f"but not scheduled in evaluation_order"
            )
        return self

    @model_validator(mode="after")
    def _human_review_triggers_reference_existing_sections(self) -> "Formula":
        keys = {s.key for s in self.sections}
        bad = [t.section_id for t in self.human_review_triggers if t.section_id not in keys]
        if bad:
            raise ValueError(
                f"formula {self.formula_id!r}: human_review_triggers reference "
                f"unknown sections {bad}"
            )
        return self


# ---------------------------------------------------------------------------
# Rubric — the full runtime shape (replaces legacy SectionDef)
# ---------------------------------------------------------------------------

# Runtime score_type — matches the current app's expectations. The `_na`
# distinction lives on `na_applicable`, not encoded in this enum, so
# existing filters like `s.score_type in ("numeric", "manual")` keep working.
RubricSectionScoreType = Literal[
    "numeric",
    "yn",
    "manual",
    "manual_yn",
    "auto_value",
]


class RubricSection(BaseModel):
    """A single rubric section. Replaces `SectionDef` in team_config.py.

    Uses Ops-signed short `id` (Wave 2 Plan §9 remapping). Preserves the
    runtime fields SectionDef carried (rubric_question, score_descriptions,
    etc.) so the AI scoring pipeline continues to work unchanged.
    """
    model_config = ConfigDict(extra="forbid")

    id: str
    history_id: str
    name: str
    section_number: int
    score_type: RubricSectionScoreType
    score_range: Optional[list[int]] = None
    audio_dependent: bool = False
    rubric_question: Optional[str] = None
    score_descriptions: Optional[dict[str, str]] = None
    na_applicable: bool = False
    na_applies_when: Optional[str] = None
    """Scorer-facing policy: when NA is legitimately earned (sales_v2 rubric
    introduced this — NA grants full credit downstream, so it must be strict).
    Additive per §3.8 point 5; absent on older archived rubrics."""
    category: Optional[str] = None
    """Reporting group (Sales carries the PDF's category; B5 sign-off)."""
    confidence_cap: Optional[str] = None
    special_reasoning_instructions: Optional[str] = None
    auto_value: Optional[str] = None
    manual_default_value: Optional[str] = None
    deprecated_at: Optional[str] = None

    @model_validator(mode="after")
    def _auto_value_score_type(self) -> "RubricSection":
        if self.auto_value is not None and self.score_type != "auto_value":
            # SectionDef's convention lets `auto_value` coexist with `yn`
            # (Sales Q18 screen_recording is `yn` + auto_value="Yes"). Preserve
            # that: only enforce the invariant that `score_type='auto_value'`
            # implies `auto_value` is set.
            pass
        if self.score_type == "auto_value" and self.auto_value is None:
            raise ValueError(
                f"section {self.id!r}: score_type='auto_value' requires `auto_value` to be set"
            )
        return self


class ScoringPrompt(BaseModel):
    """Prompt-level settings for the AI scorer. Archived with the rubric per
    §3.19.1 so the AI contract is versioned end-to-end alongside sections."""
    model_config = ConfigDict(extra="forbid")

    system_prompt_template: str
    confidence_levels_note: str
    sop_sections: list[Union[str, int]] = Field(default_factory=list)
    """Section ids that need SOP context in the prompt.

    §3.19.1 validation: every id must exist in Rubric.sections[].id.
    Ints are accepted as legacy section_number references because the
    migration-010 seed archived them that way (e.g. member_support_v1 has
    `[5, 6]`) and archived rubric_json is immutable — Rubric normalizes
    them to ids at parse time. New content should always use ids.
    """
    long_call_focus_sections: list[str] = Field(default_factory=list)
    audio_dependent_sections: list[str] = Field(default_factory=list)
    """Plan B routing config (§8.3a). Sections whose scoring quality depends
    on audio fidelity — LandGPT reroutes them to Gemini if all come back
    uniform LOW confidence."""


class Rubric(BaseModel):
    """qa.rubric_versions.rubric_json — the archived rubric per §3.19.1."""
    model_config = ConfigDict(extra="forbid")

    rubric_version: str
    sections: list[RubricSection]
    scoring_prompt: ScoringPrompt

    @model_validator(mode="after")
    def _unique_section_ids(self) -> "Rubric":
        ids = [s.id for s in self.sections]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                f"rubric {self.rubric_version!r}: duplicate section ids {dupes}"
            )
        return self

    @model_validator(mode="after")
    def _normalize_legacy_sop_section_numbers(self) -> "Rubric":
        """Archived pre-v2 rubrics reference sop_sections by section_number
        (ints). Normalize to ids so downstream consumers see one shape."""
        if any(isinstance(i, int) for i in self.scoring_prompt.sop_sections):
            by_number = {s.section_number: s.id for s in self.sections}
            unknown = [i for i in self.scoring_prompt.sop_sections
                       if isinstance(i, int) and i not in by_number]
            if unknown:
                raise ValueError(
                    f"rubric {self.rubric_version!r}: sop_sections references "
                    f"unknown section numbers {unknown}"
                )
            self.scoring_prompt.sop_sections = [
                by_number[i] if isinstance(i, int) else i
                for i in self.scoring_prompt.sop_sections
            ]
        return self

    @model_validator(mode="after")
    def _scoring_prompt_section_ids_exist(self) -> "Rubric":
        section_ids = {s.id for s in self.sections}
        bad_sop = [i for i in self.scoring_prompt.sop_sections if i not in section_ids]
        bad_long = [i for i in self.scoring_prompt.long_call_focus_sections if i not in section_ids]
        bad_audio = [i for i in self.scoring_prompt.audio_dependent_sections if i not in section_ids]
        errs = []
        if bad_sop:
            errs.append(f"sop_sections references unknown section ids {bad_sop}")
        if bad_long:
            errs.append(f"long_call_focus_sections references unknown section ids {bad_long}")
        if bad_audio:
            errs.append(f"audio_dependent_sections references unknown section ids {bad_audio}")
        if errs:
            raise ValueError(f"rubric {self.rubric_version!r}: " + "; ".join(errs))
        return self


# ---------------------------------------------------------------------------
# Evaluation + Assessment section rows
# ---------------------------------------------------------------------------

# DB-facing score_type on qa.evaluation_sections (§3.5). Coarser than the
# rubric's runtime score_type — flattens manual/manual_yn/numeric/binary
# into the persisted enum values.
EvaluationSectionScoreType = Literal[
    "numeric",
    "binary",
    "manual_numeric",
    "manual_binary",
    "auto_value",
]

EvaluationScoreSource = Literal["ai", "manual", "auto_value", "manual_default"]
EvaluationAiProvider = Literal["gemini", "landgpt"]


class EvaluationSection(BaseModel):
    """One row of qa.evaluation_sections. §3.5 shape.

    CHECKs enforced by DB: exactly one of numeric_score/binary_value populated
    matching score_type; ai_provider populated iff score_source='ai'. Pydantic
    mirrors those checks at the writer boundary so violations fail fast.
    """
    model_config = ConfigDict(extra="forbid")

    evaluation_id: Optional[int] = None
    section_id: str
    section_number: int
    score_type: EvaluationSectionScoreType
    numeric_score: Optional[int] = Field(default=None, ge=RATING_MIN, le=RATING_MAX)
    binary_value: Optional[Literal["Y", "N", "NA"]] = None
    score_source: EvaluationScoreSource
    ai_provider: Optional[EvaluationAiProvider] = None
    model: Optional[str] = None
    confidence: Optional[Literal["LOW", "MED", "HIGH"]] = None
    reasoning: Optional[str] = None

    @model_validator(mode="after")
    def _exactly_one_score_populated(self) -> "EvaluationSection":
        is_numeric = self.score_type in ("numeric", "manual_numeric")
        is_binary = self.score_type in ("binary", "manual_binary")
        if is_numeric:
            # Migration 012 / §3.8 point 7: an unscored na_default section
            # persists as numeric_score NULL + binary_value 'NA'. Y/N on a
            # numeric row remains invalid.
            is_na = self.numeric_score is None and self.binary_value == "NA"
            if not is_na and (self.numeric_score is None or self.binary_value is not None):
                raise ValueError(
                    f"section {self.section_id!r}: score_type={self.score_type!r} "
                    "requires numeric_score set and binary_value NULL (or the "
                    "NA shape: numeric_score NULL and binary_value='NA')"
                )
        elif is_binary:
            if self.binary_value is None or self.numeric_score is not None:
                raise ValueError(
                    f"section {self.section_id!r}: score_type={self.score_type!r} "
                    "requires binary_value set and numeric_score NULL"
                )
        elif self.score_type == "auto_value":
            if self.binary_value is None and self.numeric_score is None:
                raise ValueError(
                    f"section {self.section_id!r}: auto_value section must "
                    "populate one of binary_value or numeric_score"
                )
        return self

    @model_validator(mode="after")
    def _ai_provider_iff_ai_source(self) -> "EvaluationSection":
        if self.score_source == "ai":
            if self.ai_provider is None:
                raise ValueError(
                    f"section {self.section_id!r}: score_source='ai' requires ai_provider"
                )
        else:
            if self.ai_provider is not None:
                raise ValueError(
                    f"section {self.section_id!r}: ai_provider only valid when "
                    "score_source='ai'"
                )
        return self


AssessmentTrend = Literal["improving", "stable", "declining"]


class AssessmentSection(BaseModel):
    """qa.assessment_sections row — §3.21 shape. Snapshots section identity
    at generation time so historical assessments render correctly after the
    rubric is reshaped."""
    model_config = ConfigDict(extra="forbid")

    assessment_id: Optional[int] = None
    section_id: str
    section_name: str
    section_number: int
    trend: AssessmentTrend
    summary: str
    coaching_tip: str


# ---------------------------------------------------------------------------
# JSONB shapes on qa.evaluations
# ---------------------------------------------------------------------------

class ModelInfo(BaseModel):
    """One leg of the audio/text cascade in `models_used`."""
    model_config = ConfigDict(extra="forbid")
    provider: str
    model: str
    version: Optional[str] = None


class FallbackInfo(BaseModel):
    """Plan B activation record — which sections rerouted and why. §8.3a."""
    model_config = ConfigDict(extra="forbid")
    provider: str
    sections: list[str]
    reason: str


class ModelsUsed(BaseModel):
    """qa.evaluations.models_used JSONB — §8.1 shape.

    Pre-LandGPT runs: `audio` NULL/omitted, `text = gemini-2.5-flash`.
    Post-LandGPT: both legs populated, `fallback` records Plan B if fired.
    """
    model_config = ConfigDict(extra="forbid")
    audio: Optional[ModelInfo] = None
    text: Optional[ModelInfo] = None
    fallback: Optional[FallbackInfo] = None


class TranscriptTurn(BaseModel):
    """One turn in AnnotatedTranscript.turns. Qwen2-Audio output per §8.2."""
    model_config = ConfigDict(extra="forbid")
    speaker: Literal["agent", "caller", "system", "other"]
    text: str
    emotion: Optional[str] = None
    paraphrase_intent: Optional[str] = None
    pace_marker: Optional[str] = None
    interruption: Optional[bool] = None
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None


class HoldSegment(BaseModel):
    """One OBSERVATIONAL hold heard in the audio — Stage-A output
    (TwoStageScoringDesign §3.1). Never a verified system fact: verified
    hold durations come from command_center.hold_times via the grounding
    block, gated by has_hold_truth. Prompts must word these as
    observations ("audio suggests a hold of ~63s"), never assertions."""
    model_config = ConfigDict(extra="forbid")
    start_ms: int
    end_ms: int
    kind: Literal["hold_music", "dead_air", "mute_suspected"]
    note: Optional[str] = None


class AnnotatedTranscript(BaseModel):
    """qa.evaluations.annotated_transcript JSONB — §8.2 shape, extended
    additively by TwoStageScoringDesign §3 (schema_version exists exactly
    so variants can do this):

    - ``qwen2_audio_v1``      — the original LandGPT shape (turns only)
    - ``gemini_annotate_v1``  — the cloud Stage-A annotator; adds
      ``holds`` (observational) + ``call_observations`` (call-level)

    NULL for evaluations scored without an annotation stage (single-stage
    Gemini / Plan-B fallback routes).
    """
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    language_detected: Optional[str] = None
    turns: list[TranscriptTurn]
    holds: list[HoldSegment] = Field(default_factory=list)
    call_observations: list[str] = Field(default_factory=list)


class RecordingUrls(BaseModel):
    """qa.evaluations.recording_urls JSONB — §3.4.2 shape. Raw dialpad
    recording_details (ids, durations, start_time) stay in dialpad_call_metadata."""
    model_config = ConfigDict(extra="forbid")
    audio: list[str] = Field(default_factory=list)
    screen: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Cross-artifact consistency (§3.19.3)
# ---------------------------------------------------------------------------

def validate_formula_against_rubric(formula: Formula, rubric: Rubric) -> None:
    """§3.19.3 hard-fail check — every section the formula references must
    exist in the rubric. Called at API boundaries where formula/rubric are
    loaded together (e.g. compute_overall_score, POST /rubric edit path).
    """
    rubric_ids = {s.id for s in rubric.sections}
    formula_ids = {s.key for s in formula.sections}

    # Rules and evaluation_order can reference sections via when/effect.
    rule_referenced: set[str] = set()
    for rule in formula.rules:
        rule_referenced |= _sections_in_when(rule.when)
        rule_referenced |= _sections_in_effect(rule)

    missing = (formula_ids | rule_referenced) - rubric_ids
    if missing:
        raise FormulaRubricMismatchError(sorted(missing), formula.formula_id, rubric.rubric_version)


def _sections_in_when(when: RuleWhen) -> set[str]:
    if isinstance(when, RuleWhenSection):
        return {when.section}
    if isinstance(when, RuleWhenSignal):
        return set()
    if isinstance(when, (RuleWhenAny, RuleWhenAll)):
        clauses = when.any if isinstance(when, RuleWhenAny) else when.all
        out: set[str] = set()
        for c in clauses:
            if isinstance(c, RuleWhenSection):
                out.add(c.section)
        return out
    return set()


def _sections_in_effect(rule: Rule) -> set[str]:
    out: set[str] = set()
    effect = getattr(rule, "effect", None)
    if effect is None:
        return out
    src = getattr(effect, "from_", None)
    if isinstance(src, str):
        out.add(src)
    dst = getattr(effect, "to", None)
    if isinstance(dst, str) and dst not in ("active_sections", "remaining"):
        out.add(dst)
    elif isinstance(dst, list):
        out.update(dst)
    elif isinstance(dst, dict):
        out.update(dst.keys())
    return out


class FormulaRubricMismatchError(ValueError):
    """§3.19.3: rubric drops/renames a section the active formula references."""

    def __init__(self, missing: list[str], formula_id: str, rubric_version: str) -> None:
        self.missing = missing
        self.formula_id = formula_id
        self.rubric_version = rubric_version
        super().__init__(
            f"formula {formula_id!r} references sections not in rubric "
            f"{rubric_version!r}: {missing}"
        )


__all__ = [
    # Formula
    "Formula",
    "FormulaSection",
    "FormulaSectionScoreType",
    "ScoreScale",
    "Normalization",
    "RatingCurve",
    "BinaryMap",
    "NaPolicy",
    "SectionTrigger",
    "TriggersConfig",
    "TriggerThreshold",
    "ExternalSignal",
    "HumanReviewTrigger",
    "WEIGHTED_SUM",
    # Rules
    "Rule",
    "ScoreOverrideRule",
    "ScoreOverrideEffect",
    "WeightTransferRule",
    "WeightTransferEffect",
    "WeightRedistributionRule",
    "WeightRedistributionEffect",
    "WeightRedistributionMethod",
    "ScoreScaleRule",
    "ScoreScaleEffect",
    "FlagRule",
    "FlagEffect",
    "NaRedistributionRule",
    "RuleWhen",
    "RuleWhenSection",
    "RuleWhenSignal",
    "RuleWhenAny",
    "RuleWhenAll",
    # Rubric
    "Rubric",
    "RubricSection",
    "RubricSectionScoreType",
    "ScoringPrompt",
    # DB row shapes
    "EvaluationSection",
    "EvaluationSectionScoreType",
    "EvaluationScoreSource",
    "EvaluationAiProvider",
    "AssessmentSection",
    "AssessmentTrend",
    # JSONB shapes
    "ModelsUsed",
    "ModelInfo",
    "FallbackInfo",
    "AnnotatedTranscript",
    "TranscriptTurn",
    "RecordingUrls",
    # Cross-checks
    "validate_formula_against_rubric",
    "FormulaRubricMismatchError",
]
