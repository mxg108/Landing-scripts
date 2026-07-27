"""Pydantic models for the QA scorecard and pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ValidationInfo, model_validator


class ScorecardSection(BaseModel):
    id: str
    name: str
    score: Optional[int] = None        # 1-5 for numeric sections; null for yn or explicit N/A
    score_type: str                     # "numeric" or "yn"
    yn_value: Optional[str] = None
    # "Y" / "N" — yn / manual_yn sections only.
    # "NA"     — yn / manual_yn AND numeric / manual sections whose team-config
    #            entry has `na_applicable: true`. Acts as the explicit N/A flag
    #            for any score_type; `score` must be None when set to "NA".
    confidence: str                     # "high", "medium", "low"
    reasoning: str
    audio_dependent: bool = False
    flags: List[str] = []

    @model_validator(mode="after")
    def _validate_score_shape(self, info: ValidationInfo) -> "ScorecardSection":
        """Cross-field consistency + optional team-config check.

        Always-on (no context): catches impossible shapes regardless of source.
            * yn_value="NA" must coexist with score=None.
            * yn_value in {"Y","N"} requires score_type yn/manual_yn and score=None.
            * a numeric score requires score_type numeric/manual and yn_value=None.

        Context-aware (when caller passes context={"section_def": sec_def}):
            * rejects yn_value="NA" on a section with na_applicable=False —
              defense-in-depth against a model hallucinating NA on a section
              that doesn't accept it.
        """
        yn = self.yn_value
        score = self.score
        st = self.score_type

        if yn == "NA" and score is not None:
            raise ValueError(
                f"section {self.id!r}: yn_value='NA' requires score=None, got {score!r}"
            )
        if yn in ("Y", "N"):
            if st not in ("yn", "manual_yn"):
                raise ValueError(
                    f"section {self.id!r}: yn_value={yn!r} is only valid on yn/manual_yn "
                    f"sections, got score_type={st!r}"
                )
            if score is not None:
                raise ValueError(
                    f"section {self.id!r}: yn_value={yn!r} requires score=None, got {score!r}"
                )
        if score is not None:
            if st not in ("numeric", "manual"):
                raise ValueError(
                    f"section {self.id!r}: numeric score={score!r} is only valid on "
                    f"numeric/manual sections, got score_type={st!r}"
                )
            if yn not in (None, ""):
                raise ValueError(
                    f"section {self.id!r}: numeric score and yn_value={yn!r} are "
                    f"mutually exclusive"
                )

        ctx = info.context or {}
        sec_def = ctx.get("section_def")
        if sec_def is None:
            by_id = ctx.get("sections_by_id")
            if isinstance(by_id, dict):
                sec_def = by_id.get(self.id)
        if sec_def is not None and yn == "NA" and not getattr(sec_def, "na_applicable", False):
            raise ValueError(
                f"section {self.id!r}: yn_value='NA' but team config declares "
                f"na_applicable=false"
            )

        return self


class Scorecard(BaseModel):
    sections: List[ScorecardSection]
    call_summary: str = ""
    key_strengths: str
    opportunities: str


class ApprovalRequest(BaseModel):
    """Payload from the frontend Approve & Send button."""
    sections: List[ScorecardSection]
    key_strengths: str
    opportunities: str
    # ScorecardActionsDesign §3 (S1): the in-memory job carries the
    # original manager_email, but an approval context reconstructed from
    # the DB after a restart doesn't — the frontend sends the signed-in
    # evaluator here. Optional for backward compatibility; the route
    # falls back to job/row identity and 422s only when every source
    # comes up empty.
    evaluator_email: Optional[str] = None


class ScorecardWithMeta(Scorecard):
    """Scorecard enriched with call metadata for the pipeline."""
    call_id: Optional[str] = None
    agent_name: Optional[str] = None
    manager_email: Optional[str] = None
    dialpad_link: Optional[str] = None
    duration_ms: Optional[float] = None
    flagged_long_call: bool = False
    model: str = "gemini-2.5-flash"
    sop_used: Optional[str] = None      # SOP title injected, if any
    caller_name: Optional[str] = None   # From Dialpad get_call_details
    caller_phone: Optional[str] = None  # From Dialpad get_call_details
    # Call-time initiative (PR-1) — see references/CallTimeOnAnalystHistory.md
    call_started_at_utc: Optional[datetime] = None
    """Dialpad `date_connected` for this call, normalized to a UTC-aware
    datetime by `_epoch_ms_to_utc_datetime`. None when get_call_details
    failed or the field was missing — the writer treats None as
    'leave the col C cell blank; backfill will fix it.'"""
    call_ended_at_utc: Optional[datetime] = None
    """Dialpad `date_ended`, same normalization. Plumbed but NOT written
    to Analyst_History in this phase — only fed to the
    `compute_call_duration` stub. Writing it to a dedicated column is a
    future-project decision."""
    transcript_display: List[dict] = []  # [{timestamp, speaker, text}, ...]
    moments_display: List[dict] = []     # [{timestamp, time, type, agent}, ...]
    # DispositionDesign §5 step 4 — verified CC facts stamped onto the
    # qa.evaluations row at Stage 1. None = CC never saw the call, or the
    # call was undispositioned (absence is expected-normal).
    dialpad_disposition_category: Optional[str] = None
    dialpad_disposition: Optional[str] = None
    ai_csat: Optional[float] = None
    # Durable join keys from get_call_details — the 006 eval columns
    # existed but were never populated, which left evals joinable only
    # by the per-leg id (the Stats export keys by ENTRY-POINT id).
    dialpad_entry_point_call_id: Optional[str] = None
    dialpad_master_call_id: Optional[str] = None
    # PulpoConnection §4.2 step 6 — RAG retrieval provenance:
    # [{id, title, score, score_kind, updated_at, open_flags}, ...].
    # Stamped in shadow AND on modes; [] when retrieval was off/skipped.
    pulpo_docs: List[dict] = []
    # TwoStageScoringDesign §3 — the Stage-A artifact (AnnotatedTranscript
    # dump, schema gemini_annotate_v1) + the model that produced it, for
    # the models_used audio-leg stamp. None when SCORING_PIPELINE=single
    # or annotation failed (annotation is never a scoring blocker).
    annotated_transcript: Optional[dict] = None
    annotator_model: Optional[str] = None
