# Roster management on Sandy — agents, departures, supervisors (spec)

*Design doc, 2026-08-19, written for the post-vacation session. Companion
to CoachingLoopSpec.md / CoachingTagsSpec.md. Doc-first; **no code exists
yet** — ladder AA0–AA3 below is the implementation plan. Owner intent:
retire the Google-Sheet "Mails" tab as the roster source and manage
agents, departures (never deletions — rehires are common), departure
reasons, and supervisor↔agent associations from the team dashboard.*

## 1. How roster data flows TODAY (audited 2026-08-19)

```
Google Sheet "Mails" tab (per team: A=Name, B=Email, C=Supervisor, D=Canonical)
   │
   ├─(operator runs scripts/import_agents.py --team X)──► Railway PG qa.agents
   │       upsert on (team, lower(name)); absent-from-sheet → active=FALSE;
   │       empty-read refusal rail. 155 LOC. THE ONLY WRITE PATH TODAY.
   │
   ├─(Railway RUNTIME, still live!)──► GET /{team}/mails reads the SHEET
   │       directly (provider._get_mails_sheet()), and Railway scoring
   │       resolves agent identity via Mails (routes/scoring.py:481–506).
   │
   └─ Railway PG qa.agents
          │
          └─(shadow_sync.py::sync_agents_upsert, every 30 min)──► Sandy D1 qa_agents
                  INSERT … ON CONFLICT(id) DO UPDATE — PG wins every column,
                  every 30 minutes. (Deliberately never DELETEs; sofia row
                  10000000 is Sandy-born, no PG counterpart.)
```

**Sandy D1 `qa_agents` consumers** (all read-only): `historyFrame`
(canonical/active/supervisor maps → every chart + Active Only +
supervisor filter), `/team/mails` (console dropdown; D1-backed, NOT the
sheet), scoring roster-resolve (`scoreTriggerInternal`), coaching
(`rosterAgent`, `selfAgentFor`, coverage facts), `retellSweep` (Sofia's
supervisor = reviewer), `lookup` `resolveTeamForAgent`.

**Schema today** (0001): `id, team_id, name, canonical_name, email
(NOT NULL), supervisor_email, dialpad_agent_id, active, created_at,
updated_at` + unique `(team_id, lower(name))`. **No departure reason,
date, or history** — a rehire wipes all trace that the person ever left.

## 2. "The Sheets are fully deprecated now — or are they?" — the audit

Not fully. Three couplings are still load-bearing, one is cosmetic:

| Coupling | Status | Dies when |
|---|---|---|
| **Mails tab as roster source** (import_agents.py; Railway `/mails` + scoring identity read the sheet LIVE at runtime) | **ALIVE — the roster's system of record** | **This spec (AA)** for Sandy; Railway's runtime reads die at cutover |
| **Analyst_History projection** — Railway still WRITES a row on every finalized eval (`sheets_projection.py`, called from routes/scoring.py) | ALIVE (write-side) | Cutover (Railway stops scoring) |
| **GAS row mode** — Railway-born scorecard emails still read the Analyst_History row (`Main.js` row mode; Sandy uses payload mode, zero sheet reads) | ALIVE for Railway-born sends only | Cutover (then row mode is dead code in Main.js) |
| dashboard.html footer: `Data source: Google Sheets` | **Cosmetic relic — it's D1** | AA0 (one line) |

So: Sandy never reads a sheet. Railway still reads Mails at runtime and
writes Analyst_History. The Mails tab remains the only way a human edits
the roster — which is exactly what AA replaces.

## 3. The critical design problem: sync authority

Any roster edit made on Sandy to a **Railway-range agent row (id < 10M)
is clobbered within 30 minutes** — `sync_agents_upsert` is `ON
CONFLICT(id) DO UPDATE` (PG wins). A departure marked on the new screen
would silently un-depart at the next tick. **AA0 flips roster authority
to Sandy:**

- `sync_agents_upsert` becomes **INSERT-only** (`ON CONFLICT(id) DO
  NOTHING`): brand-new PG-side agents still arrive (protects the eval
  FK import — the CL0 failure class), but Sandy's edits are never
  overwritten again. PG-side *updates* stop propagating — intended: the
  screen becomes the roster tool, the Mails tab freezes.
- New agents created on Sandy take **high-range ids** (`MAX(id)+1 >=
  10_000_000`, the house pattern) so PG inserts can never collide.
- Residual-shadow edge (documented, accepted): an agent added ONLY on
  Sandy can't be scored ON RAILWAY (Railway resolves identity from the
  sheet). Ops runs on Sandy now; if a Railway score is ever needed for
  a new agent during residual shadow, add them to the Mails tab too.
