# TeamStatsBoard — Team-Level Statistical Analytics for Agent Progression Dashboard

## Overview

This document specifies the team-level statistical analytics layer to be added to the existing **Agent Progression Dashboard** (`localhost:8000/dashboard`). The current dashboard shows per-agent, per-timeframe views with basic summary stats (eval count, average score, latest score, highest score), an overall score trend line, per-section bar averages, and a Gemini-powered AI Progression Assessment.

**TeamStatsBoard** changes current dashboard rout to /dashboard/{{agent}}, adds a new route/view (dashboard/{{team}}, in this case member-support) with team-wide analytics, statistical outlier detection, EWMA-based trend monitoring, per-supervisor aggregation, section weakness mapping, and a training opportunity identifier. It reads from the same `Analyst_History` tab that powers the current per-agent dashboard.

---

## 1. Existing Architecture Context/Issues

## Optimization opportunities

 1. Gemini per-agent assessments are not persistent. They are not stored in memory; if we return to a timeframe we've previously checked, a new API call will be created and another $0.15 will be spent to re-generate redundant information we already know about an agent or a whole team.
 2. We send Gemini API requests too frequently every time we check agent stats, $0.15 per 1M context Window can scale pretty quickly, getting AI summaries per agent or per team needs to be a deliberate, counscious choice the operator makes, not the default pipeline.

**Decision (2026-03-24)**: Both agent and team views show cold stats by default. Gemini AI summaries are opt-in via a "Get AI Summary" button on the agent view only. No Gemini integration on the team view for v1.

### Known Data Quality Issues

- **Duplicate agent names**: `Abegail` / `Abegail Tremoya`, `Alfredo León` / `Alfredo Leon`, `Jorge Barrón` / `Jorge Barron`, `Nitzia` / `Nitzia Pedraza`
- **Manager email casing**: `Fran.bernal@` vs `fran.bernal@`
- **Test/zero-score rows**: Some `Maximiliano Pérez` entries with Overall Score = 0 (self-test entries — exclude)
- **Epoch timestamps**: Two rows with NaN agent name and 1969-12-31 timestamp (exclude)

**The new code must normalize all of these on load.**

---

## 2. Active Analyst Roster

Only analysts whose names and emails are the sheet tab "Mails" in columns A and B respectively are considered "active." The current 21 active analysts (as of March 2026):

```
Abegail Tremoya, Alfredo Leon, Andrés Castella, Bruno Riquelme,
Cassandra Cervantes, Daniela Solares, David Santiago, Fernanda Acosta,
Gustavo Herrera, Israel Valencia, Ivan García, Javier Martínez,
Jhona Arcilla, Jorge Barron, Luis Ortuzar,
Melissa Quintanilla, Mich Palacios, Nitzia Pedraza,
Renee Valencia, Rubio Rivera, Selenne Manriquez
```

**Implementation note**: Active status should be derived dynamically (any eval with timestamp in the current year), not hardcoded. The roster filter should allow toggling between "Active only" and "All analysts."

---

## 3. Statistical Methods to Implement

### 3.1 Modified Z-Score Outlier Detection

**Purpose**: Flag individual evaluations that deviate significantly from an agent's personal baseline. More robust than standard Z-scores for the skewed distributions typical of QA scoring data.

**Formula**:
```
Modified Z = 0.6745 × (score - median) / MAD
```
Where `MAD = median(|score_i - median|)` (Median Absolute Deviation).

**Parameters**:
- Minimum evals per agent: 5 (skip agents below this)
- Threshold: |Modified Z| > 3.5
- Classification: positive Z = "Exceptional", negative Z = "Concerning"

**Output per outlier record**: agent name, eval date, score, agent median, modified Z value, classification tag.

**Validation note**: 27 outliers were detected in the full dataset as of March 2026. If the implementation finds a significantly different count, investigate.

### 3.2 EWMA (Exponentially Weighted Moving Average)

**Purpose**: Smooth out per-agent score trajectories to detect sustained performance shifts — more responsive to recent change than a simple rolling average, and the standard tool in SPC (Statistical Process Control) for detecting small, persistent drift.

**Formula**:
```
EWMA_t = α × score_t + (1 - α) × EWMA_{t-1}
α = 2 / (span + 1)
```

