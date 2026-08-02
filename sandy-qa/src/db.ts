// Database schema — define your tables here using Drizzle ORM.
// Add columns, create new tables, and keep this file as the single source of truth for your schema.

import { drizzle } from "drizzle-orm/d1";
import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

// Example table — replace or extend with your own schema
export const items = sqliteTable("items", {
  id: text("id").primaryKey(),
  name: text("name").notNull(),
  created_at: text("created_at").notNull(),
});

// Workflow runs — persists trigger + callback state for async Sandy Workflows.
// Insert a row at trigger time; update it when the callback arrives.
export const workflowRuns = sqliteTable("workflow_runs", {
  run_id: text("run_id").primaryKey(),
  workflow_name: text("workflow_name").notNull(),
  status: text("status").notNull().default("pending"), // pending | complete | error
  result: text("result"),                              // full callback body as JSON string
  created_at: text("created_at").notNull(),
});

// Helper: create a Drizzle client from the D1 binding
export function createDb(d1: D1Database) {
  return drizzle(d1, { schema: { items, workflowRuns } });
}
