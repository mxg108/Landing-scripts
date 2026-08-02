# Sandy App - {APP_NAME}

Add summary information about the Sandy App in this section.

## MCP Server (stateless)

`src/mcp.ts` exposes your D1 data as MCP tools at `/api/v1/mcp`.

**Endpoint:** `https://<your-app>.sandy.hellolanding.tech/api/v1/mcp`

**Built-in tools:**
- `list-items` — query D1 items table (accepts `limit`)
- `add-item` — insert a new row (accepts `name`)

**To add tools:** add `server.tool(name, schema, handler)` calls in `src/mcp.ts`. Each handler has access to `env.DB` via closure.

**To remove:** delete `src/mcp.ts` and remove the import + route block in `src/index.tsx`.

## Change Log

> {APP_VERSION} : {YYYY-MM-DD} {HH:MM}
- ADD CHANGELOG NOTES HERE

> {APP_VERSION} : {YYYY-MM-DD} {HH:MM}
- ADD MORE INCREMENTAL CHANGELOG NOTES HERE 
