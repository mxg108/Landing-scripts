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

> v0.8 : 2026-08-06
- P3 SMS companion (OpsVP): dispatch workflow v0.2 adds an SMS phase — one
  AI summary per campaign (AI Gateway, ≤320 chars, length-guarded, sentence
  truncate + flag), Dialpad sends from +14159804986, quiet hours 08:00–21:00
  local (MS_TIMEZONE Rails→IANA), GraphQL opt-out respect (graceful degrade).
  New kinds: sms_preview / sms_test (to any number) / sms_only (post-send
  retry). App: per-campaign SMS toggle, preview card with regenerate, test
  input, retry button, per-recipient SMS states in the grid; migration 003
  caches the preview on campaigns. SSE watcher upgraded to a change-signature
  model covering SMS operations. E2E: preview verified (185-char summary).

> v0.7 : 2026-08-06
- Frontend polish (operator feedback): custom navy app header + slim footer
  (no stock marketing dead links), OG GAS WebApp background (#E7EFFB);
  flash banners auto-clear from the URL (no stale "Fetch queued");
  recipient status is one pill-styled auto-saving select (duplicate chip +
  cramped Set button gone); Configure gains labeled Templates vs Card
  sub-panels with layman explanations; /edit icon field accepts plain
  emoji and stores Gmail-safe hex entities (round-trips back as emoji).

> v0.6 : 2026-08-06
- Editable email assets: cards + disclaimers live in D1 (templates table,
  seeded from emailkit SEED_* on first run), managed at **/edit** (list,
  editor with sample-token preview, active toggle; new cards/disclaimers
  become Configure chiclets). Card + disclaimer selection is now
  **instant-apply chiclets** (fixes select-without-save doing nothing).
  **SSE live status** (`/c/:id/events` + page watcher) replaces
  refresh-to-see-results during fetching/sending. Migration 002 adds
  templates.updated_by/updated_at.

> v0.5 : 2026-08-06
- P2 send pipeline: `emailkit.ts` (token engine with HTML-escape-by-default +
  {{html:}} escape hatch, body composer, 6 cards, 6 seeded templates — legacy
  parity), campaign Configure/Preview/Send UI, `mass-notify-dispatch` workflow
  (batched GAS dispatcher calls, per-recipient results, callback), runs audit
  with undo, dry-run drafts + test-send. E2E verified: draft created in
  member.support@ for a real Wayland recipient, D1 states updated end-to-end.

> v0.1 : 2026-08-05
- P1 recipient pipeline: campaigns CRUD, warehouse fetch via
  `mass-notify-fetch` (Snowflake `LANDING.CORE`, legacy Looker "Active
  Occupants" semantics, dedupe → REVIEW, E.164 normalization), recipients grid
  with field-level edits, RBAC (invisible SSO, request-access, admin grants),
  MCP read tools. Send pipeline lands in P2.
