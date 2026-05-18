# Lookup-to-Score — design doc

> **Purpose:** Replace manual "download .mp3 → enter call_id → upload" with a
> one-click "Score this call" action from `/lookup`. Adds a privileged-key tier
> so non-team-bound evaluators (HR, MGMT, debugging) can score cross-team,
> while team keys remain scoped to their own Mails roster.
> **Author session:** 2026-05-18.
> **Status:** Design — not yet implemented. Bug-fix prerequisites landed
> in the current branch (`feat/webapp-landing-styling`): Dialpad semaphore
> + `download_recording()` + handler-side metadata pre-fetch.

---

## Decisions locked (2026-05-18)

| Decision | Choice | Notes |
|---|---|---|
| Auth tier | One privileged role to start | `API_KEY_PRIVILEGED`. Split into HR/MGMT later if permissions diverge. |
| Unassigned agent (privileged caller) | Frontend dialog forces team pick | Backend just validates the supplied `team_id` is real; no implicit defaulting. |
| Endpoint shape | Extend `/score` (audio_file optional) | Single endpoint, two input modes. `download_recording()` handles the call-id-only path. |
| `/lookup` read-access | Stays cross-team for all valid keys | Matches existing `project_lookup_design` posture. Only the **action** (scoring) is gated. |

---

## Goal + motivation

**Today**, scoring a call requires the analyst to:
1. Pull the call recording out of Dialpad (or use the recording share-link).
2. Download the `.mp3`.
3. Paste the call_id into the scoring form.
4. Upload the audio.

Each step is a place to lose the call_id, grab the wrong file, or get blocked
by Dialpad permissions on the share-link UI.

**After this work**, the analyst opens `/lookup`, finds the agent, clicks
"Score Call" on the row, and the backend handles the recording fetch and
scoring pipeline. Manual upload stays available as a fallback for calls
without recordings or recordings outside Dialpad's retention.

**The new auth surface** prevents a Member Support analyst from scoring a
Sales agent's calls (or vice versa) by accident. Privileged keys
(`API_KEY_PRIVILEGED`, used by leadership/HR/dev) can score across teams
and must explicitly choose the target team when the agent isn't rostered.

---

## Scope

**In scope (this design):**
- New `API_KEY_PRIVILEGED` env var + role concept in `middleware/auth.py`.
- `email_in_team_mails()` helper backed by the existing Mails cache.
- Agent→team resolver (small endpoint + frontend usage).
- `/score` extended: `audio_file: Optional[UploadFile]`. If absent, fetch
  via `download_recording(call_id)`.
- `Score_Audit` sheet tab (timestamp / role / evaluator / agent / call_id /
  target_team / action).
- Per-API-key concurrent-jobs semaphore (in-memory, 5 concurrent default).
- Frontend `/lookup`: split "Generate" into "Share Link" and "Score Call";
  per-row enable rules; team-pick dialog for privileged + unrostered agents.

**Out of scope (deferred):**
- Per-evaluator identity (still client-supplied `manager_email`; tracked
  alongside API-key role in `Score_Audit`).
- HR/MGMT role split (one `PRIVILEGED` role for now).
- Postgres-backed audit log (Sheets-first, migrate in Phase 2 schema work
  if/when audit volume justifies it — see `PhaseTwo.md`).
- Restricting privileged keys by Dialpad callcenter assignment (future
  refinement called out in user proposal).
- Per-evaluator rate limits beyond concurrent-jobs cap (token-bucket layer
  can come later if abuse appears).

---

## Auth model

### Env-var inventory

| Var | Role | Team binding |
|---|---|---|
| `API_KEY_MEMBER_SUPPORT` | `team` | `member_support` |
| `API_KEY_SALES` | `team` | `sales` |
| `API_KEY_PRIVILEGED` | `privileged` | none (cross-team) |

`_build_key_map()` in `backend/middleware/auth.py` currently returns
`{token: team_id}`. Becomes:

```python
@dataclass(frozen=True)
class KeyIdentity:
    role: Literal["team", "privileged"]
    team_id: Optional[str]  # None for privileged
```

`_PRIVILEGED_SUFFIXES = {"privileged"}` — env-var suffix `PRIVILEGED`
maps to `(role="privileged", team_id=None)`; any other suffix is a team key.

### New dependencies

- `require_api_key` → returns `KeyIdentity` (was `str` / team_id).
- `require_team_access(team_id)` → unchanged for routes that should stay
  team-locked (`/team/...`, `/agents/...`, `/team/sections`); rejects
  privileged keys too? **Decision:** privileged keys are allowed through
  team-locked routes (they bypass the team check). One bypass rule,
  applied everywhere.
