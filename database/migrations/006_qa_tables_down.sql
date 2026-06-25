-- ============================================================================
-- Migration 006 — DOWN
--
-- Drops in reverse FK-dependency order, no CASCADE. The FK from
-- qa.evaluations.command_center_call_id → command_center.calls.id is
-- declared on qa.evaluations side, so dropping qa.evaluations also
-- drops that constraint cleanly without touching command_center.calls.
-- ============================================================================

DROP TABLE IF EXISTS qa.agent_stat_points;
DROP TABLE IF EXISTS qa.api_audit_log;
DROP TABLE IF EXISTS qa.score_audit_archive;
DROP TABLE IF EXISTS qa.score_audit;
DROP TABLE IF EXISTS qa.formula_compliance_sweeps;
DROP TABLE IF EXISTS qa.evaluation_sections;
DROP TABLE IF EXISTS qa.evaluations;
DROP TABLE IF EXISTS qa.formula_versions;
DROP TABLE IF EXISTS qa.agents;
