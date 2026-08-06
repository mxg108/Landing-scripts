// Database schema — single source of truth for the mass-notifications D1 schema.
// Mirrors sandy-mass-notifications/PRD.md §8. Migrations are run via
// `sandy.py db migrate` (remote) and `npx wrangler d1 execute --local` (dev);
// keep both in sync with this file.

import { drizzle } from "drizzle-orm/d1";
import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

// One row per campaign (a property/event send). Legacy parity: mode strings
// INDIVIDUAL | MOVE_IN (BCC retired per PRD D5).
export const campaigns = sqliteTable("campaigns", {
  id: text("id").primaryKey(),
  mode: text("mode").notNull().default("INDIVIDUAL"),
  property_name: text("property_name").notNull(),
  event_name: text("event_name").notNull().default(""),
  status: text("status").notNull().default("draft"), // draft|fetching|ready|sending|complete|errored|undone
  config_json: text("config_json").notNull().default("{}"),
  sms_enabled: integer("sms_enabled").notNull().default(0),
  fetch_stats_json: text("fetch_stats_json"),
  created_by: text("created_by").notNull(),
  created_at: text("created_at").notNull(),
  completed_at: text("completed_at"),
});

// One row per recipient per campaign. Eligibility statuses keep legacy strings:
// '' | PENDING | READY eligible; REVIEW | DRAFT | SENT blocked.
export const recipients = sqliteTable("recipients", {
  id: text("id").primaryKey(),
  campaign_id: text("campaign_id").notNull(),
  reservation_id: text("reservation_id"),
  email: text("email").notNull(),
  name: text("name").notNull().default(""),
  unit: text("unit").notNull().default(""),
  phone_e164: text("phone_e164"),
  phone_raw: text("phone_raw"),
  segment_timezone: text("segment_timezone"),
  market_segment: text("market_segment"),
  agm_name: text("agm_name"),
  source: text("source").notNull().default("warehouse"), // warehouse|manual|csv
  status: text("status").notNull().default("PENDING"),
  notes: text("notes").notNull().default(""),
  attach_ids_json: text("attach_ids_json"),
  email_state: text("email_state").notNull().default("pending"), // pending|sent|error|skipped
  email_sent_at: text("email_sent_at"),
  sms_state: text("sms_state").notNull().default("off"), // off|queued|sent|error|skipped_optout|skipped_quiet_hours
  sms_sent_at: text("sms_sent_at"),
  sms_body: text("sms_body"),
  sms_error: text("sms_error"),
});

// Per-property contact book (Move-In Flow). Seeded from
// DIMPROPERTY.PROPERTY_CONTACT_*_1..6, operator-curated thereafter.
export const propertyContacts = sqliteTable("property_contacts", {
  id: text("id").primaryKey(),
  property_id: text("property_id"),
  property_name: text("property_name").notNull(),
  name: text("name").notNull().default(""),
  email: text("email").notNull(),
  phone: text("phone"),
  title: text("title"),
  source: text("source").notNull().default("manual"), // warehouse|manual
  active: integer("active").notNull().default(1),
  updated_by: text("updated_by"),
  updated_at: text("updated_at").notNull(),
});

// Move-In Flow drafts — autosaved, survive reloads and handoffs.
export const moveinDrafts = sqliteTable("movein_drafts", {
  id: text("id").primaryKey(),
  reservation_id: text("reservation_id").notNull(),
  property_id: text("property_id"),
  fields_json: text("fields_json").notNull().default("{}"),
  contact_ids_json: text("contact_ids_json").notNull().default("[]"),
  status: text("status").notNull().default("draft"), // draft|sent|archived
  created_by: text("created_by").notNull(),
  updated_at: text("updated_at").notNull(),
});

// Audited runs: every send, dry-run, test, undo — legacy Run_Log successor.
export const runs = sqliteTable("runs", {
  id: text("id").primaryKey(),
  campaign_id: text("campaign_id").notNull(),
  kind: text("kind").notNull(), // send|dryrun|test|undo|fetch
  actor: text("actor").notNull(),
  count: integer("count").notNull().default(0),
  row_states_json: text("row_states_json"),
  started_at: text("started_at").notNull(),
  completed_at: text("completed_at"),
  error: text("error"),
});

// Editable email assets — cards and disclaimers (kind='card'|'disclaimer'),
// seeded from emailkit SEED_* on first run, curated at /edit. No redeploys.
export const templates = sqliteTable("templates", {
  id: text("id").primaryKey(),
  name: text("name").notNull(),
  kind: text("kind").notNull(), // card|disclaimer (email templates + prompts later)
  config_json: text("config_json").notNull().default("{}"),
  active: integer("active").notNull().default(1),
  updated_by: text("updated_by"),
  updated_at: text("updated_at"),
});

export const appConfig = sqliteTable("app_config", {
  key: text("key").primaryKey(),
  value: text("value").notNull(),
});

// RBAC: invisible-SSO roles. Absent email → request-access screen.
export const roles = sqliteTable("roles", {
  email: text("email").primaryKey(),
  role: text("role").notNull(), // admin|operator|requested
  granted_by: text("granted_by"),
  created_at: text("created_at").notNull(),
});

// Workflow runs — trigger + callback staging (template pattern).
export const workflowRuns = sqliteTable("workflow_runs", {
  run_id: text("run_id").primaryKey(),
  workflow_name: text("workflow_name").notNull(),
  status: text("status").notNull().default("pending"), // pending | complete | error
  result: text("result"),
  created_at: text("created_at").notNull(),
});

export function createDb(d1: D1Database) {
  return drizzle(d1, {
    schema: {
      campaigns, recipients, propertyContacts, moveinDrafts,
      runs, templates, appConfig, roles, workflowRuns,
    },
  });
}