**Parameters**:
- Span: 5 evaluations
- Minimum evals to compute: 3
- Trend: difference between current EWMA and EWMA from 5 evals ago

**Output per agent**: current EWMA value, trend (positive/negative/flat), full EWMA series for charting.

**Rendering**: Horizontal bar chart sorted by EWMA ascending (weakest at top — the people who need attention first). Color code: green ≥ 90, blue ≥ 80, amber ≥ 70, red < 70.

### 3.3 SPC Control Chart (Team Monthly)

**Purpose**: Detect whether a given month's team-wide average is a statistical signal or just noise. Uses Shewhart-style ±2σ control limits computed from the monthly averages themselves.

**Implementation**:
1. Group all evals by calendar month
2. Compute monthly mean overall score
3. Compute center line (grand mean of all monthly means) and σ (standard deviation of monthly means)
4. UCL = center + 2σ, LCL = center - 2σ
5. Months outside limits are signals worth investigating

**Rendering**: Line chart with monthly average, UCL line (dashed red), LCL line (dashed red), center line (dashed gray). Points outside limits should be visually highlighted.

### 3.4 Per-Section Weakness Analysis

**Purpose**: Identify which QA rubric sections are weakest team-wide and which agents have specific section-level training needs.

**Sections** (8 scored on 1–5 scale):
```python
SECTIONS = [
    'Greeting',
    'Purpose of the Call',
    'Matching the Moment',
    'Process Adherence',
    'Call Resolution',
    'Communication',
    'Efficiency & Call Handling',
    'Documentation'
]
```

**Team-level reference values** (from full dataset, ~1144 evals):

| Section | Team Mean | Team Std |
|---------|-----------|----------|
| Greeting | 4.83 | 0.60 |
| Purpose of the Call | 4.06 | 0.91 |
| Matching the Moment | 3.96 | 0.93 |
| Process Adherence | 4.12 | 1.06 |
| Call Resolution | 4.21 | 1.09 |
| Communication | 4.12 | 0.93 |
| Efficiency & Call Handling | 4.00 | 1.15 |
| Documentation | 3.87 | 1.50 |

**Training opportunity detection**: An agent has a "weakness" in a section when their average for that section is more than 0.5 below the team average. Output sorted by gap size, with priority tags:
- Gap ≥ 1.5: **High priority**
- Gap ≥ 1.0: **Medium priority**
- Gap < 1.0: **Low priority**

### 3.5 Per-Supervisor Aggregation

