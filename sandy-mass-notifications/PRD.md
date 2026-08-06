# PRD — Mass Notifications on Sandy (`mass-notifications`)

**Status:** v1.1 — decisions ratified 2026-08-05 (Q1–Q6 answered by Max; integration paths validated live)
**Owner:** Max Pérez García
**Predecessor:** `mass-notifications/` (GAS v3.3.1, container-bound WebApp + Sheets, Looker-fed)
**Sandy app name:** `mass-notifications` (verified free in `apps list`; 18 chars, valid)

---

## 1. Summary & Motivation

The Mass Notifications system lets Member Support / ERT notify every active resident of a property about events (fire inspections, outages, maintenance, weather, parking changes) by email, and notify property managements about incoming members ("Move-In Flow"). Since launch it has reached **12,000+ members across 620+ campaigns** (per #ert-member-support, 2026-06-17).

Today it is a Google Apps Script WebApp bolted to a Google Sheet, fed by **Looker** (being retired as SOT in favor of **Sigma**), sending via the deployer's personal Gmail, with audit split across a Run_Log sheet and a Railway Postgres. It has a single-point-of-failure deployment (broke org-wide on 2026-07-24 — "the Mass Notification System is broken 💔"), no real RBAC, and a manual SMS side-channel (Hub Managers hand-send SMS on request).

This project rebuilds it as a **Sandy app** with:

1. **Campaign mode** (parity + fixes): recipients from the **Landing warehouse (Snowflake `LANDING.CORE` dims — the same source Looker and the Sigma workbook sit on)**, per-member email sends, dry-run/test/undo, full D1 audit. The Sigma workbook **Member Information/Emails** stays the human-facing parity reference.
2. **SMS companion** (new, OpsVP request): an AI-summarized version of the email body sent to each member via **Dialpad SMS** from the verified Member Support line.
3. **Move-In Flow mode** (redesigned, own UI): operator picks a reservation, all member data auto-fills from **Landing Admin (GraphQL)** + warehouse, operator corrects/confirms, email goes to **property contacts seeded from the warehouse** rendered in a live mock of the actual email.
4. **Slack intake continuity**: the existing #ert-member-support Slack Workflow form ("What needs to be notified? / What Property will be affected? / …") remains the request channel; v1 accepts its `property_name` + body as campaign seed via prefill link. A Sandy Agent listening in-channel is pinned as a later robustness/accessibility upgrade (D6).

---

## 2. Current System (condensed)

Full functional map produced 2026-08-05 (session artifact); source of truth is `mass-notifications/src/`.

| Aspect | Today |
|---|---|
| Modes | `INDIVIDUAL`, `BCC`, `MOVE_IN` (+ dry-run drafts, test-send, UNDO) |
| Recipients | Looker inline query on `dimreservation` (dashboard 4552 "Active Occupants" replica), filtered by `dimproperty.property_name`, platform=Landing, checked-in, not checked-out, currently occupied. Also CSV import / TSV paste / manual |
| Fields from Looker | user_email, user_full_name, unit_number, occ_name, user_phone, reservation_id, check_in/out dates, property_name, 4× applicant alt-emails |
| Dedup | group by email; 2nd occupant moved to alt applicant email; 3+ → `REVIEW` |
| Templating | `{{token \| fallback}}` engine; 7 seeded templates; 7 "notification cards" (HTML blocks, Landing palette); optional branded wrapper (bg color + header img) |
| Send | `GmailApp` as deployer; CC manager; 90/batch BCC; 500 max/run; Drive attachments (folder-expansion, Google-files→PDF, 20MB cap) |
| Statuses | `''`/`PENDING`/`READY` eligible → `SENT`/`DRAFT`/`REVIEW` blocked |
| Audit | Run_Log sheet (RowStates JSON, config snapshot) + Railway Postgres `mass_notifications.{campaigns,recipients}` (Move-In **not** archived — TODO in code) |
| Move-In Flow | Separate sheet, hand-entered rows, one email per reservation to hand-entered "Property Email(s)", MoveInCard body, background-check + ID attachments |
| Auth | Domain-wide access, zero roles; executes as deployer |
| SMS | None — Hub Managers manually send SMS on request (Slack, 2026-07-24) |

### Known defects the rewrite fixes by design
(numbering from the audit)
1. WebApp save wipes `STATUS`/`NOTES`/`ATTACH_IDS` → Looker `REVIEW` guard defeated.
2. Restore-from-log corrupts Move-In rows (resident-schema-only).
3. Run log writes to `getLastRow()` → concurrent-run corruption.
4. Config validation can never hard-fail; warnings auto-pass headless.
5. Tokens injected as raw unescaped HTML (except MoveInCard).
6. Server email validation is `.includes('@')`.
7. INDIVIDUAL mode doesn't dedupe.
9. BCC marks deduped duplicates `SENT` (moot — BCC retired, D5).
11. Test sends and BCC drafts leave no audit trail.
12. `ALLOWALL` iframe embedding (clickjackable).
Plus: Move-In campaigns never archived to DB; personal Imgur URL as production header image; single-account Gmail quota and identity.

---

## 3. Decisions log (2026-08-05)

| # | Decision |
|---|---|
| D1 | **Snowflake gateway is the recipient/data path, not Sigma's REST API.** Looker and Sigma sit on the same warehouse; go straight to the source. Sigma API credentials become a *later, optional* upgrade (workbook-parity guarantees) once the pilot exists as leverage. |
| D2 | Property contacts: **warehouse-seeded** (see §5.2 — `DIMPROPERTY.PROPERTY_CONTACT_*` discovered during validation), operator-curated in D1. The eng request to enable `Contact.name/email/phone_number` in GraphQL is escalated but **not a dependency**. |
| D3 | Email sender identity: **member.support@hellolanding.com** (Member Support general inbox). Max holds credentials; the GAS dispatcher deploys under it. No dependence on a personal account. |
| D4 | SMS sends from Dialpad number **+1 (415) 980-4986** — verified Member Support Line; API send path already exercised by Max. |
| D5 | **BCC mode retired.** Individual + dedupe covers it with better audit. Historical `SEND_BCC` runs remain readable in backfilled history. |
| D6 | Slack intake: **prefill-link in v1**; a Sandy Agent listening in #ert-member-support is pinned as a post-v1 upgrade for robustness and company-wide accessibility. |
| D7 | Member phone SOT: **`DIMUSER.USER_PHONE`** (E.164-normalized at ingest). Dialpad's contacts API was evaluated and rejected: an exact-email search for a known member returned only unrelated fuzzy matches from the shared pool — members are not reliably in Dialpad's directory. |

## 4. Goals / Non-Goals

**Goals**
- G1: Replace Looker with the **warehouse dims via the Snowflake MCP gateway** as recipient SOT (Sigma workbook = parity reference); property + horizon parameterized.
- G2: Full campaign lifecycle on Sandy: fetch → curate → configure → preview → dry-run/test → send → audit → undo.
- G3: **Dialpad SMS companion** with AI-summarized body (OpsVP), respecting timezone quiet hours (`MS_TIMEZONE` via `DIMMARKETSEGMENT`).
- G4: **Move-In Flow as a first-class mode** with its own email-mock UI, reservation picker, Admin+warehouse auto-fill, warehouse-seeded per-property contacts.
- G5: Durable, queryable audit in the app's D1 (one SOT, no sheet/DB split; Move-In included).
- G6: RBAC via Sandy SSO identity (operator/admin roles, same invisible-SSO pattern as qa-scoring v0.24).
- G7: Retire the GAS WebApp + Sheet + Looker client after cutover.

**Non-Goals**
- No new email vendor: sending stays Gmail-based via the proven **GAS dispatcher (payload mode)** pattern (qa-scoring v0.22, live since #177), deployed as member.support@.
- No BCC mode (D5).
- No member-facing surfaces; internal operators only.
- No Sigma REST integration in v1 (D1); no dependency on eng enabling GraphQL `Contact` fields (D2).
- The Slack Workflow form itself is unchanged (owned in Slack); we only consume its output.

---

## 5. Architecture

```
                       ┌────────────────────────────────────────────┐
 Slack WF form ──────► │  Sandy App: mass-notifications (React SSR) │
 (#ert-member-support, │  UI: Campaigns · Move-In · History · Admin │
  prefill link v1)     │  D1: campaigns/recipients/contacts/config  │
                       └───────┬─────────────────────────┬──────────┘
                               │ trigger                 │ read (auto-auth)
                               ▼                         ▼
                  ┌─────────────────────────┐   Landing GraphQL API
                  │ Sandy Workflow:         │   api.hellolanding.com/api/v1/graphql
                  │ mass-notify-dispatch    │   (X-API-TOKEN injected for the app;
                  │  · Snowflake fetch      │    LANDING_API_GRAPHQL_KEY for the WF)
                  │  · AI Gateway (2 calls) │
                  │  · GAS mail dispatch ───┼──► GAS dispatcher (payload mode)
                  │  · Dialpad SMS ─────────┼──►   as member.support@hellolanding.com
                  │  · D1 write-back        │    api.dialpad.com (from +14159804986)
                  └───────────┬─────────────┘
                              ▼
              Snowflake MCP gateway                Cloudflare AI Gateway
              sandy.hellolanding.tech/             (org secret AI_GATEWAY_TOKEN,
              mcp-gateway/snowflake                 OpenAI-compat /chat/completions)
              (MCPGW_SNOWFLAKE_TOKEN,
               LANDING.CORE dims, read-only)
```

**Division of labor** (Sandy doctrine: apps are ms-scale request/response, no AI):
- **App (worker)**: UI, D1 CRUD, GraphQL reads for the Move-In picker (fast, auto-authenticated by the gateway), triggering workflows, status polling.
- **Workflow (`mass-notify-dispatch`)**: everything slow or credentialed — Snowflake recipient fetch, the AI calls, the email/SMS fan-out, writing results back to D1 via callback. Sends are **queue-serialized per campaign** (same discipline as qa-scoring's scoring queue).

### 5.1 Credentials inventory (verified 2026-08-05)

| Need | Mechanism | Status |
|---|---|---|
| **Snowflake (recipient + contacts path)** | Org secret `MCPGW_SNOWFLAKE_TOKEN` → `sandy.hellolanding.tech/mcp-gateway/snowflake`, tool `snowflake_sql_exec_tool` (read-only, `LANDING.CORE`) | ✅ exists; queries validated live |
| Landing GraphQL (app) | Outbound allow-list rule `(www\|api).hellolanding.com/api/v1/(tools\|graphql\|leads)` auto-injects `X-API-TOKEN` | ✅ exists, global |
| Landing GraphQL (workflow) | Org secret `LANDING_API_GRAPHQL_KEY` | ✅ exists |
| AI inference | Org secret `AI_GATEWAY_TOKEN` → Cloudflare AI Gateway `/compat/chat/completions` | ✅ exists, auto-injected in every WF |
| Dialpad SMS (write) | Per-workflow secret `DIALPAD_API_KEY`; from-number +14159804986 (D4). The MCP-gateway Dialpad token is read-only — not usable for sends | ➕ add to new WF |
| Gmail send | GAS dispatcher WebApp URL + shared secret, payload mode, deployed as member.support@ (D3) | ➕ new deployment of the proven qa-scoring pattern |
| Admin MCP (dev/agents) | Org secret `LANDING_MCP_TOKEN`; URL-path auth `…/mcp/admin/{token}` (⚠ header auth with the .env token 401s — use path form) | ✅ exists |
| Sigma REST API | Client ID/secret → element export with control values | ⏸ deferred (D1); request once pilot is live |
| Everything else | Outbound rule "Allow Everywhere" `^https://.*` (no auth injection) | ✅ safety net |

---

## 6. Data Sources & Contracts

### 6.1 Recipients — Snowflake `LANDING.CORE` (validated live 2026-08-05)

Tables confirmed: `DIMRESERVATION`, `DIMUSER`, `DIMHOME`, `DIMPROPERTY`, `DIMMARKETSEGMENT`, `DIMOCCUPANT`, `TBLAPPLICATION` — the exact dims the retired Looker explore and the Sigma workbook are built on (DBT-managed).

**Recipient query contract** (replicates today's Looker "Active Occupants" semantics; validated — returns the same members/reservation IDs the Sigma workbook shows for Woodhill):

```sql
SELECT r.RESERVATION_ID, p.PROPERTY_NAME, u.USER_FULL_NAME, u.USER_EMAIL,
       u.USER_PHONE, h.UNIT_NUMBER, p.MARKET_SEGMENT, p.MARKET_SEGMENT_AGM_NAME,
       ms.MS_TIMEZONE,
       r.RESERVATION_CHECK_IN_DATE, r.RESERVATION_CHECK_OUT_DATE
FROM LANDING.CORE.DIMRESERVATION r
JOIN LANDING.CORE.DIMUSER u      ON u.USER_ID = r.RESERVATION_USER_ID
JOIN LANDING.CORE.DIMHOME h      ON h.HOME_ID = r.RESERVATION_HOME_ID
JOIN LANDING.CORE.DIMPROPERTY p  ON p.PROPERTY_ID = h.PROPERTY_ID
LEFT JOIN LANDING.CORE.DIMMARKETSEGMENT ms ON ms.MARKET_SEGMENT_ID = p.MARKET_SEGMENT_ID
WHERE p.PROPERTY_NAME = :property_name
  AND r.RESERVATION_PLATFORM = 'Landing'
  AND r.RESERVATION_CHECK_IN_DATE <= CURRENT_DATE
  AND (r.RESERVATION_CHECK_OUT_DATE IS NULL OR r.RESERVATION_CHECK_OUT_DATE >= CURRENT_DATE)
  AND h.HOME_CURRENTLY_OCCUPIED = TRUE
```

Known nuance to settle with Ops during pilot: for a reservation **starting today**, the Sigma workbook's "Current" horizon (date-range) includes it while `HOME_CURRENTLY_OCCUPIED` (today's Looker behavior) may not yet. Default = today's behavior; a "starting today" toggle is cheap if Ops wants workbook semantics.

**Dedup policy (revised from GAS):** group by lowercased `USER_EMAIL`; N=1 → eligible; N≥2 → one row eligible, others `REVIEW` with note (the applicant-alt-email juggling is dropped — flag instead of guess). INDIVIDUAL sends **always dedupe** (fixes defect 7).

**Phone normalization:** `USER_PHONE` formats are inconsistent (`(405) 441-3017`, `+14355254840`, `9518708735`). Normalize to E.164 at ingest (default +1 for bare 10-digit); rows that fail normalization get `sms_state = 'error'` with note, email unaffected.

### 6.2 Move-In Flow — Landing Admin GraphQL + warehouse (validated live)

Nested GraphQL works through the auto-auth gateway:

```graphql
{ reservations(scope: "upcoming", page: 1, per_page: 2) {
    data { id start_date end_date move_in_date
           user { full_name email }
           home { unit_number property_id } }
    total_count } }   # → 4,708 upcoming
```

Auto-fill sources per Move-In field:

| Move-In field | Source |
|---|---|
| Reservation, dates | `Reservation` (`start_date`, `end_date`, `move_in_date`, `access_codes`, `move_in_method`) |
| Member name/email | `Reservation.user` |
| Member phone | `DIMUSER.USER_PHONE` by `RESERVATION_USER_ID` (D7; GraphQL `User` has no phone field) |
| Unit | `Reservation.home.unit_number` |
| Property name/address | `Property` (`name`, `address1/2`, `state`, `zip`; note `move_in_flow`, `move_in_buffer_in_days` fields exist) |
| Pets | `Pet` model ("pets registered by members on applications") |
| Occupants / vehicles | `RentalApplication` ("move-in date, occupant details…") — exact fields TBD during build; `DIMOCCUPANT` as fallback |
| Area manager | `DIMPROPERTY.MARKET_SEGMENT_AGM_NAME` / `Market::Segment` |
| **Property contact emails** | **`DIMPROPERTY.PROPERTY_CONTACT_{NAME,EMAIL,PHONE_NUMBER,DESIGNATION,SUBSCRIBED_TO_EMAILS}_1..6`** (discovered during validation — full contact records in the warehouse). Contact book in D1 is **seeded from these columns per property**, then operator-curated (add/edit/deactivate persists — satisfies "persisted until the operator edits"). Warehouse refresh is DBT-cadence (not real-time) — acceptable; contacts change rarely and operator edits win. GraphQL `Contact` field enablement (escalated, D2) would only upgrade freshness. |

### 6.3 Email dispatch — GAS payload-mode dispatcher as member-support@

Same pattern as qa-scoring v0.22 (live, smoke-tested): a minimal Apps Script WebApp exposing `doPost` with a shared-secret header; the workflow POSTs `{to, cc, bcc, replyTo, senderName, subject, htmlBody, attachmentFileIds[], draftMode}` per message; GAS resolves Drive attachments natively (keeps folder-expansion + Google-file→PDF + 20MB behaviors) and sends via `GmailApp`. **Deployed under member.support@hellolanding.com (D3)** — the "from" line is the team inbox, replies land where members already write, and quota is the team account's, not a person's. Quota surfaced in the UI (counter vs Workspace ~1,500 recipients/day) instead of discovered by crash.

### 6.4 Dialpad SMS

`POST dialpad.com/api/v2/sms` from the workflow, `DIALPAD_API_KEY` secret, **from +1 (415) 980-4986** (verified Member Support Line, D4). Sent per member after that member's email send succeeds. Quiet hours (as built, v0.8): no SMS outside 08:00–21:00 local (`MS_TIMEZONE`, Rails→IANA mapped) — members outside the window are **marked `skipped_quiet_hours` and re-sent via the one-click "Send SMS to N emailed recipients" action** rather than scheduled (v1 simplification; timed scheduling can come later). Members with `text_notifications_enabled = false` (GraphQL `User`) are skipped as `skipped_optout`; a lookup failure degrades to sending with an audited warning (parity: the manual Hub-Manager process had no opt-out check). One AI summary per campaign (not per member); operator can preview/regenerate it and test-send to their own number before anything goes to members.

### 6.5 AI calls — Cloudflare AI Gateway

Both via the org `AI_GATEWAY_TOKEN`, OpenAI-compatible endpoint, from workflows (never the app):

1. **`draft-assist`** (on-demand, from the composer): given intake fields (event, property, custom body from the Slack form), produce/polish the HTML email body in Landing's voice, preserving `{{tokens}}` verbatim. Operator always reviews; nothing auto-sends.
2. **`sms-summarize`** (in dispatch): email subject + rendered body → ≤ 320 chars plain-text SMS, no links unless present in the body, no invented facts, includes property name. Deterministic-ish (temp 0.2), output length-validated; over-length → one retry with stricter instruction → hard truncate at sentence boundary + audit flag.

Model default: whatever the gateway routes (Claude); both prompts stored in D1 `app_config` so OpsVP wording tweaks don't need redeploys.

---

## 7. Modes & UX

Single app, three top-level surfaces + admin. Landing design system (template default), no client JS beyond the established SSR + form-POST + islands-for-charts patterns.

### 7.1 Campaign mode (INDIVIDUAL)

Wizard-ish single page, mirroring today's 4 sections but with server truth:

1. **Recipients** — "Fetch active residents" (property autocomplete; Snowflake query §6.1 via workflow), CSV import, paste, manual add. Grid shows status chips (`PENDING`/`READY`/`REVIEW`/`SENT`/`DRAFT`), notes, per-row attachment IDs. Edits are **field-level PATCHes — never a whole-grid overwrite** (fixes defect 1).
2. **Configure** — templates (7 seeded, D1-stored, editable in Admin), cards (6 resident cards; MOVE_IN excluded here), subject/greeting/intro/closing/disclaimer, branded wrapper, reply-to, CC extra, window start/end, per-campaign SMS toggle + preview of the SMS summary.
3. **Preview** — server-rendered with the real pipeline against a chosen recipient (not just the first), sandboxed iframe, attachment manifest, **plus SMS preview** (via short workflow, cached per body-hash).
4. **Send** — validation with real, blocking errors (empty subject, no eligible recipients, invalid emails by real regex, attachment failures — fixes defects 4/6); dry-run (Gmail drafts in the member.support@ mailbox via dispatcher draft mode), test-send-to-me, and Send. Progress polled from D1 as the workflow reports per-recipient results.

### 7.2 Move-In Flow mode (redesigned)

Own route and visual language: **the screen IS the email**. Left rail = reservation picker + data panel; main pane = live MoveInCard rendered exactly as it will send (same renderer as dispatch, no drift).

Flow:
1. **Pick reservation** — search by property (GraphQL: property → homes → upcoming reservations) or by member name/reservation ID.
2. **Auto-fill** — §6.2 mapping populates the draft; every field editable inline; edits persist to D1 `movein_drafts` (autosave) so drafts survive reloads and handoffs. Fields the sources couldn't fill render as required-empty highlights.
3. **Contacts** — property contact chips seeded from `DIMPROPERTY` contact columns, curated in the D1 contact book (add/edit inline; persisted for next time). Warning if none active.
4. **Attachments** — background check + ID scan Drive IDs (validated names shown).
5. **Confirm & send** — full-size email mock + explicit recipient list + "Send to N property contacts". Sends one email per reservation; archived to D1 **including Move-In** (closes the GAS TODO).

### 7.3 History & Undo

Campaign list (all modes) with per-recipient drill-down, filters by property/actor/date/mode — replaces the Database menu queries. **Undo** reverts recipient statuses of a completed run (campaign-scoped, race-free since state lives in D1 rows, not `getLastRow()` — fixes defect 3) and is itself an audited run. Test sends and drafts are audited runs too (fixes defect 11).

### 7.4 Admin

Role management (operator/admin, invisible-SSO resolution like qa-scoring v0.24), template/card editing, prompt editing, dispatcher health check, quota counter, contact-book bulk re-seed from warehouse.

## 8. D1 Schema (initial)

```sql
campaigns(id, mode, property_name, event_name, status,        -- draft|sending|complete|errored|undone
          config_json, subject_resolved, sms_enabled,
          created_by, created_at, completed_at)
recipients(id, campaign_id, reservation_id, email, name, unit, phone_e164,
           segment_timezone, status, notes, attach_ids_json,
           email_state,  email_sent_at,                        -- pending|sent|error|skipped
           sms_state,    sms_sent_at, sms_body, sms_error)     -- off|queued|sent|error|skipped_optout|skipped_quiet_hours
movein_drafts(id, reservation_id, property_id, fields_json, contact_ids_json,
              status, created_by, updated_at)                  -- draft|sent|archived
property_contacts(id, property_id, property_name, name, email, phone, title,
                  source, active, updated_by, updated_at)      -- warehouse|manual
runs(id, campaign_id, kind, actor, count, row_states_json,     -- send|dryrun|test|undo
     started_at, completed_at, error)
templates(id, name, kind, config_json, active)                 -- email template | card | sms/draft prompt
app_config(key, value)
roles(email, role)
```

Historical Railway Postgres (`mass_notifications.campaigns/recipients`) stays read-only; optional one-time backfill into D1 is Phase 6 (same B-phase playbook as qa-scoring).

## 9. Send pipeline & state machine

- Eligibility unchanged: `''|PENDING|READY` eligible; `SENT|DRAFT|REVIEW` blocked. Exact status strings preserved for operator muscle-memory.
- Campaign trigger inserts a `runs` row, enqueues the workflow with `{campaign_id, kind}`; the workflow processes recipients serially (Gmail pacing ≈ today's 100ms sleeps), reporting per-recipient outcomes to the callback → D1. Partial failure leaves accurate per-row state.
- Token rendering: same `{{token | fallback}}` grammar and catalogue, but **all token values HTML-escaped by default** with an explicit `html:` prefix escape hatch for the few config-authored HTML fields (fixes defect 5).
- Run IDs keep the `<MODE>-yyyyMMdd_HHmmss` shape for continuity in exports.

## 10. Rollout

| Phase | Deliverable | Exit criteria |
|---|---|---|
| P0 | ~~Decisions~~ ✅ done 2026-08-05; deploy GAS dispatcher from member.support@ | Dispatcher answers a health-check payload |
| P1 | App scaffold (database template), D1 migrations, RBAC, Campaign mode UI with Snowflake fetch + manual/CSV | Fetch Woodhill → grid matches Sigma workbook |
| P2 | Dispatch workflow: email send E2E (dry-run, test, send, undo), full audit | Parity campaign on a test property; drafts land in member.support@ Gmail |
| P3 | SMS companion: summarize + Dialpad send + quiet hours + opt-out + E.164 normalization | OpsVP approves SMS copy on 3 real bodies; test SMS delivered from +14159804986 |
| P4 | Move-In Flow mode (picker, auto-fill, warehouse-seeded contact book, email-mock UI) | One real move-in sent side-by-side with GAS output |
| P5 | Slack intake prefill link; Admin surface | ERT issues one request end-to-end without touching the Sheet |
| P6 | History backfill from Railway PG (optional); GAS retirement: WebApp offline, Sheet frozen, Looker creds revoked | Two clean weeks on Sandy |

**Post-v1 pinned upgrades** (unblocked by pilot leverage): Sigma REST export path (workbook-parity guarantees, D1); GraphQL `Contact` field enablement (real-time contact freshness, D2); Sandy Agent in #ert-member-support (D6).

## 11. Appendix — exact-string parity checklist

- Modes: `INDIVIDUAL`, `MOVE_IN` (`BCC` retired, D5); run kinds `SEND_INDIVIDUAL`, `SEND_MOVE_IN`, `DRYRUN_DRAFTS`, `DRYRUN_DRAFTS_MOVE_IN`, `UNDO` (`SEND_BCC` historical-only in backfilled data).
- Statuses: `''`, `PENDING`, `READY`, `REVIEW`, `DRAFT`, `SENT` (+ SMS states §8).
- Cards: `FIRE_INSPECTION`, `WATER_OUTAGE`, `MAINTENANCE`, `WEATHER_ALERT`, `POWER_OUTAGE`, `WIFI_OUTAGE`, `MOVE_IN`.
- Templates: `Annual Fire Inspection`, `Water Outage`, `General Maintenance`, `Weather Alert`, `Power Outage`, `Move-In Notification`, `WiFi Outage`.
- Subject prefixes: `[DRAFT] `, `TEST — `; attachment errors `ATTACH_NOT_FOUND id=…`, `ATTACH_TOO_LARGE total=…`.
- Numeric defaults: `dry_run_limit=10`, `max_per_run=500`, attachment cap 20MB.
- Token catalogue: global `property_name, event_name, date_range, today, manager_email, manager_name`; per-row `member_email, member_name, first_name, unit`; Move-In adds `apartment_number, member_phone, move_in_date, move_out_date, vehicle_info, pet_info, area_mgr_name, area_mgr_phone, area_mgr_email, reservation_id`.