# Live Dashboard via SSE — design doc

> **Purpose:** Make the team dashboard react in real time as evaluations
> are approved. Adds a server-sent-events (SSE) channel that the approval
> handler publishes to; connected dashboards show a toast for each new
> eval and refresh their charts. Closes the loop opened by PR #36 (month
> chiclets) so a viewer sitting on `/dashboard/<team>` sees new data
> arrive without manual reload.
> **Author session:** 2026-05-25.
> **Status:** Design — not yet implemented. Phase A (month chiclets)
> merged as PR #36. This doc covers Phases B–E.

---

## Decisions locked (2026-05-25)

| Decision | Choice | Notes |
|---|---|---|
| Multi-replica posture | Single replica today; design for swap | In-memory `asyncio.Queue` bus, wrapped behind an `EventBus` interface so a Redis impl is a one-file change later. |
| SSE update strategy | Server pushes thin event → client refetches `/team/stats` | One aggregation path in the codebase. Server stays stateless about client filter state. Debounced 3s burst window so a flurry of approvals = one refetch. |
| Auth on the SSE stream | `?api_key=` query param | Extend `require_api_key` (or sibling) to accept query-string keys. `EventSource` can't set Authorization headers. Internal tool; query-string logging is acceptable. |
| Phasing | A separate (merged), then B+C bundled, then D, plus E folded into B | Phase A landed standalone. B+C are intertwined (SSE infra serves chart liveness). D needs B's event history. E is a small frontend-only addition. |
| Toast lifecycle | 6s auto-dismiss, max 3 concurrent, FIFO drop | Predictable; no scrolling toast tower if a batch lands. |
| Recent-evals retention | No server-side ring buffer | On dashboard load, populate the "Recent Evals" list from the current-month chiclet's existing data; append SSE events afterward. Refresh loses history — but the source of truth is one fetch away. |

---

## Phase A recap (merged in PR #36 — context only)

- `compute_monthly_summary(df)` returns `{last, current}` blocks.
- `/team/stats` carries `monthly` field.
- `GET /team/evals?year_month=YYYY-MM` lists every eval in a month.
- Frontend: two clickable chiclets above the KPI row; new `/dashboard/<team>/evals` page; score column in that page links to `/datapoint/<team>/<call_id>`.

Phase A intentionally avoided SSE. Everything below builds on it.

---

## Phase B — SSE infrastructure + approval publish + toast

### Backend

```
backend/services/event_bus.py        — NEW.
  class EventBus(Protocol):
      async def publish(team_id: str, event: str, payload: dict) -> None
      def subscribe(team_id: str) -> asyncio.Queue[Event]
      def unsubscribe(team_id: str, queue: asyncio.Queue[Event]) -> None

  class InMemoryEventBus(EventBus):
      _subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

      async def publish(team_id, event, payload):
          for q in self._subscribers[team_id]:
              await q.put({"event": event, "data": payload})

      # subscribe/unsubscribe are sync — they just mutate the set.

  _bus = InMemoryEventBus()
  def get_event_bus() -> EventBus: return _bus
```

Single module-level singleton, accessed via `get_event_bus()`. Adding
Redis later means a new `RedisEventBus(EventBus)` class and changing
`get_event_bus()` to return it — no caller changes.

```
backend/routes/events.py             — NEW.
  GET /events/stream
    Auth: ?api_key=... (validated via the same key map as Authorization
    header). On invalid key → 401 before opening the stream.
    Returns: StreamingResponse(_stream(team_id), media_type="text/event-stream")

  async def _stream(team_id):
      q = bus.subscribe(team_id)
      try:
          yield ": connected\n\n"           # comment line, primes the stream
          while True:
              try:
                  evt = await asyncio.wait_for(q.get(), timeout=15)
                  yield f"event: {evt['event']}\ndata: {json.dumps(evt['data'])}\n\n"
              except asyncio.TimeoutError:
                  yield ": heartbeat\n\n"   # keeps proxies from closing
      finally:
          bus.unsubscribe(team_id, q)
```

Heartbeat every 15s keeps Railway's proxy alive (default idle close
~60s). `Cache-Control: no-cache` + `Connection: keep-alive` headers.

### Auth extension

`require_api_key` currently checks the `Authorization: Bearer <key>`
header. SSE can't set headers via `EventSource`, so:

```
backend/middleware/auth.py
  def require_api_key(request, ...):
      key = (
          _extract_bearer(request) or
          request.query_params.get("api_key")
      )
      ...
```

Or, less invasive, a sibling `require_api_key_loose` accepting query
params. **Decision needed (open Q1).**

### Approval-handler publish

`backend/routes/scoring.py` — `POST /score/{job_id}/approve`, after Stage
4 (`finalize_to_analyst_history` returns `history_row`) and the audit
write, before Stage 5 (Apps Script dispatch):