**Purpose**: Compare team performance across supervisors to surface systemic patterns (e.g., one supervisor's cohort consistently scoring lower may indicate a training methodology gap rather than individual agent issues).

**Supervisor mapping**: Read from Mails sheet column C (agent → supervisor assignment). This is the source of truth, not derived from Manager Email frequency.

**Output**: Average score, standard deviation, eval count, unique agent count per supervisor.

---

## 4. Route & Template Structure

### Route Structure (decided)

```
GET /dashboard                    → Team dashboard (landing page)
GET /dashboard/agent/{name}       → Per-agent drill-down view
GET /api/team/stats?days=90       → Team stats JSON (all computations)
GET /api/team/mails               → Active roster from Mails sheet
```

The existing per-agent API endpoints remain:
```
GET /api/agents                           → agent name list
GET /api/agents/{name}/history?days=30    → eval records
GET /api/agents/{name}/progression?days=30 → Gemini assessment (opt-in)
```

### Navigation

- `/dashboard` (team view) header has agent dropdown or roster table links
  that navigate to `/dashboard/agent/{name}`
- `/dashboard/agent/{name}` header has a "Team View" link back to `/dashboard`
- Gemini AI summaries are NOT auto-triggered on either view. Both views show
  cold stats by default. A "Get AI Summary" button triggers the Gemini call
  on the agent view. No Gemini on the team view for v1.

### Template: `team_dashboard.html`

**Layout** — Five tab panels, matching the analysis we prototyped:

1. **Team Overview** — KPI row (total evals, avg score, std dev, analyst count) + monthly SPC control chart + score distribution histogram + EWMA bar chart
2. **Agent Roster** — Sortable table of all (or active-only) agents with: eval count, mean, std, EWMA, trend, identity validation %, status tag, weak sections
3. **Outlier Detection** — Table of flagged evaluations with agent, date, score, agent median, modified Z, type tag
4. **Section Analysis** — Team section averages bar chart + section variability bar chart + training opportunities table
5. **Supervisor View** — Supervisor average comparison chart + supervisor summary table

### Filters (top bar, applies to all tabs)

- **Scope toggle**: Active analysts / All analysts
- **Supervisor filter**: Dropdown populated from unique manager names in Column C in "Mails"
- **Time range**: All time / Last 6 months / Last 3 months / Last month

---

## 5. Backend Module: `team_stats.py`

Create a new module `team_stats.py` alongside the existing `scoring_service.py`. This module owns all statistical computation and returns plain Python dicts/lists that the route passes to the template.

### Public API

```python
def load_and_clean(sheet_data: list[list]) -> pd.DataFrame:
    """
    Takes raw rows from gspread, returns a cleaned DataFrame with:
    - Normalized agent names (merge duplicates)
    - Lowercased manager emails
    - Parsed timestamps (exclude pre-2020)
    - Excluded test rows (Maximiliano Pérez zeros, NaN agents)
    - 'is_active' boolean column (eval in current year)
    """

def compute_outliers(df: pd.DataFrame, min_evals: int = 5, threshold: float = 3.5) -> list[dict]:
    """
    Per-agent modified Z-score outlier detection.
    Returns list of dicts with keys:
        agent, date, score, agent_median, modified_z, classification
    """

def compute_ewma(df: pd.DataFrame, span: int = 5, min_evals: int = 3) -> dict[str, dict]:
    """
    Per-agent EWMA computation.
    Returns {agent_name: {current_ewma, trend, dates, scores, ewma_series}}.
    """

def compute_monthly_spc(df: pd.DataFrame) -> dict:
    """
    Monthly team average with control limits.
    Returns {months, means, ucl, lcl, center, counts}.
    """

def compute_section_analysis(df: pd.DataFrame) -> dict:
    """
    Team-level section stats + per-agent weakness detection.
    Returns {
        team_means: {section: float},
        team_stds: {section: float},
        training_opportunities: [{agent, section, agent_avg, team_avg, gap, n, priority}]
    }.
    """

def compute_supervisor_stats(df: pd.DataFrame) -> list[dict]:
    """
    Per-supervisor aggregation.
    Returns [{supervisor, avg_score, std_score, eval_count, agent_count}].
    """

def compute_agent_roster(df: pd.DataFrame, active_only: bool = True) -> list[dict]:
    """
    Summary row per agent for the roster table.
    Returns [{agent, n, mean, std, ewma, trend, id_val_pct, status, weak_sections, is_active}].
    """
```

### Dependencies

- `pandas` (already used in the project or add to requirements)
- `numpy` (for median, MAD, EWMA computation)
- No new external libraries needed — all stats are implemented from scratch using numpy

---

## 6. Frontend Charting

Use **Chart.js** (already loaded in the existing dashboard). Specific chart configs:

### Monthly SPC Chart
- Type: `line`
- Datasets: Monthly avg (solid blue, filled), UCL (dashed red), LCL (dashed red), Center (dashed gray)
- Y-axis: min 60, max 100
- Legend: bottom

### Score Distribution
- Type: `bar`
- Bins: 0-20, 20-30, 30-40, 40-50, 50-60, 60-70, 70-80, 80-90, 90-100
- Colors: gradient red → amber → blue → green by bin

### EWMA Bar Chart
- Type: horizontal `bar`
- Sorted ascending (weakest at top)
- Color-coded: green ≥ 90, blue ≥ 80, amber ≥ 70, red < 70
- Tooltip: show trend value and eval count

### Section Averages
- Type: `bar`
- Y-axis: min 3, max 5
- Single dataset, uniform color

### Section Variability
- Type: `bar`
- Color-coded by severity: red if std > 1.1, amber if > 0.9, green otherwise

### Supervisor Comparison
- Type: `bar`
- Y-axis: min 60, max 100

---

## 7. Status Tags & Visual Language

Reuse across roster table, outlier table, and training table:

| Tag | Condition | Background | Text Color |
|-----|-----------|------------|------------|
| Excellent | EWMA ≥ 90 | `#dcfce7` | `#166534` |
| Good | EWMA ≥ 80 | `#dbeafe` | `#1e40af` |
| Watch | EWMA ≥ 70 | `#fef3c7` | `#92400e` |
| At risk | EWMA < 70 | `#fee2e2` | `#991b1b` |
| Exceptional | Positive outlier | `#dcfce7` | `#166534` |
| Concerning | Negative outlier | `#fee2e2` | `#991b1b` |
| High priority | Training gap ≥ 1.5 | `#fee2e2` | `#991b1b` |
| Medium priority | Training gap ≥ 1.0 | `#fef3c7` | `#92400e` |
| Low priority | Training gap < 1.0 | `#dbeafe` | `#1e40af` |

Trend indicators: `▲` (green) for trend > +3, `▼` (red) for trend < -3, `●` (gray) for flat.

---

## 8. Data Normalization

### Mails Sheet Layout (source of truth for roster)

| Col | Field |
|-----|-------|
| A | Agent Name (as entered in forms) |
| B | Agent Email |
| C | Supervisor (manager name or email) |
| D | Canonical Name (normalized display name) |

### Normalization in `load_and_clean()`

1. Read the Mails sheet via `SheetsProvider._get_mails_sheet()`
2. Build a canonical name map from col A → col D (only where col D is non-empty)
3. Apply the map to Analyst_History agent names during load
4. For agents not in the map, use the raw name as-is
5. Manager email: `.str.lower()` on the entire column
6. Active analyst = present in Mails sheet col A (after normalization)
7. Supervisor = looked up from Mails col C by canonical name

This replaces the hardcoded `AGENT_NAME_NORMALIZATION` dict — the mapping lives
in the sheet, so ops can fix duplicates without a code change.

Exclusion rules:
- Drop rows where `Agent Name` is NaN
- Drop rows where `Timestamp` parses to before 2020
- Drop rows where `Agent Name == 'Maximiliano Pérez'` and `Overall Score == 0`

---

## 9. Performance Considerations

- The Analyst History sheet currently has ~1,150 rows and grows by ~80–100/month. At this scale, loading the full sheet on each request is fine.
- All statistical computations run in < 100ms on 1,150 rows — no caching needed yet.
- If the sheet grows past ~5,000 rows, consider caching the cleaned DataFrame in-memory with a 5-minute TTL, similar to the existing `_jobs` dict pattern.

---

## 10. Testing Checklist

After implementation, verify:

- [ ] Name normalization merges all duplicate variants (check agent count = 42 for all, 24 for active)
- [ ] EWMA chart shows 20+ active agents with bars (agents with < 3 evals excluded)
- [ ] Outlier table shows ~27 records when viewing all time, all agents
- [ ] SPC chart shows 14 monthly data points (Feb 2025 → Mar 2026)
- [ ] Documentation section shows as weakest team average (~3.87)
- [ ] Efficiency & Call Handling shows as second-weakest (~4.00)
- [ ] Documentation shows highest variability (std ~1.50)
- [ ] Training opportunities table lists Sebastian Parga with weak sections in Process Adherence, Communication, Efficiency
- [ ] Supervisor filter correctly subsets all charts/tables
- [ ] Time range filter correctly subsets all charts/tables
- [ ] "Active only" toggle reduces agent roster to 24
- [ ] Navigation between `/dashboard` (per-agent) and `/dashboard/team` works bidirectionally

---

## 11. Future Extensions (Not in Scope for v1)

These were discussed in the analysis phase and should be the next iterations:

- **Change Point Detection (PELT / Bayesian Online CPD)**: Automatically identify _when_ an agent's performance regime shifted, answering "did the retraining in week 12 actually work?"
- **Linear Mixed-Effects Models**: `score ~ week + (1 + week | agent) + (1 | supervisor)` to properly decompose agent-level vs supervisor-level vs team-level effects. Requires `statsmodels`.
- **Predictive Risk Scoring**: Logistic regression or decision tree to flag agents likely to fall below threshold next month based on rolling mean, variance, trend, section profile, and time since last coaching.
- **Clustering (GMM)**: Agent performance profile segmentation — identify natural groupings rather than arbitrary threshold buckets.
- **CUSUM Charts**: More sensitive than EWMA to small sustained shifts, good for post-coaching monitoring.