- `require_scoring_access(team_id, agent_email)` — **new**. Used by
  `/score`. Logic:
  - Team key: `key.team_id == team_id AND email_in_team_mails(agent_email, team_id)`.
  - Privileged key: `team_id` must be a real team (member_support/sales);
    `agent_email` does NOT need to be in that team's Mails (privileged
    bypass — the frontend confirmed the operator's intent via the team-pick
    dialog).

### `email_in_team_mails(email, team_id) -> bool`

Lives in `backend/services/history_service.py` (next to the existing
`_get_mails_sheet` and `_mails_cache_key`). Uses the cached Mails fetch
(TTL=300). Matches against the **active** subset only — same semantics as
`team_dashboard`'s `active_only=true`.

---

## Route contracts

### `POST /api/{team_id}/score` (extended)

**Body (multipart/form-data):**

| Field | Type | Required | Notes |
|---|---|---|---|
| `audio_file` | `UploadFile` | **No** (was Yes) | If omitted, backend calls `download_recording(call_id)`. |
| `call_id` | `str` | Yes | Unchanged. |
| `agent_email` | `str` | **No (new)** | If supplied, server resolves agent_name from Mails. Preferred over `agent_name`. |
| `agent_name` | `str` | No (was Yes) | Kept for backward compat with the existing manual-upload form. Required if `agent_email` is absent. |
| `manager_email` | `str` | Yes | Unchanged. |
| `duration_ms` | `float` | No | Unchanged. |

**Auth:** `require_scoring_access(team_id, agent_email_or_resolved_from_name)`.

**Response (unchanged):** `{ "job_id": str, "status": "pending" }`.

**Behavior changes:**
- If `audio_file` is missing → call `download_recording(call_id)`. On
  `NoRecordingAvailable` → 422 with `"Call has no recording in Dialpad"`.
  On `DialpadRateLimited` → 503 with retry hint.
- If existing in-memory `_jobs[key]` is `"pending"` or `"scoring"`, return
  that job_id (idempotent against double-click) instead of starting a new
  run.
- Before scheduling background task, append a `Score_Audit` row.

### `GET /api/{team_id}/lookup/scoring-permission?email={agent_email}` (new)

Lightweight helper for the frontend so the Score Call button can render
enabled/disabled per-row without the client re-implementing the Mails
membership rule.

**Auth:** `require_api_key` (any valid key).

**Response:**
```json
{
  "agent_email": "luis.rubio@hellolanding.com",
  "resolved_team": "member_support",          // null if not rostered
  "can_score": true,                          // for THIS key's role + team
  "needs_team_pick": false                    // true if privileged + unrostered
}
```

Server logic:
- Look up `email_in_team_mails(email, t)` for each configured team.
- `resolved_team` = the first team where found (or null).
- For team key: `can_score = (resolved_team == key.team_id)`.
- For privileged key: `can_score = true`; `needs_team_pick = (resolved_team is None)`.

---

## Agent→team resolution

Lives in `history_service.py` next to the Mails helper:

```python
def resolve_team_for_agent(email: str) -> Optional[str]:
    """Return the team_id whose Mails roster contains `email`, or None."""
```

Iterates configured teams (from `team_config.get_all_team_ids()` or
similar — check what exists). Returns first match. None if unrostered.

If we ever support multi-team agents, this needs a richer return type;
deferred until a real case appears.

---

## `Score_Audit` sheet schema

New tab on the **member_support** spreadsheet (single source for now;
revisit if Sales auditing needs to diverge). One row per `/score` POST
that passes auth — write **before** the background task is scheduled so
denials are recorded too.

| Column | Field | Notes |
|---|---|---|
| A | `timestamp` | ISO 8601 UTC |
| B | `api_key_role` | `team` / `privileged` |
| C | `evaluator_email` | From `manager_email` form field |
| D | `agent_email` | Resolved or supplied |
| E | `agent_name` | Display name from Mails or form |
| F | `call_id` | Dialpad call ID |
| G | `target_team` | Which team's pipeline ran |
| H | `action` | `scored` (kicked off scoring) / `denied` (auth failure) / `approved` (Stage 4 finalize) |
| I | `result_row` | FR-AI row number on success; empty on denial |
| J | `notes` | e.g., `"no_recording"`, `"rate_limited"`, `"unrostered_privileged"` |

Writes go through `sheets_service` with a new helper `append_score_audit_row()`.

---

## Idempotency + rate limiting

**Idempotency** — in `routes/scoring.py`:

```python
existing = _jobs.get(key)
if existing and existing["status"] in {"pending", "scoring"}:
    return {"job_id": job_id, "status": existing["status"]}  # don't restart
```

Sheets side already handles re-submission via the "Overwrote FR-AI row
for dialpad_link match" path (`sheets_service.py:289`), so a job that
completes twice doesn't double-write.