```python
await get_event_bus().publish(team_id, "eval_approved", {
    "eval_id": str(history_row),
    "agent": job.get("agent_name") or sc.get("agent_name") or "",
    "overall_score": overall_score,
    "summary": sc.get("call_summary", ""),
    "strengths": approval.key_strengths,
    "opportunities": approval.opportunities,
    "dialpad_link": sc.get("dialpad_link", ""),
    "timestamp": datetime.now(timezone.utc).isoformat(),
})
```

Publish AFTER the audit row is written (no event for a half-finalized
eval) and BEFORE the Apps Script call (so the toast races the email,
which is fine — the dashboard reflects approved state; the email is the
deliverable).

### Frontend (`team_dashboard.html`)

```js
// On load:
const es = new EventSource(`/api/${TEAM_ID}/events/team/${TEAM_ID}?api_key=${encodeURIComponent(getApiKey())}`);
es.addEventListener('eval_approved', e => {
  const data = JSON.parse(e.data);
  showToast(data);
  // ...client-side handlers for Phase C and D added later
});
es.addEventListener('error', () => {
  // Reconnect with exponential backoff; EventSource auto-reconnects but
  // we cap the rate so a permanently-down server doesn't spam.
});

// Toast: max 3 concurrent, 6s auto-dismiss, FIFO drop.
//        Slides in from bottom-right; click-to-dismiss.
//        Content: agent name, score (colored), one-line summary,
//        link to /datapoint/<team>/<eval_id>.
```

CSS: new `.toast-container` fixed bottom-right; `.toast` card with same
brand language as the chiclets (`border-radius: 12px`, `border: 1px
solid var(--border)`, subtle drop shadow).

### Phase B deliverables

- `backend/services/event_bus.py` (new)
- `backend/routes/events.py` (new)
- `backend/main.py` mounts events router
- `backend/middleware/auth.py` accepts `?api_key=`
- `backend/routes/scoring.py` publishes on Stage-4 success
- `frontend/team_dashboard.html` opens EventSource + shows toast
- Tests:
  - `EventBus` pub/sub round-trip with two subscribers.
  - `auth` accepts query-string key (parity with Bearer).
  - Route test: `/events/team/{id}` returns 401 without key.
  - Approval-route test: a successful `/approve` enqueues an
    `eval_approved` event on the bus (use `monkeypatch` to replace the
    bus with a capture instance).

---

## Phase C — live chart updates via debounced refetch

No new infra — purely wires Phase B's events to existing fetch logic.

### Frontend

```js
let _refetchTimer = null;
function scheduleStatsRefetch() {
  if (_refetchTimer) clearTimeout(_refetchTimer);
  _refetchTimer = setTimeout(() => {
    _refetchTimer = null;
    fetchTeamStats();          // existing function; re-renders all charts
  }, 3000);
}

es.addEventListener('eval_approved', e => {
  showToast(JSON.parse(e.data));
  scheduleStatsRefetch();      // coalesces bursts
});
```

That's it. Top-3 KPIs, SPC, EWMA, Distribution, Section Analysis,
Supervisor charts — all re-render because they all read from `teamData`
and `fetchTeamStats()` reassigns it then calls `renderTab()`.

### Phase C deliverables

- 1 file changed: `frontend/team_dashboard.html`.
- No backend, no new tests (the existing `fetchTeamStats` path is
  unchanged — we just call it more often).

### Trade-off accepted

Each approval triggers one extra `/team/stats` per active dashboard
client (after the 3s debounce). With manager_sample coverage (~30–100
evals/month/team), this is bounded — even during a "scoring sprint"
half-hour where 20 evals land back-to-back, the debounce collapses them
into ≤10 refetches per dashboard. Each refetch is one sheet read + 8
aggregator runs (~1–2s wall clock today). Acceptable.

If load becomes a concern: cache the `load_and_clean(df)` result with a
short TTL (e.g. 5s) keyed by `(team_id, sheet_etag)`. Out of scope for
this PR.

---

## Phase D — Recent Evals expandable chiclet

A third chiclet next to the existing two, showing the **N most recent
approved evals** as a compact list. Collapsed: count + most-recent
agent/score. Expanded: scrollable list of last 10 events.

### Frontend

```
- New chiclet card at the right of the chiclet row (3-column grid on
  desktop, stacked on mobile).
- Collapsed: "Recent · Mich Palacios 92.0 · 12 min ago" + expand arrow.
- Expanded: vertical list of {timestamp, agent, score (colored), summary
  truncated to 1 line, link to /datapoint/<team>/<eval_id>}.
- Backed by a client-side ring buffer (size 50) of SSE events.
- Initial population: on first `/team/stats` fetch, ALSO call
  `/team/evals?year_month=<current>` (already exists from Phase A),
  pre-fill the buffer with the 10 most recent. Subsequent SSE events
  prepend to the buffer.
```

