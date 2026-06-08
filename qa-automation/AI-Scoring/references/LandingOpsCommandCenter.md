# Landing Ops Command Center — Design & Implementation Reference

> **Owner:** Max (Maximiliano) Perez — Member Support Team Lead
> **Status:** Design spec — v1.0
> **Stack:** FastAPI · Jinja2/HTML · Chart.js · SSE · Dialpad Webhooks · Google Sheets API · Looker API
> **Repo:** `Landing-scripts` — top-level `command-center/` directory; routers mounted into the existing QA scoring FastAPI app at `qa-automation/AI-Scoring/backend/` for shared infrastructure (SSE, config loader, Slack client, Railway deployment). See §6 → Deployment.

---

## 1. Product vision

A single-pane-of-glass real-time operations dashboard for Landing Living's Member Support team. It aggregates live events from Dialpad (calls, hold times, agent status), Google Sheets (maintenance tickets, job requests), the QA scoring pipeline (new evaluations, SPC outliers), Slack workflows (mass notification requests), and a Looker-maintained frequent-caller registry into a unified command center with tiered alerts, actionable chiclets, and live call management.

### What it replaces

Today managers monitor operations across 4+ browser tabs: Dialpad call center view, multiple Google Sheets, the QA dashboard, and Slack. The Command Center collapses all of these into one screen optimized for real-time decision-making.

### What it is NOT (v1)

- Not a replacement for Dialpad's full admin panel (call routing config, IVR management stay in Dialpad)
- Not a local AI inference platform (profanity detection and advanced NLP features are Phase 2+)
- Not an MCP-connected agent orchestration layer (planned but post-v1)

---

## 2. Alert tier architecture

**This is the most important design decision.** Every notification source maps to exactly one of three tiers. Research on tiered alert systems consistently shows that beyond three tiers, the hierarchy collapses into noise. Three tiers hold.

### Tier 1 — CRITICAL (interrupts, demands immediate action)

- **Visual:** Large chiclet with animated pulsing red border (`box-shadow` glow, 1.5s CSS animation cycle)
- **Behavior:** Appears at top of alert rail, triggers simultaneous Slack notification to managers channel
- **Audio:** Optional browser notification sound (configurable per user)
- **Sources:**
  - Call on hold > 5 minutes (escalates from Tier 2 at 3 min to Tier 1 at 5 min)
  - Repeated caller (3rd+ call same day) — with AI summary of prior calls
  - Profanity detection (Phase 2: agent→caller is always Tier 1; caller→agent starts Tier 2)

### Tier 2 — IMPORTANT (needs attention soon, not interruptive)

- **Visual:** Chiclet with steady amber/orange border (2px solid, subtle inner glow, no animation)
- **Behavior:** Appears in alert rail below any Tier 1 items, no audio
- **Sources:**
  - New QA evaluation that triggers SPC outlier detection (>2σ below mean)
  - Google Sheets updates (new maintenance tickets, job requests marked urgent)
  - Mass notification requests from Slack workflow
  - Call on hold 3–5 minutes (pre-escalation)
  - Frequent caller (incoming `external_number` matches the Looker frequent-caller registry — persistently flagged by ops; escalates in place to T1 Repeated on the 3rd call same day, see §4)

### Tier 3 — INFORMATIONAL (awareness, no action required)

- **Visual:** Floating toast notification, bottom-right overlay, auto-dismiss after 8–10 seconds
- **Behavior:** Stacks up to 3 visible toasts; older ones collapse into a badge count
- **Sources:**
  - Routine QA evaluations (non-outlier)
  - Agent duty status changes (on/off duty)
  - Completed call transfers
  - Scoring job completions

### Rules

- A chiclet's tier is set at creation and can only escalate (Tier 2 → Tier 1), never downgrade while active. **Escalation mutates the existing chiclet in place** — same ID, same accumulated history; only the tier/border/position-in-rail change. The SSE `chiclet_escalated` event signals the transition (see §6 SSE protocol).
- Acknowledged chiclets collapse and move to a "resolved" section at the bottom of the alert rail.
- **Day-boundary reset.** All "today" counters (repeated-call counts, Frequent-caller call counts, calls-today metric) reset at midnight in the team's configured `timezone` (default `America/Mexico_City`, UTC-6, no DST). Active chiclets whose tier was driven by those counters (Repeated and escalated-Frequent T1s) auto-resolve at the boundary.
- The tier classification logic lives in `command-center/services/alert_classifier.py` as pure functions (testable without I/O).

---

## 3. Layout architecture

The dashboard follows a "command center" layout pattern, optimized for a 1920×1080 monitor (typical wall-mounted display). It degrades gracefully to 1366×768 laptops by collapsing the analytics sidebar into an expandable drawer.

### Zone A — Status bar (top, full width, 60px height)

A persistent strip of macro health indicators. Answers: "Is the floor OK right now?"

| Metric | Source | Color logic |
|--------|--------|-------------|
| Agents on duty / total | Dialpad agent status events | Green ≥75%, Amber 50–74%, Red <50% |
| Calls in queue | Dialpad call events (state=queued count) | Green 0–2, Amber 3–5, Red >5 |
| Average wait time | Computed from queued→connected deltas | Green <2:00, Amber 2:00–4:00, Red >4:00 |
| Longest current hold | Max hold duration across active calls | Green <3:00, Amber 3:00–5:00, Red >5:00 |
| Calls today | Running count from midnight reset | No color threshold |
| QA avg (today) | From QA scoring pipeline | Green ≥85, Amber 75–84, Red <75 |

Thresholds are configurable per team via `config/teams/{team_id}.json` (same pattern as existing QA config).

### Zone B — Alert rail (left column, ~30% width)

Vertically stacked chiclets. Tier 1 on top, Tier 2 below. Each chiclet is a self-contained action card. Scrollable when overflow occurs. Maximum visible chiclets before scroll: 4–5 depending on content height.

### Zone C — Live activity feed (center, ~45% width)

Real-time call activity table showing all active calls, agents on duty, and queue entries. Rows are color-coded by status (active=default, hold=red tint, ringing=amber pulse). Clicking a row expands inline action buttons: Transfer, Merge, Send Context via Slack, View Transcript, etc, depending on the nature of each chiclet. Below the table: Tier 3 toast notification stack.

### Zone D — Analytics sidebar (right column, ~25% width)

Compact analytics cards showing shift context. Top 5 performers' EWMA sparkline of the day (from `team_stats.py`), QA scores today, average handle time, SPC status, calls-by-hour distribution, pending mass notifications. Updates on new QA evaluations via SSE (existing infrastructure).

---

## 4. Chiclet design system

### Anatomy

