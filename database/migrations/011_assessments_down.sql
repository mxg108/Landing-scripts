-- ============================================================================
-- Migration 011 — DOWN
--
-- Drops both v1.4 tables in dependency order (sections → assessments).
-- The FKs from qa.assessments to qa.rubric_versions + qa.formula_versions
-- drop cleanly with the table; no reverse-alter needed on those tables.
--
-- Historical rows are lost if any exist — v1.4 shipped BEFORE Wave-2
-- application code wrote to these tables, so in normal prod state both
-- are empty and this is a no-op. If assessments have been generated,
-- operator should back up qa.assessments + qa.assessment_sections
-- before rolling back.
-- ============================================================================

DROP TABLE IF EXISTS qa.assessment_sections;
DROP TABLE IF EXISTS qa.assessments;