- At cutover: delete `sync_agents_upsert` entirely.

D1-only columns (0010, below) are safe under the sync: the upsert
builds its column list from PG's information_schema, so D1 extras are
never touched.

## 4. Schema — migration `0010_roster_management.sql` (next session)

```sql
-- Departure semantics (soft, reversible — rehires are common):
ALTER TABLE qa_agents ADD COLUMN departure_reason TEXT
    CHECK (departure_reason IS NULL OR departure_reason IN
        ('left_company','other_team','on_leave','terminated','other'));
ALTER TABLE qa_agents ADD COLUMN departed_at TEXT;      -- ISO, LA-day honest
ALTER TABLE qa_agents ADD COLUMN departure_note TEXT;

-- Roster history (Sandy-only, never synced): every add/depart/rehire/
-- edit/reassign as an event — the rehire story survives, and 'who
-- changed the roster' is answerable. Mirrors the coaching-audit stance
-- (qa_score_audit's CHECK enum is closed; own table, like cron_runs).
CREATE TABLE qa_roster_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    INTEGER NOT NULL,            -- soft ref (agents never delete)
    team_id     TEXT NOT NULL REFERENCES teams(id),
    action      TEXT NOT NULL CHECK (action IN
                    ('added','departed','rehired','edited','supervisor_changed')),
    detail      TEXT,                        -- JSON: {reason, note, from, to, fields}
    actor_email TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_roster_events_agent ON qa_roster_events (agent_id, created_at DESC);
```

Notes: `on_leave` is a departure *reason*, not a third state — an agent
on leave is `active=0` (drops out of Active Only and rosters) and comes
back via the same rehire flow. `active` stays the single behavioral
switch every consumer already respects; no consumer changes needed.

## 5. API — new `src/routes/roster.ts` (next session)

Gate: **`coach`** (admin | qa | team-scoped manager) — supervisors
manage their own teams; same capability the coaching surfaces use.
Actor = CF-Access email; every mutation writes a `qa_roster_events` row.

| Route | Method | Behavior |
|---|---|---|
| `/api/{t}/roster` | GET | full roster incl. departed: all columns + departure fields + last event + per-agent eval count (cheap COUNT) |
| `/api/{t}/roster` | POST | add `{name, email, canonical_name?, supervisor_email?, dialpad_agent_id?}` — email+name normalized; **rehire detection**: a departed row matching email OR lower(name) → 409 with the row + "rehire instead?" (the screen offers one-click rehire); high-range id; event `added` |
| `/api/{t}/roster/{id}` | PATCH | edit email / canonical / dialpad id / supervisor (supervisor change logs `supervisor_changed` with from→to) |
| `/api/{t}/roster/{id}/depart` | POST | `{reason, note?}` → `active=0` + stamps; refuses if already departed; event `departed` |
| `/api/{t}/roster/{id}/rehire` | POST | `{supervisor_email?}` → `active=1`, clears departure stamps (history lives in events); event `rehired` |
| `/api/{t}/roster/reassign` | POST | `{from_supervisor, to_supervisor}` — bulk move every active agent (the supervisor-left case); one event per agent |
| `/api/{t}/roster/supervisors` | GET | distinct active supervisor emails (picker source) |

Guards: departing an agent with an **open coaching session** or
in-flight scoring job → 200 with a `warnings` array (never a block —
the departure is a fact; surfaces let the coach cancel sessions).
Sofia's roster row is manageable like any other (its supervisor drives
the R4 sweep reviewer — changing it is a feature, not a hazard).

## 6. UI — the Agent Roster tab grows management (next session)

Placement: `team_dashboard.html` already has an **Agent Roster tab**
(analytics table). AA2 adds a **Manage roster** card above it, rendered
only for `whoami.can_coach` (invisible to everyone else, house pattern):

- **Roster table**: active agents first (name, email, supervisor,
  dialpad id, evals, since), then a collapsed **Departed** section
  (reason pill + date + note; Rehire button).
- **Add agent** row: name + email + canonical + supervisor picker
  (existing supervisors dropdown + free-email entry) + optional dialpad
  id. On 409-rehire → inline "X departed {date} ({reason}) — Rehire?".
- **Depart** flow: inline panel (house style, no modal) — reason select
  + note; shows the open-sessions warning when present.
- **Reassign supervisor** row: from-picker → to-picker + count preview.
- RBAC cross-link only (no auto-grant): "New supervisor? Grant them the
  `manager` role in /admin so they can coach." Roles stay explicit.

The existing roster *analytics* table is untouched; stats/charts pick
up changes automatically (historyFrame reads live rows per request).

