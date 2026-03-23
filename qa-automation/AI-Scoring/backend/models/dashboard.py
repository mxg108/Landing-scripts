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
    dialpad_link: Optional[str] = None
    key_strengths: Optional[str] = None
    improvements: Optional[str] = None
    source: str = "manual"  # "manual", "ai", or "backfilled"

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
