-- 0014_hr_export.sql — HR bonus export moves to Sandy (Sep 2026).
-- Railway's Sep 1 auto-run computed August from the ~90 leak evals (the
-- real August corpus is Sandy-born and never syncs to PG) — the payload
-- builder now lives worker-side and reads D1. This column carries the
-- team's hr_export block verbatim from backend/config/teams/*.json
-- (Railway's team-config shape): HR-visible section subset + labels +
-- excluded agents. Teams without a block 404 the endpoint — how Sales
-- stays dark until its HR-visible subset is decided (HRBonusSheet §8).
ALTER TABLE teams ADD COLUMN hr_export TEXT
    CHECK (hr_export IS NULL OR json_valid(hr_export));

UPDATE teams SET hr_export = json('{
  "sections": [
    {"id": "greeting",          "hr_label": "Greeting"},
    {"id": "caller_id",         "hr_label": "Caller ID"},
    {"id": "purpose",           "hr_label": "Purpose of the call"},
    {"id": "matching",          "hr_label": "Matching the moment"},
    {"id": "process_adherence", "hr_label": "Process Adherence"},
    {"id": "call_resolution",   "hr_label": "Call Resolution"},
    {"id": "comms",             "hr_label": "Communication"},
    {"id": "efficiency",        "hr_label": "Efficiency & Call Handling"},
    {"id": "cri",               "hr_label": "CRI"}
  ],
  "excluded_agents": [
    "maximiliano perez",
    "maximiliano.perez",
    "max pérez",
    "max perez"
  ]
}') WHERE id = 'member_support';
