# mass-notifications (Sandy app)

Successor to the GAS Mass Notifications WebApp (`mass-notifications/` in this
repo). Property mass notifications for Member Support / ERT: resident email
campaigns fed by the Landing warehouse, a Dialpad SMS companion, and a
redesigned Move-In Flow to property contacts.

- **PRD:** `PRD.md` (this directory) — architecture, decisions D1–D7, rollout.
- **App:** `src/` — Sandy worker (SSR React, D1, Landing design system).
- **Workflows:** `workflows/mass-notify-fetch.js` — Snowflake recipient fetch.
- **Mail dispatcher:** `gas-dispatcher/` — GAS payload-mode sender, deployed
  under member.support@hellolanding.com (see its README for the runbook).
- **Migrations:** `migrations/` — D1 schema, applied via `sandy.py db migrate`.

App id `0b8bc7ad-9f12-40a2-9a73-44256d9d47ca` ·
live at https://mass-notifications.sandy.hellolanding.tech ·
fetch workflow `ffaa18b1-92a6-4eef-8219-c85c950c9068`.

## MCP Server (stateless)

`src/mcp.ts` exposes campaign data as read-only MCP tools at `/api/v1/mcp`:
`list-campaigns` (limit) and `get-campaign-recipients` (campaign_id).

## Change Log

> v0.1 : 2026-08-05
- P1 recipient pipeline: campaigns CRUD, warehouse fetch via
  `mass-notify-fetch` (Snowflake `LANDING.CORE`, legacy Looker "Active
  Occupants" semantics, dedupe → REVIEW, E.164 normalization), recipients grid
  with field-level edits, RBAC (invisible SSO, request-access, admin grants),
  MCP read tools. Send pipeline lands in P2.
