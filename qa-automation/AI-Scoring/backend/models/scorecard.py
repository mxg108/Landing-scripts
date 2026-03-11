"""Pydantic models for the QA scorecard and pipeline."""

from typing import List, Optional
from pydantic import BaseModel


class ScorecardSection(BaseModel):
    id: str
    name: str
    score: Optional[int] = None        # 1-5 for numeric sections, null for Y/N
    score_type: str                     # "numeric" or "yn"
    yn_value: Optional[str] = None     # "Y", "N", "NA" — only for Y/N sections
    confidence: str                     # "high", "medium", "low"
    reasoning: str
    audio_dependent: bool = False
    flags: List[str] = []


class Scorecard(BaseModel):
    sections: List[ScorecardSection]
    call_summary: str = ""
    key_strengths: str
    opportunities: str


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