Every chiclet has a consistent structure:

```
┌─────────────────────────────────────────┐
│ [4px source stripe]                      │
│  [Tier badge]               [Source icon]│
│  [Title with icon]                       │
│  [Timer — if applicable, large font]     │
│  [Body — 2-3 lines of context]           │
│  [Action buttons]                        │
└─────────────────────────────────────────┘
```

### Source stripe colors

| Source | Color | Hex |
|--------|-------|-----|
| Dialpad | Purple | `#7C52FF` |
| Google Sheets | Green | `#34A853` |
| Slack | Amber | `#BA7517` |
| QA Scoring | Navy Blue | `#15192D` |

### Border states

| Tier | Border | Animation |
|------|--------|-----------|
| Tier 1 (Critical) | 2.5px solid red | `pulse-border` — 1.5s ease-in-out infinite, alternates border-color and box-shadow glow |
| Tier 2 (Important) | 2px solid amber | Static, no animation |
| Tier 3 (Toast) | 0.5px standard border | No special treatment |

### Chiclet types

#### Hold alert chiclet

- **Trigger:** Dialpad webhook `state=hold`; chiclet appears once the call crosses the T2 threshold (cumulative `total_hold_seconds` ≥ 180s by default).
- **Content:** Live-ticking timer showing **cumulative hold time** (`total_hold_seconds`) across all hold cycles of this call (large, centered); agent name; caller name; a small "on hold now" / "reconnected" badge that reflects current state.
- **Actions:** "Notify Agent" (Slack DM)
- **Resolution:** Auto-resolves on `state=hangup`. The chiclet does **not** resolve on hold-released-but-still-active calls — once a call has crossed the T2 threshold it stays visible until hangup, since cumulative hold may resume.
- **Two counters per call** (see §5.1):
  - `total_hold_seconds` — accumulates across every hold cycle, drives the displayed timer and the T2/T1 threshold checks (default T2 ≥ 180s, T1 ≥ 300s).
  - `current_hold_started_at` — set on each `state=hold`, cleared on each `state=connected` / `hangup`. Drives the Slack exponential-backoff schedule (see §5.4); resets per hold cycle so a brief mid-hold reconnect restarts the backoff clock.

#### Repeated caller chiclet

