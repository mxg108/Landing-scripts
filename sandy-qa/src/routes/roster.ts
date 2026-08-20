// Roster management — AgentAddition §5 (AA1). The Manage-roster screen's
// API: add (with rehire detection), depart (soft, reversible — rehires are
// common; never a DELETE), rehire, edit, bulk supervisor reassign. Sandy is
// roster-AUTHORITATIVE from AA0 on (shadow_sync qa_agents arm is
// INSERT-only), so edits made here stick; the Google-Sheet Mails tab is
// frozen. Every mutation writes a qa_roster_events row (Sandy-only table —
// the rehire story survives, and "who changed the roster" is answerable).
//
// Gate: `coach` (admin | qa | team-scoped manager) — supervisors manage
// their own teams; same capability the coaching surfaces use (§9.2 default,
// events make it auditable). Actor = CF-Access email.

import { accessEmail, canCoach, resolveAccess } from "../lib/rbac.js";

export const DEPARTURE_REASONS = [
  "left_company",
  "other_team",
  "on_leave",
  "terminated",
  "other",
] as const;

const SANDY_BASE = 10_000_000;

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const now = () => new Date().toISOString();

async function gate(request: Request, db: D1Database, teamId: string, lookupAllow?: string) {
  const access = await resolveAccess(request, db, lookupAllow);
  if (!canCoach(access, teamId))
    return { deny: json({ detail: "Roster management is restricted to QA staff and team managers." }, 403) };
  const email = access.email || accessEmail(request);
  if (!email) return { deny: json({ deny: true, detail: "No SSO identity on this request." }, 401) };
  return { email };
}

async function teamExists(db: D1Database, teamId: string): Promise<boolean> {
  const row = await db.prepare("SELECT id FROM teams WHERE id = ?").bind(teamId).first<any>();
  return !!row;
}

async function logEvent(
  db: D1Database,
  agentId: number,
  teamId: string,
  action: "added" | "departed" | "rehired" | "edited" | "supervisor_changed",
  detail: Record<string, unknown> | null,
  actor: string
): Promise<void> {
  await db
    .prepare(
      "INSERT INTO qa_roster_events (agent_id, team_id, action, detail, actor_email) VALUES (?,?,?,?,?)"
    )
    .bind(agentId, teamId, action, detail ? JSON.stringify(detail) : null, actor)
    .run();
}

async function agentOn(db: D1Database, teamId: string, id: number): Promise<any | null> {
  return await db
    .prepare("SELECT * FROM qa_agents WHERE id = ? AND team_id = ?")
    .bind(id, teamId)
    .first<any>();
}

// The scoring queue's job-id slug (scoring.ts jobId construction) — lets
// the depart flow spot in-flight scoring for this agent without a schema
// change (qa_score_queue has no agent column; the slug is the linkage).
const agentSlug = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, "-");

// ── GET /api/{t}/roster ────────────────────────────────────────────────────
// Full roster INCLUDING departed rows: all columns + departure fields +
// last roster event + per-agent eval count. Active first, then name order.

export async function listRoster(
  request: Request,
  db: D1Database,
  teamId: string,
  lookupAllow?: string
): Promise<Response> {
  const g = await gate(request, db, teamId, lookupAllow);
  if ("deny" in g) return g.deny;
  if (!(await teamExists(db, teamId))) return json({ detail: `unknown team ${teamId}` }, 404);
  const rows = (
    await db
      .prepare(
        `SELECT a.*,
                (SELECT COUNT(*) FROM qa_evaluations e WHERE e.agent_id = a.id) AS eval_count,
                (SELECT json_object('action', ev.action, 'actor', ev.actor_email, 'at', ev.created_at)
                   FROM qa_roster_events ev WHERE ev.agent_id = a.id
                   ORDER BY ev.created_at DESC, ev.id DESC LIMIT 1) AS last_event
         FROM qa_agents a WHERE a.team_id = ?
         ORDER BY a.active DESC, LOWER(a.name)`
      )
      .bind(teamId)
      .all<any>()
  ).results.map((r) => ({ ...r, last_event: r.last_event ? JSON.parse(r.last_event) : null }));
  return json({
    team_id: teamId,
    active: rows.filter((r) => r.active === 1),
    departed: rows.filter((r) => r.active !== 1),
    departure_reasons: [...DEPARTURE_REASONS],
  });
}