**Rate limiting** — per-API-key concurrent-jobs semaphore in
`routes/scoring.py`:

```python
_key_semaphores: dict[str, asyncio.Semaphore] = {}
_KEY_CONCURRENT_LIMIT = 5

def _semaphore_for_key(token: str) -> asyncio.Semaphore:
    return _key_semaphores.setdefault(token, asyncio.Semaphore(_KEY_CONCURRENT_LIMIT))
```

Acquired around the `score_call(...)` await inside the background task,
not the HTTP handler — so the response returns immediately but the actual
Gemini scoring queues behind the cap.

---

## Frontend changes (lookup.html)

**Per-row "Generate" cell becomes two buttons:**

- **Share Link** — current behavior (`POST /lookup/recording-link`).
- **Score Call** — new. Disabled if `was_recorded == false` OR
  (`/lookup/scoring-permission` says `can_score == false`).

**Click flow for "Score Call":**

1. Fetch `/lookup/scoring-permission?email={evaluator_email_or_agent_email}`.
2. If `needs_team_pick`: show modal with team options
   (member_support / sales). User picks; chosen `team_id` carried to step 3.
3. Else: use `resolved_team` directly.
4. POST `/api/{team_id}/score` with `call_id` + `agent_email` +
   `manager_email`, no `audio_file`.
5. Receive `job_id`, poll `/score/{job_id}` (same loop as `index.html` already uses).
6. On `status: complete`, deep-link to the scorecard editor view.

**State for manager_email:** session-stored on the lookup page (same as
the API key) — analyst types it once per session.

---

## Implementation sequence (PR breakdown)

Each step is reviewable in isolation; mergeable without the next.

| PR | Scope | LoC est. | Touch |
|---|---|---|---|
| 1 | Auth tier (privileged role) + `email_in_team_mails` helper + `resolve_team_for_agent` | ~120 | `middleware/auth.py`, `services/history_service.py`, tests |
| 2 | `Score_Audit` sheet schema + `append_score_audit_row()` helper + manual sheet-tab creation steps in CONTRIBUTING.md or a one-shot script | ~80 | `services/sheets_service.py`, `scripts/init_score_audit_tab.py` |
| 3 | `/score` extension: optional `audio_file` + `download_recording` fallback + idempotency check + audit-row write + per-key job semaphore | ~120 | `routes/scoring.py`, `services/scoring_service.py` |
| 4 | `/lookup/scoring-permission` endpoint | ~60 | `routes/lookup.py` |
| 5 | Frontend: split Generate button, team-pick modal, polling wire-up | ~200 | `frontend/lookup.html` |
| 6 | End-to-end smoke test script (uses real privileged key, real call_id) + CONTRIBUTING.md update | ~80 | `scripts/score_by_call_id.py`, `CONTRIBUTING.md` |

**Total:** ~660 LoC across 6 PRs, no single PR > 200 LoC.

**Pytest checkpoints (per `feedback_design_doc_first`):**
- After PR 1: `test_email_in_team_mails`, `test_resolve_team_for_agent`,
  `test_require_scoring_access_team_key_blocks_cross_team`,
  `test_require_scoring_access_privileged_bypasses`.
- After PR 3: `test_score_endpoint_without_audio_fetches_recording`,
  `test_score_endpoint_idempotent_on_in_flight_job`,
  `test_score_endpoint_writes_audit_row`.
- After PR 4: `test_scoring_permission_team_key_in_roster`,
  `test_scoring_permission_privileged_unrostered_needs_team_pick`.

---

## Open questions / future work

1. **`Score_Audit` location.** Member-support spreadsheet now; if Sales
   audit volume grows or auditors want team-scoped tabs, split per team.
   Tracked as Phase 2 follow-up.
2. **Per-evaluator identity.** `manager_email` stays client-supplied.
   When sessions land (future), derive from session and drop the field.
   Until then: `Score_Audit` lets us spot-check mismatches between
   API-key role and claimed evaluator_email.
3. **Privileged + unrostered + "no team makes sense" case.** If HR wants
   to score a contractor whose email isn't anywhere, the team-pick dialog
   still requires picking one. That row goes through (e.g.) Sales's
   pipeline using Sales's prompt — which may not fit. Accept as-is for
   v1; revisit when it actually happens.
4. **`/score/batch` parity.** Same audit + auth + idempotency rules apply.
   Out of scope for this design but should land in a follow-up PR.
5. **Dialpad callcenter scoping** (user-proposed refinement). Defer until
   `Score_Audit` shows real usage patterns — premature optimization
   otherwise.
6. **Recording retention.** Dialpad ages recordings out (see
   `PhaseThree.md:316-321` on the `call_started_at` backfill). Score Call
   on an aged-out call → `NoRecordingAvailable` → manual upload fallback.
   Frontend should surface that path on 422.