- **Trigger:** Third+ call from same `external_number` **today** (calendar day in the team's `timezone`, default `America/Mexico_City`). Applies whether or not the caller is in the Looker registry — if they *are* in Looker, the chiclet is the in-place escalation of the existing T2 Frequent chiclet (see Frequent caller chiclet → Escalation); if not, a new T1 chiclet is created from scratch.
- **Content:** Caller name/number; call count badge (e.g. "3rd call today"); concatenated `recap_summary` from every prior call today.
- **Actions:** "Send History to Agent" (Slack DM with summary), "Full History" (opens call log)
- **Resolution:** Manual acknowledgment by manager, or auto-resolve at day-boundary midnight in `timezone`. 4th+ calls the same day do **not** create new chiclets — they append the new `recap_summary` to this one.

#### Frequent caller chiclet

- **Trigger:** Inbound Dialpad call where `external_number` (E.164-normalized) matches the Looker-maintained frequent-caller registry (see §5.6). Fires on the **first** call of the day from that number, not the third.
- **Content:** Caller name/number; flag reason from the Looker row (e.g. "habitual escalator", "unresolved billing"); call count badge ("1st call today"); concatenated `recap_summary` from each prior call today.
- **Actions:** "Send Context to Agent" (Slack DM), "View Caller Profile" (deep link to the Looker row)
- **Resolution:** Manual acknowledgment by manager, or auto-resolve at day-boundary midnight in `timezone`.
- **Same-day behavior:**
  - **1st call today** → new T2 Frequent chiclet created.
  - **2nd call today** → same chiclet stays in the rail; call count increments to "2 calls today"; new `recap_summary` appended. **No new chiclet, no SSE `chiclet_created` re-fire.** SSE emits a `chiclet_updated` payload instead.
  - **3rd call today** → **in-place escalation to T1 Repeated.** Chiclet keeps its ID and content history; tier mutates T2 → T1; border + position-in-rail update; SSE emits `chiclet_escalated`. From this point the chiclet IS the Repeated chiclet.
  - **4th+ calls today** → no further escalation, no new chiclet; just append the new `recap_summary` and bump the count. SSE emits `chiclet_updated`.
- **Source stripe:** Dialpad (trigger is a Dialpad call event; Looker is the enrichment source).
- **Relationship to Repeated chiclet:** For Looker-flagged callers, the Repeated chiclet is reached *only* via this escalation path. For non-Looker callers, the Repeated chiclet appears fresh on the 3rd call today (no prior T2). The Looker registry is **VP-curated only** — the system never writes to it, even on a 3rd-call escalation event.

#### QA outlier chiclet

- **Trigger:** New evaluation where `compute_outliers()` flags the agent (>2σ via modified Z-score)
- **Content:** Agent name, score vs. average, which SPC rule was violated
- **Actions:** "View Scorecard" (links to existing `/datapoint/{eval_id}`), "Agent Dashboard" (links to `/dashboard/{agent}`)
- **Resolution:** Manual acknowledgment

#### Sheets update chiclet

- **Trigger:** New row in monitored Google Sheet (maintenance tickets, job requests)
- **Content:** Sheet name, key fields from the new row (unit number, issue type, priority)
- **Actions:** "Open Sheet" (external link), "Open Mission Control" (external link)
- **Resolution:** Manual acknowledgment or auto-dismiss after configurable timeout

#### Mass notification request chiclet

- **Trigger:** Slack workflow posts to a monitored channel
- **Content:** Requester name, email subject, target Property (from Slack Workflow)
- **Actions:** "Review & Send" (opens mass notification WebApp external link), "Dismiss"
- **Resolution:** Manual acknowledgement from Specialist or Manager

#### Profanity detection chiclet (Phase 2)

- **Trigger:** Local AI model flags transcript segment
- **Content:** Severity level, context snippet (redacted), direction (agent→caller or caller→agent)
- **Actions:** "Review Transcript", "Escalate to HR (Slack DM to managers + HR group)", "Dismiss (false positive)"
- **Resolution:** Manual review completion
- **Note:** Requires local inference (likely Gemma or similar on Ollama). API providers won't handle this. Context sensitivity is critical — caller distress (deaths in units and other safety-critical scenarios) must not trigger false positives, profanity detection is meant to detect deliberate instances of disrespect during the call.

---

## 5. Data source integration

### 5.1 Dialpad webhooks (PRIMARY — calls, holds, agent status, call center service levels and hold queue length)

**Setup flow:**
1. Create a webhook via `POST /v2/webhooks` with `hook_url` pointing to our public endpoint and a JWT `secret`
2. Create call event subscription via `POST /v2/webhook_call_event_subscriptions` with `webhook_id`, filtering for states: `ringing`, `connected`, `hold`, `hangup`, `recording`, `call_transcription`, `recap_summary`
3. Create agent status event subscription for duty status changes

**Endpoint:** `POST /api/webhooks/dialpad` (served by the AI-Scoring app on Railway — see §6 Deployment)

**JWT verification — Dialpad-specific quirk.** Dialpad webhooks deliver the **entire POST body as the JWT**, not a JSON body with a JWT in a header. The handler must therefore decode first, parse Pydantic second:

```python
@router.post("/api/webhooks/dialpad")
async def receive(request: Request):
    body = (await request.body()).decode()
    try:
        payload = jwt.decode(body, SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(401)
    event = DialpadCallEvent.model_validate(payload)  # Pydantic AFTER decode
    await cc_event_bus.enqueue(event)
    return {"ok": True}
```

The previous "JWT verify (middleware) → parse Pydantic" sequence is incorrect for Dialpad; there is no JSON to parse before the JWT decode. Reject with 401 on `InvalidTokenError`, 422 on Pydantic validation failure.

**Processing pattern:** The handler returns 200 in <100ms. It only decodes the JWT, validates Pydantic, and writes to `cc_event_bus` (an `asyncio.Queue` plus Postgres write-through for crash-resilience — see Persistence below). A background worker drains the queue: updates `call_state`, runs `alert_classifier.classify()`, pushes SSE events to connected dashboards. All heavy work is async.

**Call graph resolution:** Dialpad's call model uses multiple `call_id`s for a single interaction (`entry_point_call_id`, `operator_call_id`, `master_call_id` for transfers). The event processor resolves the "real" call context using the same precedence logic as `build_dialpad_link()` in `dialpad_client.py`. Key fields:

- `entry_point_call_id` — links operator calls to the customer-facing call
- `master_call_id` — links transferred calls back to the original
- `is_transferred` — on hangup, indicates the call flow continues

**Phone number normalization:** All `external_number` values pass through a single shared helper, `utils.phone.normalize()` (lives in the AI-Scoring backend, imported by Command Center), which returns E.164 (strips extensions, formatting, and normalizes country code — defaulting to MX/US per call center). **Every** comparison or storage site uses normalized form: webhook ingest, Looker cache load, repeated-caller counter keys, Slack message rendering. Raw `external_number` strings are never compared directly.

**Repeated-caller / Frequent-caller detection:** Both run off a single per-team `todays_calls: dict[normalized_number, list[CallRecord]]` keyed on E.164. The dict is reset at midnight in the team's `timezone` (default `America/Mexico_City`). On every inbound `state=connected`:

1. Normalize `external_number` via `utils.phone.normalize()`.
2. Append the call to `todays_calls[number]`.
3. Check Frequent: if number ∈ Looker frequent-caller cache → emit/update T2 Frequent chiclet (see §4 chiclet semantics).
4. Check Repeated: if `len(todays_calls[number]) >= 3` → if a T2 Frequent already exists for this number, escalate it in place (emit `chiclet_escalated`); else create a fresh T1 Repeated chiclet.
5. 4th+ calls today append `recap_summary` to the existing chiclet (emit `chiclet_updated`); never create new chiclets.

**Persistence (gating dependency).** `todays_calls`, the webhook idempotency dedupe set, hold accumulators (`total_hold_seconds` per call), and the frequent-caller cache **must survive Railway redeploys.** All of these are backed by Postgres tables in the `command_center` schema; the in-memory dicts above are runtime caches hydrated from Postgres on startup and written through on mutation. Concrete table definitions live in the **SQL Migration design doc** (drafted in a separate session — see §12). Phase 1 implementation is gated on that doc being complete.

**Rate limits:** Dialpad API is 20 req/sec per company globally. Webhook delivery is separate (inbound to us) and not rate-limited on our side, but idempotency dedupe (`call_id` + `state` + `event_timestamp`) is mandatory — Dialpad retries on every non-200, including transient deploy-window non-200s, and the dedupe set must be Postgres-backed (in-memory is wiped on restart, causing reprocessing).

### 5.2 Google Sheets polling (maintenance tickets, job requests)

**Pattern:** Polling via `gspread` on a configurable interval (default: 60 seconds). Compare row count / last-modified timestamp to detect new rows.

**Alternative (preferred):** Google Apps Script `onEdit` trigger that POSTs to our webhook endpoint when a row is added to a monitored sheet. This gives near-instant detection without polling overhead.

**Configuration:**
```json
{
  "sheets_monitors": [
    {
      "sheet_id": "...",
      "tab_name": "Maintenance Tickets",
      "key_columns": {"unit": "A", "issue": "B", "priority": "C"},
      "tier": 2,
      "chiclet_type": "sheets_update"
    }
  ]
}
```

### 5.3 QA scoring pipeline (existing SSE infrastructure)

The QA scoring app already implements SSE for live dashboard updates. The Command Center connects to the same SSE endpoint (`GET /api/{team_id}/events`) and listens for `score_complete` events. When a new evaluation arrives, run it through `compute_outliers()` to determine if it warrants a Tier 2 chiclet or just a Tier 3 toast.

### 5.4 Slack integration (mass notifications, outbound alerts)

**Inbound:** Monitor a designated Slack channel for mass notification workflow posts. Use the existing Bolt for Python / Socket Mode infrastructure from the AckTracker app.

**Outbound for hold alerts — exponential backoff per current hold cycle.** Notifications are driven by `current_hold_started_at` (not `total_hold_seconds`), so a brief mid-hold reconnect resets the clock. The schedule, configurable in `command_center.json` → `slack.hold_backoff_minutes` (default `[5, 10, 20, 40]`, doubling thereafter):

- t = 5 min into current hold cycle → first Slack DM (also the T2 → T1 chiclet escalation point)
- t = 10 min → second DM
- t = 20 min → third DM
- t = 40 min → fourth DM, and so on, doubling

On `state=connected` or `state=hangup`, the backoff counter resets. The chiclet's displayed cumulative `total_hold_seconds` is independent of this Slack schedule — the chiclet keeps showing total hold time even when Slack is silent.

**Outbound for repeated callers / frequent callers:** A single Slack DM at the moment a T1 Repeated chiclet is created — whether by a fresh 3rd-call detection (non-Looker caller) or by escalation from a T2 Frequent (Looker caller). No additional DMs for 4th+ calls the same day.

All outbound messages use `slack_send_message` and include rich formatting with call context, caller history (concatenated `recap_summary`), and action deep-links back to the Command Center.

### 5.5 Dialpad WebSocket (Phase 3 — agent status wallboard)

For the lowest-latency agent status updates, connect to Dialpad's WebSocket service:
1. Create WebSocket entity via `POST /v2/websockets`
2. Create agent_status event subscription with the WebSocket's `id` as `endpoint_id`
3. Connect to `websocket_url` (token expires in 1 hour; reconnect with fresh URL from `GET /v2/websockets/{id}`)

The WebSocket client runs server-side (keeps API key off the frontend), enriches events with roster data, and relays to the dashboard via SSE.

### 5.6 Looker frequent-caller registry (enrichment source for Dialpad call events)

**Purpose:** A persistent, **VP-curated** Looker Look serves as the global source of truth for `external_number`s that should trigger a T2 Frequent chiclet on inbound calls — distinct from automatic same-day Repeated detection. Numbers are added/removed manually with a free-text "flag reason" column surfaced in the chiclet. The system never writes back to this Look.

**Scoping (v1):** The Look itself is **global** — it covers all incoming-IVR callers across the company, since callers route through one IVR before being distributed to call centers. The Command Center in v1 only subscribes to **Member Support's** `call_center_id`(s) via `dialpad.target_call_center_ids`, so even though the cache holds the global registry, only Member-Support-routed inbound calls fire chiclets. **Adding Sales (or any other call center) later is a config-only change** — append their `call_center_id` to `dialpad.target_call_center_ids`. No code change.

**Pattern:** Cached snapshot with check-on-miss.

1. **Scheduled refresh.** On startup and on `refresh_interval_seconds` (default 900s / 15 min), query the Look via the Looker API. Normalize each row's number through `utils.phone.normalize()` (E.164). Load into an in-memory `dict[str, FrequentCallerEntry]`.
2. **Check-on-miss.** When a Dialpad call arrives with a number **not** in the cache AND the cache snapshot is older than `check_on_miss_max_age_seconds` (default 120s), the service triggers a single Looker query *for that specific number* before classifying the call. This bounds the perceived-staleness window to ≤2 min worst-case — ops adds a number in Looker, the next inbound call from that number is enriched within seconds even if the scheduled refresh hasn't run.
3. **O(1) hot path.** When the cache is fresh, classification is a dict lookup; no I/O.

**Auth:** Looker API 4.0 with `client_id` / `client_secret` (Railway env vars). Token refresh handled by the official `looker-sdk` Python package.

**Configuration:**
```json
{
  "looker": {
    "base_url": "https://landing.looker.com",
    "client_id": "ENV:LOOKER_CLIENT_ID",
    "client_secret": "ENV:LOOKER_CLIENT_SECRET",
    "frequent_callers_look_id": "...",
    "refresh_interval_seconds": 900,
    "check_on_miss_max_age_seconds": 120
  }
}
```

**Failure mode:** If the Looker API call fails, log and keep serving the last cached snapshot. Never block Dialpad call-event processing on Looker availability. If the cache is empty at startup (first-boot Looker outage), Frequent chiclets are silently disabled until the next successful refresh — Repeated and hold detection continue normally. Persistence of the last-good cache snapshot survives Railway redeploys (see §5.1 Persistence; concrete `frequent_callers_cache` table defined in the SQL Migration doc).

---

## 6. Backend architecture

### New modules

The Command Center lives at the **repo root** as a top-level `command-center/` package, not under `qa-automation/`. Routers are mounted into the existing AI-Scoring FastAPI app at runtime (see Deployment below).

```
command-center/
├── routes/
│   ├── webhooks.py          # POST /api/webhooks/dialpad — JWT-body-decoded webhook receiver
│   └── command_center.py    # GET  /cc/{team_id}        — serves dashboard HTML
│                            # GET  /cc/{team_id}/events — SSE stream for live updates
│                            # GET  /cc/{team_id}/state  — current state snapshot (initial load)
├── services/
│   ├── alert_classifier.py  # Pure functions: event → tier + chiclet_type classification
│   ├── cc_event_bus.py      # In-memory pub/sub + SSE fan-out (separate from AI-Scoring's event_bus.py)
│   ├── call_state.py        # Tracks active calls, cumulative hold timers, call graph resolution
│   ├── repeated_callers.py  # Today's-calls counters (per team TZ), recap_summary aggregation
│   ├── frequent_callers.py  # Looker registry sync + check-on-miss lookup (§5.6)
│   └── sheets_monitor.py    # Polling or webhook receiver for monitored Sheets
├── models/
│   ├── alert.py             # Pydantic: Alert, Chiclet, ChicletAction, AlertTier enum
│   ├── call_event.py        # Pydantic: DialpadCallEvent, DialpadAgentStatusEvent
│   ├── frequent_caller.py   # Pydantic: FrequentCallerEntry (number, flag_reason, added_at)
│   └── command_state.py     # Pydantic: full dashboard state snapshot for initial load
└── config/
    └── command_center.json  # Per-team: timezone, thresholds, monitored sheets, Slack, Looker
```

Note: the shared phone-normalization helper `utils.phone.normalize()` lives in the **AI-Scoring backend** (`qa-automation/AI-Scoring/backend/utils/phone.py`), not in `command-center/` — both apps import it from there. See §5.1.

Note: `cc_event_bus.py` is deliberately distinct from the existing AI-Scoring `event_bus.py`. They are separate pub/sub channels in the same Python process — the QA SSE stream remains unchanged on `/dashboard/<team>` paths; CC events flow over `/cc/<team_id>/events`. The Command Center subscribes to AI-Scoring's bus for `score_complete` (read-only) but publishes only to its own.

### Deployment

The `command-center/` directory lives at the repo root, but its FastAPI routers are mounted into the existing AI-Scoring FastAPI app (`qa-automation/AI-Scoring/backend/main.py`) at startup:

```python
from command_center.routes import webhooks, command_center
app.include_router(webhooks.router)
app.include_router(command_center.router)
```

Implications:

- **One process, one URL.** The existing Railway service that hosts the AI-Scoring app also serves the Command Center. The Dialpad webhook endpoint is `https://<railway-host>/api/webhooks/dialpad`; no separate deployment.
- **Shared infrastructure** — SSE event bus, per-team JSON config loader, Slack client, Pydantic base models, and Postgres connection (when introduced in Phase 4) are reused, not duplicated.
- **Watch Paths impact.** Railway's "Watch Paths" on this service must be extended from `qa-automation/AI-Scoring/` to also include `command-center/`. Otherwise commits that only touch the Command Center code will be auto-skipped by Railway. This is a one-time service-config change.

### Event flow

```
Dialpad webhook POST
    → read raw body
    → jwt.decode(body, SECRET, algorithms=["HS256"])  → payload dict (401 on failure)
    → DialpadCallEvent.model_validate(payload)        → typed event (422 on failure)
    → idempotency dedupe check (Postgres-backed: call_id + state + event_timestamp)
    → cc_event_bus.enqueue(event)  + Postgres write-through to webhook_events
    → return 200 immediately (<100ms target)

cc_event_bus background worker
    → call_state.update(event)                 # writes through to Postgres
    → alert_classifier.classify(event, state)  → ChicletAction | None
        # ChicletAction = Create | Update | Escalate | Resolve
    → apply ChicletAction to in-memory + Postgres state
    → push corresponding SSE event to all clients subscribed to this team
        # chiclet_created | chiclet_updated | chiclet_escalated | chiclet_resolved
    → if action triggered Slack notification (per §5.4 backoff schedule): dispatch
```

### SSE protocol

Events sent to the browser follow a consistent format:

```
event: chiclet_created
data: {"id": "...", "tier": 1, "type": "hold_alert", "chiclet": {...}}

event: chiclet_updated
data: {"id": "...", "chiclet": {...}}
# Fired for 2nd/4th+ same-day calls on Frequent/Repeated chiclets
# (count++, recap_summary appended). No tier change.

event: chiclet_escalated
data: {"id": "...", "from_tier": 2, "to_tier": 1, "reason": "third_call_today", "chiclet": {...}}
# T2 Frequent → T1 Repeated in-place mutation, or T2 Hold → T1 Hold on cumulative-time threshold cross.
# Browser keeps the same chiclet DOM element, swaps border/tier classes, repositions in rail.

event: chiclet_resolved
data: {"id": "...", "resolved_by": "auto_hangup" | "manual_ack" | "day_boundary"}

event: call_state_update
data: {"calls": [...], "agents": [...]}

event: status_bar_update
data: {"agents_on_duty": 8, "calls_in_queue": 3, ...}

event: toast
data: {"message": "QA score: Diana L. — 91", "icon": "check", "source": "qa"}
```

### State management

The Command Center maintains an in-memory runtime cache per team. **Truth lives in Postgres** (`command_center` schema, defined in the SQL Migration doc) — these dataclasses are hydrated on startup and write-through on mutation, so any redeploy or crash rebuilds them losslessly from Postgres.

```python
@dataclass
class CommandCenterState:
    active_calls: dict[str, ActiveCall]                  # call_id → call state, incl. total_hold_seconds + current_hold_started_at
    agent_statuses: dict[str, AgentStatus]               # agent_id → duty/availability
    active_chiclets: dict[str, Chiclet]                  # chiclet_id → Chiclet (ordered for render by tier then timestamp)
    todays_calls: dict[str, list[CallRecord]]            # normalized E.164 → today's calls (reset at day-boundary in team TZ)
    frequent_callers_cache: dict[str, FrequentCallerEntry]  # normalized E.164 → Looker registry entry (§5.6)
    status_bar: StatusBarMetrics                         # computed from active_calls + agent_statuses
```

On initial page load, the frontend fetches `GET /cc/{team_id}/state` for the full snapshot, then subscribes to `GET /cc/{team_id}/events` for incremental updates.

A daily cron in the team's `timezone` clears `todays_calls`, emits `chiclet_resolved` for any T2-Frequent or T1-Repeated chiclets still in the rail (`resolved_by: "day_boundary"`), and resets the `calls today` status-bar metric.

---

## 7. Frontend architecture

### Technology

- **HTML/CSS/JS** served by FastAPI via Jinja2 (consistent with existing QA dashboard pages)
- **Chart.js** for sparklines and mini-charts in the analytics sidebar
- **EventSource API** for SSE connection (with automatic reconnection)
- **No build step** — CDN imports only (consistent with existing stack)
- **CSS custom properties** for theming (dark mode support via `prefers-color-scheme`)

### Key CSS

```css
/* Tier 1 — Critical: pulsing red glow */
@keyframes pulse-border {
  0%, 100% {
    border-color: var(--color-danger);
    box-shadow: 0 0 0 0 transparent;
  }
  50% {
    border-color: var(--color-danger-bright);
    box-shadow: 0 0 12px 2px rgba(226, 75, 74, 0.3);
  }
}

.chiclet.tier1 {
  border: 2px solid var(--color-danger);
  animation: pulse-border 1.5s ease-in-out infinite;
}

/* Tier 2 — Important: steady amber */
.chiclet.tier2 {
  border: 2px solid var(--color-warning);
}

/* Source stripes — hex source-of-truth is §4 Source stripe colors table */
.chiclet.src-dialpad::before  { background: #7C52FF; }  /* Dialpad purple */
.chiclet.src-sheets::before   { background: #34A853; }  /* Sheets green */
.chiclet.src-slack::before    { background: #BA7517; }  /* Slack amber */
.chiclet.src-qa::before       { background: #15192D; }  /* QA navy */
```

### SSE connection pattern

```javascript
const TEAM_ID = '{{ team_id }}';
let es;

function connectSSE() {
  es = new EventSource(`/cc/${TEAM_ID}/events`);

  es.addEventListener('chiclet_created', (e) => {
    const data = JSON.parse(e.data);
    renderChiclet(data.chiclet);
    if (data.tier === 1) playAlertSound();
  });

  es.addEventListener('chiclet_updated', (e) => {
    const data = JSON.parse(e.data);
    updateChiclet(data.id, data.chiclet);  // no tier change, just content/count
  });

  es.addEventListener('chiclet_escalated', (e) => {
    const data = JSON.parse(e.data);
    escalateChiclet(data.id, data.from_tier, data.to_tier, data.chiclet);
    if (data.to_tier === 1) playAlertSound();
  });

  es.addEventListener('chiclet_resolved', (e) => {
    const data = JSON.parse(e.data);
    collapseChiclet(data.id, data.resolved_by);
  });

  es.addEventListener('call_state_update', (e) => {
    const data = JSON.parse(e.data);
    updateActivityTable(data.calls, data.agents);
  });

  es.addEventListener('status_bar_update', (e) => {
    const data = JSON.parse(e.data);
    updateStatusBar(data);
  });

  es.addEventListener('toast', (e) => {
    const data = JSON.parse(e.data);
    showToast(data);
  });

  es.onerror = () => {
    es.close();
    setTimeout(connectSSE, 3000); // Reconnect with backoff
  };
}

// Initial load
fetch(`/cc/${TEAM_ID}/state`)
  .then(r => r.json())
  .then(state => {
    renderFullState(state);
    connectSSE();
  });
```

---

## 8. Testing strategy

Testing follows the same patterns established in the QA scoring test suite: pure function unit tests with synthetic data factories, integration tests via FastAPI TestClient, and gated live-service smoke tests.

### New conftest.py fixtures

```python
def make_dialpad_call_event(
    call_id: str = "test-call-123",
    state: str = "hangup",
    direction: str = "inbound",
    external_number: str = "+15125550184",
    duration_ms: float = 120000,
    target_email: str = "agent@landing.com",
    entry_point_call_id: str | None = None,
    **overrides,
) -> dict:
    """Generate a synthetic Dialpad call event payload."""
    ...

def make_agent_status_event(
    agent_email: str = "agent@landing.com",
    on_duty_status: str = "available",
    call_center_ids: list[str] | None = None,
    **overrides,
) -> dict:
    """Generate a synthetic Dialpad agent status event."""
    ...

def make_sheets_update_event(
    sheet_name: str = "Maintenance Tickets",
    row_data: dict | None = None,
    priority: str = "Urgent",
    **overrides,
) -> dict:
    """Generate a synthetic Google Sheets row-added event."""
    ...

def make_frequent_callers_snapshot(
    numbers: list[tuple[str, str]] | None = None,
    # default: [("+15125550199", "habitual escalator"), ...]
) -> dict[str, "FrequentCallerEntry"]:
    """Build an in-memory frequent-callers registry snapshot (stand-in for a
    Looker API response) so tests can exercise §5.6 lookup logic without I/O."""
    ...
```

### Test modules

#### `test_alert_classifier.py`

Pure function tests. No I/O.

- `test_hold_over_5min_is_tier1` — cumulative `total_hold_seconds` > 300 → Tier 1
- `test_hold_under_3min_is_no_alert` — cumulative < 180s → None
- `test_hold_3_to_5min_is_tier2` — cumulative between thresholds → Tier 2
- `test_third_call_same_number_is_tier1_repeated` — non-Looker caller, 3rd call today → fresh T1
- `test_second_call_same_number_is_no_repeated` — 2nd call today → no Repeated chiclet
- `test_qa_outlier_is_tier2` — SPC-flagged evaluation
- `test_routine_qa_score_is_tier3` — non-outlier evaluation
- `test_sheets_urgent_ticket_is_tier2` — priority=Urgent
- `test_tier_can_escalate_not_downgrade` — Tier 2 chiclet → Tier 1 on threshold cross; downgrade is no-op
- `test_escalation_returns_escalate_action` — classifier emits `ChicletAction.Escalate(from=2, to=1)` (drives `chiclet_escalated` SSE)

#### `test_webhooks_route.py`

Integration tests via TestClient.

- `test_valid_jwt_body_returns_200` — POST body IS the JWT; correctly signed → decoded → processed
- `test_invalid_jwt_body_returns_401` — malformed or wrong-secret JWT body → 401
- `test_non_jwt_body_returns_401` — plain JSON body (no JWT envelope) → 401
- `test_pydantic_validation_returns_422` — JWT decodes but payload missing required fields → 422
- `test_duplicate_event_is_idempotent` — same `call_id`+`state`+`event_timestamp` posted twice → only one event processed (dedupe via Postgres)
- `test_dedupe_survives_restart` — second post arrives after the worker restarts → still deduplicated (Postgres-backed, not in-memory)
- `test_webhook_returns_200_before_processing` — response time < 100ms (doesn't block on scoring/alerting)

#### `test_call_state.py`

Pure function tests for call graph resolution and state tracking.

- `test_entry_point_call_links_to_operator` — operator_call_id resolves to entry_point
- `test_transfer_chain_resolves_via_master_call_id` — transferred call links back to original
- `test_hold_starts_total_and_current_timers` — `state=hold` sets both `total_hold_seconds` accumulator and `current_hold_started_at`
- `test_reconnect_flushes_current_keeps_total` — `state=connected` clears `current_hold_started_at`, freezes addition to `total_hold_seconds`
- `test_cumulative_hold_across_cycles` — hold(2:30) → connected(0:30) → hold(2:30) → `total_hold_seconds == 300` (T1 threshold)
- `test_brief_reconnect_resets_slack_backoff` — Slack backoff counter resets on connect, NOT cumulative
- `test_call_removed_on_hangup` — cleanup after call ends

#### `test_repeated_callers.py`

Pure function tests for today's-calls counters (day-boundary in team TZ).

- `test_third_call_today_triggers` — fresh T1 Repeated detection for non-Looker number
- `test_calls_from_yesterday_excluded` — day-boundary reset works (calls before midnight in `America/Mexico_City` don't count toward today)
- `test_different_numbers_independent` — no cross-contamination
- `test_recap_summary_aggregation` — prior calls' `recap_summary` concatenated in order
- `test_fourth_call_appends_no_new_chiclet` — 4th+ call same day → `chiclet_updated`, no `chiclet_created`
- `test_day_boundary_emits_resolved` — midnight cron emits `chiclet_resolved` (resolved_by="day_boundary") for active Repeated chiclets

#### `test_frequent_callers.py`

Pure function tests for the Looker registry path and escalation flow.

- `test_known_number_first_call_creates_tier2` — inbound from Looker-flagged number → T2 Frequent chiclet
- `test_second_call_emits_chiclet_updated` — 2nd call same day → same chiclet, count=2, `chiclet_updated` SSE; no new chiclet
- `test_third_call_escalates_in_place` — 3rd call → tier mutates T2 → T1, same chiclet ID, `chiclet_escalated` SSE emitted with `from_tier=2, to_tier=1`
- `test_fourth_plus_appends_recap_only` — 4th+ → `chiclet_updated`, no re-escalation
- `test_unknown_number_no_frequent_chiclet` — number not in Looker → no T2 (Repeated path still applies independently)
- `test_check_on_miss_queries_looker_when_stale` — number not in cache AND cache age > `check_on_miss_max_age_seconds` → single targeted Looker API call before classification
- `test_check_on_miss_skipped_when_cache_fresh` — fresh cache + unknown number → no Looker call, classifies as unknown
- `test_looker_api_failure_keeps_last_cache` — Looker API errors during refresh → cache unchanged, Frequent detection continues
- `test_system_never_writes_to_looker` — even on T2 → T1 escalation, no write call to Looker SDK (VP-curated invariant)

#### `test_phone_normalization.py`

Pure function tests for `utils.phone.normalize()` (shared helper, AI-Scoring backend).

- `test_e164_passthrough` — already-normalized number returned unchanged
- `test_us_10_digit_to_e164` — "5125550199" → "+15125550199"
- `test_mexico_domestic_to_e164` — Mexican domestic format → "+52..."
- `test_strips_extensions` — "+15125550199x1234" → "+15125550199"
- `test_strips_formatting` — "(512) 555-0199" → "+15125550199"
- `test_empty_or_invalid_raises` — empty string / non-numeric → ValueError (defense in depth — should never reach this from a Dialpad payload)

#### `test_slack_backoff.py`

Pure function tests for the exponential-backoff scheduler (§5.4).

- `test_first_notification_at_5min` — fires at `current_hold_started_at + 5min`
- `test_backoff_doubles` — sequence is 5 → 10 → 20 → 40, doubling
- `test_backoff_resets_on_reconnect` — `state=connected` clears the schedule; next hold restarts at 5min
- `test_repeated_caller_single_dm` — T1 Repeated creation emits exactly one DM, no further DMs for 4th+ calls same day
- `test_escalation_from_frequent_emits_dm` — T2 → T1 escalation triggers the same single Repeated DM as a fresh T1 would

#### `test_sse_fanout.py`

Integration tests for SSE event delivery.

- `test_chiclet_created_reaches_all_clients` — fan-out to multiple connected browsers
- `test_chiclet_escalated_reaches_all_clients` — escalation event delivered
- `test_chiclet_updated_reaches_all_clients` — update event delivered
- `test_reconnect_gets_current_state` — client disconnect → reconnect → full state snapshot
- `test_events_scoped_to_team` — member_support events don't leak to sales clients

---

## 9. Phased implementation plan

### Phase 0 — SQL Migration design doc (prerequisite, separate session)

**Goal:** Define the `command_center` Postgres schema (plus `qa` and `embeddings` schemas for the QA pipeline migration + RAG groundwork) before Phase 1 code starts.

**Why a prerequisite:** Phase 1 services (`repeated_callers`, `frequent_callers`, `call_state`, webhook idempotency) all write through to Postgres. Sketching tables inline in this doc is shallow; they need their own design pass alongside the QA-Sheets-to-Postgres migration and the RAG embeddings layout. See §12 → SQL Migration design doc (TBD path).

### Phase 1 — Webhook ingestion + alert rail + minimal HTML (Weeks 1–3)

**Goal:** Dialpad webhooks flowing into the backend; Frequent / Repeated / Hold chiclets fully working end-to-end; a single-zone HTML page (alert rail only) so the system is observable in a browser, not just `curl -N` on the SSE stream.

**Code deliverables:**
- `routes/webhooks.py` — JWT-body decode + Pydantic validation (§5.1)
- `routes/command_center.py` — `GET /cc/{team_id}` (minimal HTML), `GET /cc/{team_id}/events` (SSE), `GET /cc/{team_id}/state` (snapshot)
- `models/call_event.py`, `models/frequent_caller.py`, `models/alert.py`
- `services/call_state.py` — `total_hold_seconds` + `current_hold_started_at`, call-graph resolution
- `services/alert_classifier.py` — pure functions emitting `ChicletAction.Create | Update | Escalate | Resolve`
- `services/cc_event_bus.py` — in-memory pub/sub + SSE fan-out, separate from AI-Scoring's `event_bus.py`
- `services/repeated_callers.py` — today's-calls counters, day-boundary reset cron in team `timezone`
- `services/frequent_callers.py` — Looker scheduled refresh + check-on-miss
- `services/slack_outbound.py` — exponential backoff scheduler for hold notifications (§5.4)
- `qa-automation/AI-Scoring/backend/utils/phone.py` — shared `normalize()` helper (imported by both apps)
- `command-center/templates/command_center_min.html` — alert rail only (Zone B). No status bar, no activity feed, no analytics sidebar — those land in Phase 2.

**Test deliverables (all in Phase 1):**
`test_alert_classifier.py`, `test_webhooks_route.py`, `test_call_state.py`, `test_repeated_callers.py`, `test_frequent_callers.py`, `test_phone_normalization.py`, `test_slack_backoff.py`, basic `test_sse_fanout.py` (full version in Phase 2).

**Infrastructure requirements:**
- **Postgres on Railway.** Either reuse an existing add-on or provision one; `command_center` schema is created by the SQL Migration doc.
- **Railway service Watch Paths change** (one-time). Extend from `qa-automation/AI-Scoring/` to also include `command-center/`. Without this, commits that only touch `command-center/` are auto-skipped by Railway and the deploy silently never happens.
- Public Dialpad webhook URL: `https://<railway-host>/api/webhooks/dialpad` (served by the AI-Scoring FastAPI app with CC routers mounted — see §6 Deployment).
- For local dev: ngrok tunnels a local port to receive Dialpad webhooks.

### Phase 2 — Full dashboard UI + Sheets integration (Weeks 4–6)

**Goal:** Promote the minimal HTML to the full 4-zone layout; wire up Sheets monitoring and the QA SSE bridge.

**Deliverables:**
- `command_center.html` — adds Zone A (status bar), Zone C (live activity feed), Zone D (analytics sidebar) on top of Phase 1's alert rail
- `services/sheets_monitor.py` — Apps Script `onEdit` webhook receiver (preferred) with `gspread` polling fallback
- Bridge to AI-Scoring's existing SSE stream for `score_complete` → CC `qa_outlier` chiclets / Tier 3 toasts
- Toast notification stack for Tier 3 events
- Status bar metric computation + `status_bar_update` SSE event
- `test_sse_fanout.py` full version, sheets-monitor tests

### Phase 3 — Call management + Slack workflows (Weeks 7–9)

**Goal:** Interactive call transfer/merge controls, Slack context forwarding, mass notification chiclets.

**Deliverables:**
- Expandable activity-table rows with Transfer, Merge, Send Context actions
- Slack DM integration for forwarding call context to transferees
- Mass notification Slack workflow monitoring
- Call graph visualization for complex transfer chains

### Phase 4 — Dialpad WebSocket + pre-computed analytics (Weeks 10–12)

**Goal:** Lowest-latency agent status via WebSocket; pre-computed dashboard metrics for snappier initial loads.

**Deliverables:**
- WebSocket client for Dialpad agent status events (server-side, relayed via SSE)
- Pre-computed dashboard metrics (EWMA, SPC) written on new evaluation, read on page load — depends on QA pipeline's Sheets → Postgres migration being complete
- 1-hour WebSocket token refresh with automatic reconnection
- (Note: Postgres for CC state lands in Phase 1, not here — moved per §5.1 Persistence)

### Phase 5 — Local AI + advanced features (Future)

- Profanity detection via local inference (Gemma/Ollama)
- Automated call scoring triggered by Dialpad `recording` webhook (replaces manual upload), backed by RAG over the SOP embeddings (depends on RAG groundwork in the SQL Migration doc / `embeddings` schema)
- MCP server wrapping the Command Center for agent orchestration
- Context-aware mass notification auto-fill from Slack workflow data

---

## 10. Configuration schema

The Command Center extends the existing per-team JSON config pattern:

```json
{
  "command_center": {
    "enabled": true,
    "timezone": "America/Mexico_City",
    "thresholds": {
      "hold_tier2_seconds": 180,
      "hold_tier1_seconds": 300,
      "repeated_caller_count": 3,
      "qa_outlier_sigma": 2.0,
      "agents_on_duty_amber_pct": 50,
      "agents_on_duty_red_pct": 25,
      "queue_depth_amber": 3,
      "queue_depth_red": 6,
      "avg_wait_amber_seconds": 120,
      "avg_wait_red_seconds": 240
    },
    "sheets_monitors": [
      {
        "sheet_id": "...",
        "tab_name": "Maintenance Tickets",
        "key_columns": {"unit": "A", "issue": "B", "priority": "C"},
        "poll_interval_seconds": 60,
        "default_tier": 2
      }
    ],
    "slack": {
      "alert_channel_id": "C...",
      "mass_notification_channel_id": "C...",
      "hold_backoff_minutes": [5, 10, 20, 40],
      "tier1_notify": true,
      "tier2_notify": false
    },
    "dialpad": {
      "webhook_secret": "ENV:DIALPAD_WEBHOOK_SECRET",
      "target_call_center_ids": ["..."],
      "monitored_states": [
        "ringing", "connected", "hold", "hangup",
        "recording", "call_transcription", "recap_summary"
      ]
    },
    "looker": {
      "base_url": "https://landing.looker.com",
      "client_id": "ENV:LOOKER_CLIENT_ID",
      "client_secret": "ENV:LOOKER_CLIENT_SECRET",
      "frequent_callers_look_id": "...",
      "refresh_interval_seconds": 900,
      "check_on_miss_max_age_seconds": 120
    }
  }
}
```

Notes:
- `timezone` drives the day-boundary reset for `todays_calls`, Repeated detection, Frequent call counts, and the `calls today` status-bar metric. Default is `America/Mexico_City` (UTC-6, no DST per Mexican federal decree). Per-team override supported for future non-MX teams.
- `repeated_caller_window_hours` was removed in favor of calendar-day semantics — see §4 and §5.1.
- `hold_backoff_minutes` defines the Slack DM schedule per current hold cycle (§5.4). The array is interpreted literally for the first N entries, then doubles indefinitely past the last value.

---

## 11. Key design principles

1. **Three tiers, no more.** If you're tempted to add a fourth tier, reconsider whether the event is truly distinct from an existing tier.

2. **Chiclets are action units, not just notifications.** Every chiclet must answer: what happened, who's involved, what can I do about it, and how urgent is it.

3. **The webhook handler returns 200 first, processes second.** Dialpad retries on non-200 responses. All processing is async.

4. **Pure functions for classification, I/O at the edges.** `alert_classifier.py` takes data in and returns tier + chiclet type. It doesn't know about Dialpad, Sheets, or SSE. This is what makes it testable.

5. **SSE for dashboard updates, webhooks for ingestion.** The browser never talks to Dialpad directly. The backend is the single source of truth.

6. **Config-driven thresholds.** All timer durations, sigma thresholds, and color breakpoints are in the team's JSON config, not hardcoded. Adding a team is a config change, not a code change (same principle as the QA scoring app).

7. **Existing patterns win.** Use the same `conftest.py` fixture factory pattern, the same `data_provider.py` abstraction, the same Pydantic validation, the same `monkeypatch` testing approach. The Command Center is an extension of the QA app, not a separate system.

8. **Postgres is the source of truth for any state that must survive a redeploy.** In-memory dicts (`todays_calls`, `frequent_callers_cache`, `active_chiclets`, idempotency dedupe set, hold accumulators) are runtime caches only — every mutation writes through to Postgres, and the caches are rebuilt from Postgres on startup. A Railway redeploy in the middle of a busy shift must not lose a hold timer, a repeated-caller count, or a webhook dedupe entry. See §5.1 Persistence and the SQL Migration design doc.

9. **Phone numbers always pass through `utils.phone.normalize()`.** E.164 at every boundary — webhook ingest, Looker cache load, repeated-caller keys, Slack message rendering, scoring deep-links. Raw `external_number` strings are never compared directly, because Dialpad returns them inconsistently and the Looker registry is authored by humans.

---

## 12. Reference links

- [Dialpad Call Events documentation](https://developers.dialpad.com/docs/call-events)
- [Dialpad Agent Status Events](https://developers.dialpad.com/docs/agent-status-events)
- [Dialpad WebSocket Event Subscriptions](https://developers.dialpad.com/docs/event-subscriptions-via-websocket)
- [Dialpad Webhook Creation API](https://developers.dialpad.com/reference/webhookscreate)
- [Smashing Magazine — UX Strategies for Real-Time Dashboards](https://www.smashingmagazine.com/2025/09/ux-strategies-real-time-dashboards/)
- [Brightmetrics — How to Build Contact Center Wallboards](https://brightmetrics.com/blog/contact-center-wallboards-dashboards/)
- [Looker API 4.0 authentication](https://cloud.google.com/looker/docs/api-auth)
- [Looker SDK for Python (`looker-sdk`)](https://github.com/looker-open-source/sdk-codegen/tree/main/python)
- Existing QA app: `qa-automation/AI-Scoring/backend/services/team_stats.py` (analytics engine, with long-form `coverage_regime` groundwork for the ~July 2026 Local AI cutover), `qa-automation/AI-Scoring/backend/services/event_bus.py` (SSE infrastructure)
- This Command Center: `command-center/` at repo root (mounted into the AI-Scoring FastAPI app — see §6 → Deployment)
- Shared phone normalization helper: `qa-automation/AI-Scoring/backend/utils/phone.py` (imported by both apps)
- Existing on-disk schema work: `database/migrations/` in the repo root (Mass Notification tool schema + stubbed `003_qa_scoring_schema.sql` from a prior QA-migration attempt — these will be reviewed and refactored in the SQL Migration session, not adopted as-is)
- **SQL Migration design doc** (drafting in next session): defines the `command_center`, `qa`, and `embeddings` schemas across one shared Postgres instance. Gates Phase 1 of this doc. Path TBD (proposed: `qa-automation/AI-Scoring/references/SQLMigration.md` or `database/SQLMigration.md`).
