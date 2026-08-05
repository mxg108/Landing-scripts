-- Migration 001 — initial mass-notifications schema (PRD §8).
-- Remote: applied via `sandy.py db migrate <APP_ID> "<stmt>"` per statement.
-- Local:  npx wrangler d1 execute mass-notifications --local --file ./migrations/001_init.sql

CREATE TABLE IF NOT EXISTS campaigns (
  id TEXT PRIMARY KEY,
  mode TEXT NOT NULL DEFAULT 'INDIVIDUAL',
  property_name TEXT NOT NULL,
  event_name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  config_json TEXT NOT NULL DEFAULT '{}',
  sms_enabled INTEGER NOT NULL DEFAULT 0,
  fetch_stats_json TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS recipients (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL,
  reservation_id TEXT,
  email TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  unit TEXT NOT NULL DEFAULT '',
  phone_e164 TEXT,
  phone_raw TEXT,
  segment_timezone TEXT,
  market_segment TEXT,
  agm_name TEXT,
  source TEXT NOT NULL DEFAULT 'warehouse',
  status TEXT NOT NULL DEFAULT 'PENDING',
  notes TEXT NOT NULL DEFAULT '',
  attach_ids_json TEXT,
  email_state TEXT NOT NULL DEFAULT 'pending',
  email_sent_at TEXT,
  sms_state TEXT NOT NULL DEFAULT 'off',
  sms_sent_at TEXT,
  sms_body TEXT,
  sms_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_recipients_campaign ON recipients(campaign_id);
CREATE INDEX IF NOT EXISTS idx_recipients_email ON recipients(email);

CREATE TABLE IF NOT EXISTS property_contacts (
  id TEXT PRIMARY KEY,
  property_id TEXT,
  property_name TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL,
  phone TEXT,
  title TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  active INTEGER NOT NULL DEFAULT 1,
  updated_by TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_property_contacts_property ON property_contacts(property_name);

CREATE TABLE IF NOT EXISTS movein_drafts (
  id TEXT PRIMARY KEY,
  reservation_id TEXT NOT NULL,
  property_id TEXT,
  fields_json TEXT NOT NULL DEFAULT '{}',
  contact_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft',
  created_by TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  row_states_json TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_campaign ON runs(campaign_id);

CREATE TABLE IF NOT EXISTS templates (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  config_json TEXT NOT NULL DEFAULT '{}',
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS app_config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
  email TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  granted_by TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
  run_id TEXT PRIMARY KEY,
  workflow_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  result TEXT,
  created_at TEXT NOT NULL
);

INSERT OR IGNORE INTO roles (email, role, granted_by, created_at)
  VALUES ('maximiliano.perez@hellolanding.com', 'admin', 'bootstrap', '2026-08-05T00:00:00Z');
