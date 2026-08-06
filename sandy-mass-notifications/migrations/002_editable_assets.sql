-- Migration 002 — audit columns for editable email assets (cards/disclaimers).
-- Seeding happens at runtime from emailkit SEED_CARDS / SEED_DISCLAIMERS.
ALTER TABLE templates ADD COLUMN updated_by TEXT;
ALTER TABLE templates ADD COLUMN updated_at TEXT;
