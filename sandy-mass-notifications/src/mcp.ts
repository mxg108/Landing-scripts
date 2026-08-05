// MCP server for the mass-notifications app — exposes campaign/recipient data
// to Sandy Agents and MCP clients at POST /api/v1/mcp. Read-only in v0.1.

import { createMcpHandler } from "agents/mcp";
import { z } from "zod";
import { createDb, campaigns, recipients } from "./db.js";
import { desc, eq } from "drizzle-orm";
import type { Env } from "./index.js";

export function mcpHandler(env: Env) {
  return createMcpHandler((server) => {
    server.tool(
      "list-campaigns",
      {
        limit: z.number().int().min(1).max(100).default(20).describe("Max campaigns to return, newest first"),
      },
      async ({ limit }) => {
        const db = createDb(env.DB);
        const rows = await db.select().from(campaigns).orderBy(desc(campaigns.created_at)).limit(limit);
        return { content: [{ type: "text", text: JSON.stringify(rows, null, 2) }] };
      }
    );

    server.tool(
      "get-campaign-recipients",
      {
        campaign_id: z.string().uuid().describe("campaigns.id"),
      },
      async ({ campaign_id }) => {
        const db = createDb(env.DB);
        const rows = await db.select().from(recipients).where(eq(recipients.campaign_id, campaign_id));
        return { content: [{ type: "text", text: JSON.stringify(rows, null, 2) }] };
      }
    );
  });
}
