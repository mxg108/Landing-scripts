-- ============================================================================
-- Migration 004 — DOWN
--
-- Removes pgvector + the three namespace schemas + public.teams.
-- Note: DROP EXTENSION vector fails if any column of type vector still
-- exists (i.e. a later embeddings migration is still applied). The runner
-- applies migrations in order; rollback should also be in reverse order.
-- The fail-loudly behavior here is intentional — a partial rollback that
-- leaves orphan VECTOR columns would silently break HNSW indexes later.
-- ============================================================================

DROP SCHEMA IF EXISTS embeddings CASCADE;
DROP SCHEMA IF EXISTS command_center CASCADE;
DROP SCHEMA IF EXISTS qa CASCADE;

DROP EXTENSION IF EXISTS vector;

DROP TABLE IF EXISTS public.teams;
