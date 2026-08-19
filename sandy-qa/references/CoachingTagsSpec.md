# Coaching v2 — typed tags & supertags + polish wrinkles (spec)

*Design doc, 2026-08-18. Extends CoachingLoopSpec.md (ladder CL0–CL5
complete, v0.47 live). Doc-first per working agreement; code follows
sign-off. Ladder prefix **T** (tags), with **T0** covering the two
post-ladder wrinkles.*

***SIGNED OFF 2026-08-19*** *— owner answers: (1) seed list = the owner-
refined §2 table (`check_in_procedures` — spelling normalized from the
table's `chek_in_procedures`; `dialpad`, `mission_control`, `no_fcr` are
the operation's real vocabulary); (2) any `coach` may create tags; (3)
retro-tag the existing Sandy-born confirmed sessions via the session
screen; (4) cross-team vocabulary — **team scoping happens only at
RAG-time** (tags are a global language; retrieval/insight queries scope by
team when they run); (5) ≥1-tag-to-confirm stays OFF in v1 (default kept).*

## 0. Goal

Turn confirmed coaching outcomes into **training-program fuel**: a
controlled, typed tag vocabulary over coaching sessions, so the insights
layer can answer "what should we RETRAIN across the floor" by theme —
not just by rubric section. Plus two polish wrinkles from live use:
header consistency and Railway-era deprecation.

## 1. T0 — wrinkles from live use

### 1.1 Header consistency on /coaching

`pages/coaching.html` wears a bespoke navy header; every other surface
uses the shared **QAHeader** (`/static/header.js` + `header.css`: nav
buttons Batch Scoring / Team View / Lookup + title + window chiclets).
Fix: `header.js` gains a `page: 'plain'` mode — nav buttons + title,
**no time-window controls** (the coaching page has no window state; a
no-op `onChange`). The coaching page adopts it (`title: 'Coaching'`,
plus a Coaching nav button added to the shared nav for coach-capable
users via `whoami.can_coach` — one fetch, cached; non-coaches never see
the button). Dashboard/team pages pick the new button up from the same
shared header.js — one edit, every page consistent.

### 1.2 Railway-era deprecation (coachings + review queue)

Owner call: the shadow period has proven Sandy; Railway-born coachings
and Railway-born human-review-queue rows stop rendering — **fresh start
on Sandy**. Mechanics matter:

- **Read-path filtering, not deletion.** The shadow sync re-imports the
  Railway range every 30 minutes — a delete would resurrect within the
  half hour. Filter `id >= SANDY_ID_BASE` at every coaching read
  (`listAgentCoachings`, `listTeamCoachings`, `teamFacts` — this also
  removes the "6 overdue confirmations" noise, which was Railway
  receipts the Sandy queue can't act on anyway) and in `reviewQueue`
  (origin pills retire with the rows).
- **Hard delete lands at cutover**, when the sync itself retires — a
  one-time cleanup alongside the rest of the Railway decommission.
- Historical Railway-born EVALS are untouched — they are the scoring
  history the charts stand on. Deprecation covers coaching rows and the
  review-queue *surface* only.
- ⚠ Flag for the owner: this decision is the first formal "Sandy wins"
  call — worth scheduling the cutover sit-down while conviction is
  fresh (shadow shortening was already the agreed lean).

### 1.3 Incident 2026-08-19 — stacked sync processes shredded the mirror

Owner report: July showed 102 evals on the dashboard vs ~500 real. D1
held 1,279 evals (Railway range: exactly ids 1–1178, contiguous, **zero**
section rows; 44 agents) — a wipe+reimport cut off mid-stream. Railway
Postgres was intact (2,597 evals, **515 July finalized**, 43 agents).

Root cause: `sandy.py`'s `urlopen` had **no socket timeout**; one sync
hung on a stalled connection for **25 hours**, another for 6.5h, and cron
kept launching new runs every 30 min on top — concurrent wipe/reimport
cycles over the same tables. Fixes (all this session): zombies killed;
`shadow_sync.sh` gained single-flight (pid lock; overlapping tick logs
SKIP) + a 20-min hard kill; `shadow_sync.py` sets `socket.setdefaulttimeout
(120)`; `sandy.py` `urlopen(..., timeout=300)` (house CLI — affects every
Sandy call). Recovery = one clean sync run (~5 min). No Railway data was
ever at risk; the mirror self-heals on the next full sync by design.
**This is the strongest argument yet for retiring the pull sync at
cutover** (§1.2's "Sandy wins" note).

## 2. The taxonomy — 4 supertags, typed tags (owner design)

Four **orthogonal supertags** (analogous to states/superstates), fixed
at birth. Every tag MUST carry exactly one:

| Supertag | Axis | Seed examples |
|---|---|---|
| `sop` | knowledge of procedure | `cancellation_policy`, `locked_out`, `chek_in_procedures` |
| `system_skills` | tooling & admin | `admin`, `dialpad`, `mission_control`|
| `soft_skills` | interpersonal | `rudeness`, `filler_words`, `tone_matching` |
| `effectiveness` | outcomes & efficiency | `long_hold`, `no_fcr`, `unresolved_callback` |

Design judgments (the honest-thoughts section, §7, argues these):

- **Type is immutable** after creation. A CHECK enum enforces the four;
  adding a fifth axis is deliberately a migration, not a UI action.
- **Names are globally unique** and normalized (`snake_case`, lowercase,
  trimmed) — not unique-per-type. Two `long_hold`s with different types
  would poison every aggregate that names tags in prose.
- **Deprecation is soft and graceful**: historical sessions keep the
  tag; pickers stop offering it; analytics label it `(deprecated)`.
  `replaced_by_tag_id` supports the most common taxonomy operation a
  year in — merge/rename — without rewriting history.
- **Session-level tags only in v1.** Commitments already carry
  `section_id`; a second tagging level doubles the UX cost for little
  analytical gain. Revisit only if tag reports prove too coarse.

### Migration `0008_coaching_tags.sql` (Sandy-only tables — never in the
sync's WIPE/RESYNC lists; deliberately NOT the Railway-parity `qa_tags`,
which is eval-scoped, sync-owned, and typeless)

```sql
CREATE TABLE qa_coach_tags (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    type               TEXT NOT NULL CHECK (type IN
                           ('sop','system_skills','soft_skills','effectiveness')),
    name               TEXT NOT NULL UNIQUE
                       CHECK (name = lower(trim(name)) AND length(name) > 0),
    description        TEXT,
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','deprecated')),
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    deprecated_by      TEXT,
    deprecated_at      TEXT,
    deprecation_note   TEXT,
    replaced_by_tag_id INTEGER REFERENCES qa_coach_tags(id),
    CHECK (status = 'active' OR (deprecated_by IS NOT NULL AND deprecated_at IS NOT NULL))
);
CREATE INDEX idx_coach_tags_active ON qa_coach_tags (type, name) WHERE status = 'active';

CREATE TABLE qa_coaching_tag_links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    coaching_id  INTEGER NOT NULL REFERENCES qa_coachings(id) ON DELETE CASCADE,
    tag_id       INTEGER NOT NULL REFERENCES qa_coach_tags(id),
    linked_by    TEXT NOT NULL,
    linked_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT uq_coaching_tag UNIQUE (coaching_id, tag_id)
);
CREATE INDEX idx_coaching_tag_links_tag ON qa_coaching_tag_links (tag_id);

-- Seeds (created_by 'seed') — the owner-refined §2 table, signed off:
--   sop:            cancellation_policy, locked_out, check_in_procedures
--   system_skills:  admin, dialpad, mission_control
--   soft_skills:    rudeness, filler_words, tone_matching
--   effectiveness:  long_hold, no_fcr, unresolved_callback
```

## 3. API — `routes/coaching.ts` additions (all gated `coach`)

| Route | Method | Behavior |
|---|---|---|
| `/api/{t}/coach-tags` | GET | active tags grouped by type (+ `?include=deprecated` for admin/history views) |
| `/api/{t}/coach-tags` | POST | `{name, type, description?}` — normalize name; 409 on duplicate (including deprecated: surface "deprecated — restore or pick a new name"); `created_by` = Access email |
| `/api/{t}/coach-tags/{id}/deprecate` | POST | `{note?, replaced_by_tag_id?}` — soft; idempotent |
| `/api/{t}/coach-tags/{id}/restore` | POST | back to active (the graceful inverse) |
| `/api/{t}/coachings/{id}/tags` | PUT | full-replace tag set on a session — allowed while `pending`/`completed`-unconfirmed; **409 after outcome confirm** (tags freeze with the record); deprecated tags accepted only if already linked |

`conduct` and `confirm` accept an optional `tag_ids: []` alongside their
existing bodies (one round-trip from the session screen). Tags are NOT
required to confirm — enforcement stays social in v1 (§8.4).

Tag tables are global vocabulary (not per-team) — the `{t}` in the URL
is routing convention; a `cancellation_policy` retraining insight should
aggregate across teams later.

## 4. The session screen — `/coaching/{team}/session/{id}`

Replaces the `prompt()` conduct flow (dashboard card) and gives confirm
a real home (the queue's inline panel becomes a link). One page, three
states, gated `coach`:

- **Pending** → conduct form: summary textarea, attitude select,
  deadline (if missing), commitments read-only list, **tag picker**.
- **Conducted, unconfirmed** → facts panel (the §11.4 windows), per-
  commitment verdict rows + notes, outcome note, tag picker (pre-loaded
  with conduct-time tags, editable), Confirm button.
- **Confirmed** → read-only record: summary, verdicts, outcome, tags as
  chips grouped by supertag.

**Tag picker** (shared component on the page): four supertag columns,
active tags as toggle chips, inline "+ new tag" per column (name field →
POST, auto-selects on success; 409 shows the duplicate/deprecated
message). Deprecated tags never offered; existing links to deprecated
tags render with a `(deprecated)` chip style.

Entry points: dashboard coaching-card "Mark conducted" → this screen;
queue "Confirm" → this screen; All-sessions "Detail" → this screen
(read-only for confirmed). Tag ADMIN (deprecate/restore, browse full
vocabulary) = a "Manage tags" card on `/coaching/{team}` — not `/admin`
(tags are a coaching-program object, owned by coaches).

## 5. Insights integration — the training-program fuel

- `teamFacts` gains `tags`: per supertag → per tag: sessions, agents
  touched, commitment met-rate on those sessions, avg overall delta
  after coaching (the §11.4 windows). Deprecated tags aggregate under
  their `replaced_by` target when set.
- `buildTeamPrompts` gains the training-program charge: "from the tag
  aggregates, recommend which THEMES warrant a floor-wide training or
  retraining program vs individual re-coaching — a theme coached across
  many agents with a low met-rate or negative delta is a program
  candidate; a theme confined to one agent is not."
- The EOM/progression prompts include the agent's session tags so
  individual assessments name their themes with the same vocabulary.
- Later (out of scope here): a per-tag drill view and Aria/PID
  calibration hooks — the tag vocabulary is deliberately the shared
  language for those.

## 6. Rollout ladder

- **T0 — wrinkles**: header `page:'plain'` + Coaching nav button;
  Railway-born filtered from coaching reads, teamFacts, review queue.
  Gate: /coaching wears the standard header and navigates everywhere;
  queue + cards show Sandy-born only; team insight regenerated without
  the Railway-overdue noise.
- **T1 — vocabulary**: migration 0008 + seeds; tag CRUD/deprecate/
  restore API; session `tags` PUT. Gate: curl lifecycle — create,
  duplicate-409, link, freeze-after-confirm 409, deprecate, restore.
- **T2 — session screen**: `/coaching/{team}/session/{id}` with the
  three states + tag picker; dashboard card and queue link to it;
  `prompt()` flow deleted. Gate: conduct + confirm a real session
  end-to-end on the screen, tags attached at both stages, chiclet still
  fires, frozen record renders chips.
- **T3 — insights**: teamFacts tag aggregates + training-program prompt
  + progression prompt tags. Gate: regenerate team insight — the
  narrative names tag themes with figures and distinguishes
  program-candidates from individual re-coaching.

## 7. Honest thoughts (asked for, recorded)

1. **The typed constraint is the strongest part of the design.** An
   untyped folksonomy would rot within a quarter (`sop`, `sops`,
   `cancelation` as three tags); forcing every tag through one of four
   orthogonal axes keeps every future aggregate meaningful. The four
   axes chosen are genuinely orthogonal (knowledge / tooling /
   interpersonal / outcomes).
2. **Attach tags at conduct, not only confirm.** The coach fresh out of
   the 1:1 knows the themes best; the supervisor at confirm may not
   have been in the room. Spec'd: editable at both stages, frozen at
   confirm. (Deviation from the letter of the ask — "added upon
   confirmation" — kept as the freeze point instead.)
3. **Supertags as a fixed enum, not a table.** They are analytical
   axes, not vocabulary; making them data invites a fifth axis by
   accident. A migration is the right amount of friction.
4. **Deprecation with `replaced_by`** is worth the one extra column
   now: merge/rename is the #1 real-world taxonomy operation, and the
   pointer lets analytics follow renames without rewriting links.
5. **Railway deprecation must be read-path now, delete-at-cutover** —
   the sync would resurrect deletes within 30 minutes. And the call
   itself is the "Sandy wins" moment: schedule the cutover sit-down.
6. Session-level tags only, global (cross-team) vocabulary, tags
   optional at confirm in v1 — all argued inline above (§2, §3).

## 8. Open questions (owner/Max) — defaults chosen

1. **Seed list**: the §2 table's ~12 tags as `seed`-created? (Default
   yes — pickers should open non-empty; trim/extend at sign-off.)
2. **Who creates tags**: any `coach` (default) vs admin-only. Default
   coach — the vocabulary must grow at floor speed; deprecation is the
   cleanup valve.
3. **Backfill**: tag the existing Sandy-born confirmed sessions
   retroactively? Default yes for the handful that exist (manual, via
   the session screen — they're the training-insight seed corpus).
4. **Require ≥1 tag to confirm?** Default no in v1 (social norm first);
   flip to required once the vocabulary stabilizes — one 422 away.
5. **Cross-team vocabulary** (default) vs per-team tag lists.