## 7. LOC / surfaces to deprecate

| What | Where | When |
|---|---|---|
| `import_agents.py` (155 LOC) | qa-automation/AI-Scoring/scripts | AA3: header marked DEPRECATED (kept runnable as emergency fallback during residual shadow); DELETE at cutover |
| `sync_agents_upsert` DO UPDATE arm | sandy-qa/scripts/shadow_sync.py | AA0 flips to DO NOTHING; whole function deleted at cutover |
| "Data source: Google Sheets" footer | sandy-qa/pages/dashboard.html:551 | AA0 (one line: "Landing QA database") |
| Mails tab as a maintained artifact | each team's Google Sheet | AA3: freeze announcement to the team (edits there do nothing after AA) |
| Railway `/mails` sheet read + `MailsEntry` + scoring's Mails identity resolve | backend/routes/team.py:390, routes/scoring.py:481–506, models/team_stats.py:116 | Cutover kill list (Railway-side; untouched now — freeze doctrine) |
| Analyst_History projection writes + GAS row mode | backend/services/sheets_projection.py; qa-automation/src/Main.js row branch | Cutover kill list |

## 8. Ladder AA0–AA3 (all NEXT SESSION — no code exists yet)

- **AA0 — authority flip + relics** (small, ships first): shadow_sync
  upsert → `DO NOTHING` (comment: roster is Sandy-authoritative per
  AgentAddition §3); dashboard footer string. Gate: edit a D1 agent row
  manually, watch two sync cycles NOT revert it; new PG agent still
  arrives.
- **AA1 — schema + API**: migration 0010 (sqlite-validated 0001→0010,
  applied live) + `routes/roster.ts` + teamApi wiring + E2E (rehire
  detection, reassign, event log, gate, warnings).
- **AA2 — Manage roster card** on the Agent Roster tab (§6). Gate: add
  a real agent, depart with reason, rehire, reassign a supervisor —
  charts/filters/console dropdown reflect each within one reload; a
  viewer sees no management UI.
- **AA3 — retirement**: import_agents.py deprecation header; Mails-tab
  freeze note to the team; cutover kill list (§7 rows 5–6) appended to
  the cutover doc/sit-down agenda.

## 9. Open questions for Max (answer post-vacation; defaults chosen)

1. **Departure reasons**: `left_company | other_team | on_leave |
   terminated | other` (+ free note). Trim/extend?
2. **Gate**: `coach` (managers can manage their team) — or tighter
   (admin|qa only), with managers limited to *their own* agents'
   supervisor changes? Default: full `coach`, events make it auditable.
3. **Cross-team transfers** (`other_team` reason): v1 = depart here,
   add there (two steps, two teams' screens). A one-click transfer can
   come later if it's common.
4. **Sofia**: manageable like any row (default) or read-only in the UI?
5. Should `/team/mails` (console dropdown) also expose supervisor for
   auto-fill? It already does — no change needed; listed for awareness.

## 10. Rollout log + Mails-tab freeze announcement (AA3)

**Shipped 2026-08-20 (AA0–AA3, one session):** AA0 flip live + gated
(sentinel edit survived a full manual sync; footer relic gone). AA1
migration 0010 applied to live D1 + `routes/roster.ts` (37-check E2E).
AA2 Manage-roster card live on the Agent Roster tab (coach-gated). AA3
markers: `import_agents.py` DEPRECATED header, kill list appended to
`SandyMigration.md` Phase 6, announcement below. §9 defaults were built
as written — Max revisits post-vacation.

**Announcement text (paste to supervisors when ready):**

> **Roster management has moved to the QA dashboard.** The "Mails" tab
> in the team Google Sheet is now frozen — edits there no longer reach
> the dashboards. To add an agent, record a departure (with reason),
> rehire, or reassign supervisors, open your team dashboard → **Agent
> Roster** tab → **Manage roster** card
> (`qa-scoring.sandy.hellolanding.tech/dashboard/<team>`). The card is
> visible to coaches/managers only — ask Max if you need access. Every
> change is logged, and departures are always reversible (rehire keeps
> the agent's full history).

**Addendum (2026-08-20, v0.55–v0.56):** /admin can now grant the
`manager` role (coaching-only, team-scoped) — grant dropdown + team
select, request-approve offers it scoped to the request's team (qa
staff always had coaching: canCoach = privileged). Supervisors are now
ADDABLE, not just reassignable: migration 0011 `qa_supervisors`
registry (Sandy-only) unions into every picker; Manage-roster card
gains an Add-supervisor row + "No agents yet" line; re-adding a
deactivated label revives it; no auto-RBAC (manager stays an explicit
/admin grant).
