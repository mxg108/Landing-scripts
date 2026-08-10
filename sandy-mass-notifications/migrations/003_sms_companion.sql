-- Migration 003 — SMS companion (P3): per-campaign AI summary cache.
ALTER TABLE campaigns ADD COLUMN sms_preview_text TEXT;
ALTER TABLE campaigns ADD COLUMN sms_preview_truncated INTEGER NOT NULL DEFAULT 0;
