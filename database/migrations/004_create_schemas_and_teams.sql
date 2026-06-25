-- ============================================================================
-- Migration 004: schema namespaces + public.teams + seed
-- Spec: database/SQLMigration.md §2, §6, §7.6
--
-- Creates the three logical-concern namespaces that live in a single Railway
-- Postgres instance:
--
--   qa              QA pipeline (Sheets-as-DB replacement, evaluations,
--                   analytics, formula compliance, audit logs)
--   command_center  Dialpad real-time state (webhook events, calls, chiclets)
--   embeddings      Model-agnostic RAG groundwork (LandGPT v2 SOP retrieval)
--
-- Also installs pgvector >= 0.7 (required for the `embeddings` schema) and
-- seeds the cross-cutting `public.teams` registry that every schema FKs to
-- via `team_id`.
--
-- Identity-column convention going forward: BIGINT GENERATED ALWAYS AS
-- IDENTITY rather than SERIAL/BIGSERIAL. Existing mass_notifications
-- tables are not retro-migrated.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS qa;
CREATE SCHEMA IF NOT EXISTS command_center;
CREATE SCHEMA IF NOT EXISTS embeddings;

CREATE EXTENSION IF NOT EXISTS vector;

-- ── public.teams ────────────────────────────────────────────────────────────
-- TEXT PK so config-driven references stay readable (`member_support`,
-- `sales`) — matches how team_id is already passed around the codebase.
--
-- `timezone` drives Command Center day-boundary resets (CC §6, §10).
-- `default_language` drives default embedding-model selection (§5.1, §5.4)
-- and is the per-team baseline used when call-level language detection
-- can't disambiguate.

CREATE TABLE IF NOT EXISTS public.teams (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    timezone         TEXT NOT NULL,
    default_language TEXT NOT NULL,
    active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed: only the two teams currently in production. Future team adds
-- happen via INSERT in a follow-up migration, not by editing this one.
INSERT INTO public.teams (id, name, timezone, default_language) VALUES
    ('member_support', 'Member Support', 'America/Mexico_City', 'en'),
    ('sales',          'Sales',          'America/Mexico_City', 'en')
ON CONFLICT (id) DO NOTHING;