// ── GET /api/{t}/roster/supervisors ────────────────────────────────────────

export async function listSupervisors(
  request: Request,
  db: D1Database,
  teamId: string,
  lookupAllow?: string
): Promise<Response> {
  const g = await gate(request, db, teamId, lookupAllow);
  if ("deny" in g) return g.deny;
  const rows = (
    await db
      .prepare(
        `SELECT DISTINCT supervisor_email AS supervisor FROM qa_agents
         WHERE team_id = ? AND active = 1
           AND supervisor_email IS NOT NULL AND TRIM(supervisor_email) <> ''
         ORDER BY LOWER(supervisor_email)`
      )
      .bind(teamId)
      .all<any>()
  ).results.map((r) => r.supervisor);
  return json({ supervisors: rows });
}

// ── POST /api/{t}/roster — add, with rehire detection ─────────────────────
// A departed row matching the email OR lower(name) → 409 carrying the row
// so the screen can offer one-click rehire instead of a duplicate. An
// ACTIVE match is a plain duplicate 409. New agents take high-range ids
// (>= SANDY_BASE, the house pattern) so PG-side inserts can never collide.

export async function addAgent(
  request: Request,
  db: D1Database,
  teamId: string,
  lookupAllow?: string
): Promise<Response> {
  const g = await gate(request, db, teamId, lookupAllow);
  if ("deny" in g) return g.deny;
  if (!(await teamExists(db, teamId))) return json({ detail: `unknown team ${teamId}` }, 404);
  let body: any = {};
  try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }

  const name = String(body.name ?? "").trim();
  const email = String(body.email ?? "").trim().toLowerCase();
  if (!name) return json({ detail: "name is required" }, 422);
  if (!email.includes("@")) return json({ detail: "a valid email is required" }, 422);
  const canonical = String(body.canonical_name ?? "").trim() || null;
  const supervisor = String(body.supervisor_email ?? "").trim() || null;
  const dialpadId = String(body.dialpad_agent_id ?? "").trim() || null;

  const existing = await db
    .prepare(
      `SELECT * FROM qa_agents WHERE team_id = ?
         AND (LOWER(name) = LOWER(?) OR LOWER(email) = ?)
       ORDER BY active DESC LIMIT 1`
    )
    .bind(teamId, name, email)
    .first<any>();
  if (existing) {
    if (existing.active === 1)
      return json(
        { detail: `${existing.name} <${existing.email}> is already on the ${teamId} roster.`, agent: existing },
        409
      );
    return json(
      {
        detail:
          `${existing.name} departed ${String(existing.departed_at ?? "").slice(0, 10) || "previously"}` +
          `${existing.departure_reason ? ` (${existing.departure_reason})` : ""} — rehire instead?`,
        rehire_available: true,
        agent: existing,
      },
      409
    );
  }

  // High-range id: MAX(id)+1, floored at SANDY_BASE.
  const maxRow = await db.prepare("SELECT MAX(id) AS mx FROM qa_agents").first<any>();
  const newId = Math.max(Number(maxRow?.mx ?? 0) + 1, SANDY_BASE);
  const ts = now();
  await db
    .prepare(
      `INSERT INTO qa_agents (id, team_id, name, canonical_name, email, supervisor_email,
                              dialpad_agent_id, active, created_at, updated_at)
       VALUES (?,?,?,?,?,?,?,1,?,?)`
    )
    .bind(newId, teamId, name, canonical, email, supervisor, dialpadId, ts, ts)
    .run();
  await logEvent(db, newId, teamId, "added", {
    name, email,
    ...(canonical ? { canonical_name: canonical } : {}),
    ...(supervisor ? { supervisor_email: supervisor } : {}),
    ...(dialpadId ? { dialpad_agent_id: dialpadId } : {}),
  }, g.email);
  const row = await agentOn(db, teamId, newId);
  return json({ ok: true, agent: row }, 201);
}

