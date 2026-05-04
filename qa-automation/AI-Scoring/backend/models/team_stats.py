from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class AgentRosterEntry(BaseModel):
    agent: str
    n: int
    mean: float
    std: float
    ewma: Optional[float] = None
    trend: str  # "improving", "declining", "flat"
    binary_pcts: dict[str, float] = Field(
        default_factory=dict,
        description="section_id -> Y%. Keys map to team_config.yn_history_ids.",
    )
    status: str  # "excellent", "good", "watch", "at_risk"
    weak_sections: list[str]
    is_active: bool
    supervisor: Optional[str] = None


class OutlierRecord(BaseModel):
    agent: str
    date: str  # ISO format
    score: float
    agent_median: float
    modified_z: float
    classification: str  # "exceptional", "concerning"
    eval_id: str = ""    # call_id from Dialpad link


class TrainingOpportunity(BaseModel):
    agent: str
    section: str
    agent_avg: float
    team_avg: float
    gap: float
    n: int
    priority: str  # "high", "medium", "low"


class SupervisorStats(BaseModel):
    supervisor: str
    avg_score: float
    std_score: float
    eval_count: int
    agent_count: int


class MonthlyPoint(BaseModel):
    month: str  # "YYYY-MM"
    mean: float
    count: int


class SPCData(BaseModel):
    months: list[MonthlyPoint]
    ucl: float
    lcl: float
    center: float


class SectionStats(BaseModel):
    team_means: dict[str, float]
    team_stds: dict[str, float]
    training_opportunities: list[TrainingOpportunity]


class BinaryAgentBreakdown(BaseModel):
    agent: str
    pct: float
    yes: int
    total: int


class BinarySectionStats(BaseModel):
    section_id: str
    label: str
    team_pct: float
    agents: list[BinaryAgentBreakdown]


class EWMAEntry(BaseModel):
    agent: str
    current_ewma: float
    trend: str  # "improving", "declining", "flat"
    eval_count: int


class DistributionBin(BaseModel):
    bin: str  # e.g. "91-100"
    count: int


class MailsEntry(BaseModel):
    agent_name: str
    email: str
    supervisor: Optional[str] = None
    canonical_name: Optional[str] = None


class TeamStatsResponse(BaseModel):
    """Envelope for /api/{team_id}/team/stats.

    Top-level metadata fields are load-bearing for the predictive era —
    they let downstream consumers (forecasters, drift detectors, parquet
    exporters) reason about whether two responses are comparable.

    schema_version: bump on any breaking shape change to this envelope.
    rubric_version: from team_config; bump when the rubric itself changes.
    coverage_regime: 'manager_sample' today (curated subset of calls);
                     'full_coverage' once Local AI scores 100% of calls.
                     Mixing the two regimes in one analysis will skew
                     statistics — consumers must check this field.
    """
    schema_version: str = "1.1"
    team_id: str
    rubric_version: str
    generated_at: datetime
    coverage_regime: str  # "manager_sample" | "full_coverage"

    kpis: dict
    roster: list[AgentRosterEntry]
    outliers: list[OutlierRecord]
    spc: SPCData
    section_analysis: SectionStats
    binary_stats: list[BinarySectionStats]
    supervisor_stats: list[SupervisorStats]
    ewma: list[EWMAEntry]
    distribution: list[DistributionBin]
    filters_applied: dict  # days, active_only, supervisor


class LongFormRow(BaseModel):
    """One row of the canonical long-form analytical shape.

    Long form is what every predictive / ML / cohort analysis will start
    from. Returned by GET /api/{team_id}/team/long_form.
    """
    agent: str
    eval_id: str
    timestamp: datetime
    supervisor: str = ""
    manager_email: str = ""
    section_id: str
    section_name: str
    score_type: str  # "numeric" | "yn" | "manual"
    score: object    # float for numeric/manual, str for yn


class LongFormResponse(BaseModel):
    schema_version: str = "1.1"
    team_id: str
    rubric_version: str
    generated_at: datetime
    coverage_regime: str
    rows: list[LongFormRow]
    filters_applied: dict