### Backend changes

None beyond the existing `/team/evals` endpoint from Phase A.

### Phase D deliverables

- `frontend/team_dashboard.html`: third chiclet, expand/collapse JS,
  ring-buffer state, initial-load fetch.
- No new tests (frontend-only; behavior is observable via manual
  verification).

---

## Phase E — SPC chart datapoint click-through

Small, additive, frontend-only. Each monthly mean point on the SPC
control chart becomes a click target → opens
`/dashboard/<team>/evals?month=YYYY-MM` for that point's month, with the
dashboard's current `active_only` + `supervisor` filters propagated.

### Why bundle with Phase B?

Same scope of "dashboard UX polish on top of A." Ships with the toast.
Trivial to implement (Chart.js `onClick` handler reads the datapoint
index → `spc.months[index].month` → builds the URL).

### Frontend

```js
// inside renderSpcChart()
options: {
  onClick: (evt, items) => {
    if (!items.length) return;
    const i = items[0].index;
    const m = spc.months[i];
    if (!m) return;
    const params = new URLSearchParams({
      month: m.month,
      active_only: activeOnly,
    });
    if (selectedSupervisor) params.set('supervisor', selectedSupervisor);
    location.href = `/dashboard/${TEAM_ID}/evals?${params.toString()}`;
  },
  ...
}
// Cursor: change to pointer on hover over a point.
```

### Phase E deliverables

- `frontend/team_dashboard.html`: ~15 lines added to `renderSpcChart`.

---

## Implementation phases + pytest checkpoints

1. **Phase 0 — Auth extension**
   - Accept `?api_key=` query param in `require_api_key`.
   - Test: query-string key validates equivalently to Bearer header;
     missing/invalid → 401.
   - Checkpoint: `pytest tests -q` green.

2. **Phase 1 — EventBus + tests**
   - `services/event_bus.py` with `InMemoryEventBus`.
   - Test: pub/sub round-trip; multi-subscriber fanout; unsubscribe
     removes from set.
   - No route wiring yet. Checkpoint green.

3. **Phase 2 — SSE endpoint + approval publish**
   - `routes/events.py` + mount in `main.py`.
   - `routes/scoring.py` calls `bus.publish` on Stage-4 success.
   - Test: approval route enqueues event (monkeypatch bus); SSE route
     401 without key.
   - Checkpoint green.

4. **Phase 3 — Frontend toast + SSE wiring**
   - `team_dashboard.html`: EventSource, toast container, toast queue,
     reconnect strategy.
   - Manual verification: approve a call, dashboard shows toast.

5. **Phase 4 — Debounced refetch (Phase C)**
   - Add `scheduleStatsRefetch` + wire into SSE handler.
   - Manual verification: top-3 KPIs and charts update without page
     reload after an approval.

6. **Phase 5 — SPC click-through (Phase E)**
   - 15-line addition to `renderSpcChart`. Cursor pointer + onClick.
   - Manual verification: click a point → lands on the right
     `/evals?month=...&active_only=&supervisor=` URL.

7. **Phase 6 — Recent Evals chiclet (Phase D)**
   - Third chiclet, expand/collapse, ring buffer, initial-load
     `/team/evals?year_month=<current>` fetch.

**PR boundaries:**
- **PR-1**: Phases 0–5 (auth + bus + endpoint + publish + toast + live charts + SPC click). One cohesive "dashboard goes live" change.
- **PR-2**: Phase 6 (Recent Evals chiclet) on its own.

---

## Open questions — resolved 2026-05-25

1. **Auth extension shape:** **Uniform.** `require_api_key` accepts
   either `Authorization: Bearer <key>` or `?api_key=<key>`, applied
   across every route. Internal tool; query-string logs are tolerable.
2. **Per-eval-approved event payload bytes:** **Truncate to 280
   chars.** SSE payload trims `summary` (and applies the same cap to
   `strengths` / `opportunities` if they're similarly long) to bound
   per-client bytes per event. Full text remains available via
   `/datapoint/<team>/<eval_id>` when the user clicks through.
3. **Reconnect strategy:** **No manual cap.** Default `EventSource`
   auto-reconnect is sufficient. Revisit if we see storms in prod.
4. **Toast persistence after navigation:** **Acceptable.** Going SPA
   isn't in scope; toast destruction on navigation is fine.
5. **Other dashboards subscribing to SSE:** **Out of scope for these
   PRs.** Phase B–E wire `/dashboard/<team>` only. A future custom
   dashboard utilizing Dialpad webhooks/websockets for real-time call
   events is planned as a separate project — that data may flow into
   the agent dashboard later, but it's a different project entirely.