// ── PATCH /api/{t}/roster/{id} — edit email/canonical/dialpad/supervisor ──
// `name` is deliberately NOT editable: the unique (team, lower(name)) index
// and every eval/frame join key on it. A supervisor change logs its own
// `supervisor_changed` event with from→to; other fields log one `edited`.

const PATCHABLE = ["email", "canonical_name", "dialpad_agent_id", "supervisor_email"] as const;

export async function patchAgent(
  request: Request,
  db: D1Database,
  teamId: string,
  id: number,
  lookupAllow?: string
): Promise<Response> {
  const g = await gate(request, db, teamId, lookupAllow);
  if ("deny" in g) return g.deny;
  let body: any = {};
  try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }
  const agent = await agentOn(db, teamId, id);
  if (!agent) return json({ detail: `no agent ${id} on ${teamId}` }, 404);

  const changes: Record<string, { from: unknown; to: string | null }> = {};
  for (const f of PATCHABLE) {
    if (body[f] === undefined) continue;
    let v: string | null = String(body[f] ?? "").trim() || null;
    if (f === "email") {
      if (!v || !v.includes("@")) return json({ detail: "a valid email is required" }, 422);
      v = v.toLowerCase();
    }
    if ((agent[f] ?? null) !== v) changes[f] = { from: agent[f] ?? null, to: v };
  }
  if (!Object.keys(changes).length)
    return json({ ok: true, agent, changed: [], idempotent: true });

  const sets = Object.keys(changes).map((f) => `${f} = ?`).join(", ");
  await db
    .prepare(`UPDATE qa_agents SET ${sets}, updated_at = ? WHERE id = ?`)
    .bind(...Object.values(changes).map((c) => c.to), now(), id)
    .run();

  if (changes.supervisor_email)
    await logEvent(db, id, teamId, "supervisor_changed", {
      from: changes.supervisor_email.from,
      to: changes.supervisor_email.to,
    }, g.email);
  const other = Object.fromEntries(Object.entries(changes).filter(([f]) => f !== "supervisor_email"));
  if (Object.keys(other).length)
    await logEvent(db, id, teamId, "edited", { fields: other }, g.email);

  const row = await agentOn(db, teamId, id);
  return json({ ok: true, agent: row, changed: Object.keys(changes) });
}

// ── POST /api/{t}/roster/{id}/depart ───────────────────────────────────────
// Soft: active=0 + reason/date/note stamps. Open coaching sessions or
// in-flight scoring jobs come back as `warnings` — never a block (the
// departure is a fact; surfaces let the coach cancel sessions).

export async function departAgent(
  request: Request,
  db: D1Database,
  teamId: string,
  id: number,
  lookupAllow?: string
): Promise<Response> {
  const g = await gate(request, db, teamId, lookupAllow);
  if ("deny" in g) return g.deny;
  let body: any = {};
  try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }
  const reason = String(body.reason ?? "");
  if (!(DEPARTURE_REASONS as readonly string[]).includes(reason))
    return json({ detail: `reason must be one of ${DEPARTURE_REASONS.join("|")}` }, 422);
  const note = String(body.note ?? "").trim() || null;

  const agent = await agentOn(db, teamId, id);
  if (!agent) return json({ detail: `no agent ${id} on ${teamId}` }, 404);
  if (agent.active !== 1)
    return json(
      { detail: `${agent.name} already departed ${String(agent.departed_at ?? "").slice(0, 10)}`.trim(), agent },
      409
    );

  const warnings: string[] = [];
  const openSessions = await db
    .prepare(
      `SELECT COUNT(*) AS n FROM qa_coachings
       WHERE agent_id = ? AND id >= ?
         AND (status = 'pending' OR (status = 'completed' AND outcome IS NULL))`
    )
    .bind(id, SANDY_BASE)
    .first<any>();
  if (Number(openSessions?.n ?? 0) > 0)
    warnings.push(
      `${openSessions.n} open coaching session(s) — cancel or confirm them from the coaching page.`
    );
  const inFlight = await db
    .prepare(
      `SELECT COUNT(*) AS n FROM qa_score_queue
       WHERE team_id = ? AND status IN ('queued','triggering','running')
         AND job_id LIKE ?`
    )
    .bind(teamId, `%-${agentSlug(agent.name)}`)
    .first<any>();
  if (Number(inFlight?.n ?? 0) > 0)
    warnings.push(`${inFlight.n} in-flight scoring job(s) — they will finish and persist normally.`);

  const ts = now();
  await db
    .prepare(
      `UPDATE qa_agents SET active = 0, departure_reason = ?, departed_at = ?,
              departure_note = ?, updated_at = ? WHERE id = ?`
    )
    .bind(reason, ts, note, ts, id)
    .run();
  await logEvent(db, id, teamId, "departed", { reason, ...(note ? { note } : {}) }, g.email);
  const row = await agentOn(db, teamId, id);
  return json({ ok: true, agent: row, warnings });
}

