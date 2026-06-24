-- ============================================================================
-- Migration 005 — DOWN
--
-- Drops in dependency order, no CASCADE: if migration 006 is still applied
-- (qa.evaluations FKs to command_center.calls), DROP TABLE calls will fail
-- — that's the fail-loud behavior we want. Operator must down 006 first.
-- Partial UNIQUE indexes drop automatically with their table.
-- ============================================================================

DROP TABLE IF EXISTS command_center.chiclet_events;
DROP TABLE IF EXISTS command_center.chiclets;
DROP TABLE IF EXISTS command_center.calls;
DROP TABLE IF EXISTS command_center.frequent_callers_cache;
DROP TABLE IF EXISTS command_center.dialpad_agents;
DROP TABLE IF EXISTS command_center.webhook_events;
