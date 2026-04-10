from __future__ import annotations
from typing import Optional
from pydantic import BaseModel


class AgentRosterEntry(BaseModel):
    agent: str
    n: int
    mean: float
    std: float
    ewma: Optional[float] = None
    trend: str  # "improving", "declining", "flat"
    id_val_pct: float
    cust_res_pct: float
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
    team_pct: float
    agents: list[BinaryAgentBreakdown]


class BinaryStats(BaseModel):
    identity_validation: BinarySectionStats
    customer_resolution: BinarySectionStats


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
    kpis: dict  # total_evals, avg_score, std_score, analyst_count
    roster: list[AgentRosterEntry]
    outliers: list[OutlierRecord]
    spc: SPCData
    section_analysis: SectionStats
    binary_stats: BinaryStats
    supervisor_stats: list[SupervisorStats]
    ewma: list[EWMAEntry]
    distribution: list[DistributionBin]
    filters_applied: dict  # days, active_only, supervisor