// ── POST /api/{t}/roster/{id}/rehire ───────────────────────────────────────
// active=1, departure stamps cleared (the story lives in qa_roster_events),
// optional fresh supervisor.

export async function rehireAgent(
  request: Request,
  db: D1Database,
  teamId: string,
  id: number,
  lookupAllow?: string
): Promise<Response> {
  const g = await gate(request, db, teamId, lookupAllow);
  if ("deny" in g) return g.deny;
  let body: any = {};
  try { body = await request.json(); } catch {}
  const agent = await agentOn(db, teamId, id);
  if (!agent) return json({ detail: `no agent ${id} on ${teamId}` }, 404);
  if (agent.active === 1)
    return json({ detail: `${agent.name} is already active on ${teamId}`, agent }, 409);

  const supervisor = String(body?.supervisor_email ?? "").trim() || agent.supervisor_email || null;
  await db
    .prepare(
      `UPDATE qa_agents SET active = 1, departure_reason = NULL, departed_at = NULL,
              departure_note = NULL, supervisor_email = ?, updated_at = ? WHERE id = ?`
    )
    .bind(supervisor, now(), id)
    .run();
  await logEvent(db, id, teamId, "rehired", {
    ...(supervisor ? { supervisor_email: supervisor } : {}),
    previous: { reason: agent.departure_reason, departed_at: agent.departed_at },
  }, g.email);
  const row = await agentOn(db, teamId, id);
  return json({ ok: true, agent: row });
}

// ── POST /api/{t}/roster/reassign — bulk supervisor move ──────────────────
// The supervisor-left case: every ACTIVE agent under from_supervisor moves
// to to_supervisor, one event per agent. Values are matched exactly as
// stored (case-insensitive) — supervisor_email holds names on legacy rows.

export async function reassignSupervisor(
  request: Request,
  db: D1Database,
  teamId: string,
  lookupAllow?: string
): Promise<Response> {
  const g = await gate(request, db, teamId, lookupAllow);
  if ("deny" in g) return g.deny;
  let body: any = {};
  try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }
  const from = String(body.from_supervisor ?? "").trim();
  const to = String(body.to_supervisor ?? "").trim();
  if (!from || !to) return json({ detail: "from_supervisor and to_supervisor are required" }, 422);
  if (from.toLowerCase() === to.toLowerCase())
    return json({ detail: "from and to are the same supervisor" }, 422);

  const agents = (
    await db
      .prepare(
        `SELECT id, name FROM qa_agents
         WHERE team_id = ? AND active = 1 AND LOWER(COALESCE(supervisor_email,'')) = LOWER(?)`
      )
      .bind(teamId, from)
      .all<any>()
  ).results;
  if (!agents.length)
    return json({ detail: `no active agents on ${teamId} under '${from}'` }, 404);

  const ts = now();
  for (const a of agents) {
    await db
      .prepare("UPDATE qa_agents SET supervisor_email = ?, updated_at = ? WHERE id = ?")
      .bind(to, ts, a.id)
      .run();
    await logEvent(db, a.id, teamId, "supervisor_changed", { from, to, via: "bulk_reassign" }, g.email);
  }
  return json({ ok: true, moved: agents.length, agents: agents.map((a) => a.name) });
}
