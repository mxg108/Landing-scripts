from datetime import datetime
from typing import Optional
from pydantic import BaseModel

class SectionScore(BaseModel):
    """Score details for a single scorecard section in evaluation history."""
    score: str               # "1"-"5" or "Y"/"N"/"N/A"
    confidence: Optional[str] = None  # "high", "medium", "low"
    reasoning: Optional[str] = None

class EvaluationRecord(BaseModel):
    """A single QA evaluation from Analyst_History."""
    timestamp: datetime
    agent_name: str
    agent_email: str
    manager_email: str
    overall_score: float
    sections: dict[str, SectionScore]
    eval_id: Optional[str] = None        # call_id extracted from dialpad_link
    dialpad_link: Optional[str] = None
    key_strengths: Optional[str] = None
    improvements: Optional[str] = None
    call_summary: Optional[str] = None
    caller_name: Optional[str] = None
    caller_phone: Optional[str] = None
    source: str = "manual"  # "manual", "ai", or "backfilled"
    # Call metadata (DispositionDesign §5 / PulpoConnection §4.2) — served
    # by PostgresProvider only; the SheetsProvider parity path leaves the
    # defaults, so parity comparisons must exclude these fields.
    dialpad_disposition_category: Optional[str] = None
    dialpad_disposition: Optional[str] = None
    ai_csat: Optional[float] = None      # Dialpad Ai estimate, NOT a survey
    call_duration_ms: Optional[int] = None
    dialpad_call_id: Optional[str] = None
    dialpad_entry_point_call_id: Optional[str] = None
    dialpad_master_call_id: Optional[str] = None
    sop_used: Optional[str] = None       # top SOP title injected at scoring
    pulpo_docs: list[dict] = []          # retrieval provenance footnotes

class SectionAssessment(BaseModel):
    """Gemini's assessment of a single section's progression."""
    trend: str               # "improving", "stable", "declining"
    summary: str
    coaching_tip: str

class ProgressionAssessment(BaseModel):
    """Gemini's overall progression assessment for an agent."""
    overall_assessment: str
    section_assessments: dict[str, SectionAssessment]
    evaluation_count: int
    time_range_days: int
    data_source: str         # "Google Sheets" or "PostgreSQL"
