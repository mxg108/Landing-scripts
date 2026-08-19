// Coaching loop — CoachingLoopSpec §4 (CL1). Session lifecycle over the
// 0001 qa_coachings model (pending → completed → confirmed-outcome), the
// commitments table (0007), the deadline-driven confirmation queue, and the
// §6.5 coaching_confirmed T2 chiclet (the first Sandy-side chiclet of the
// LandingOpsCommandCenter design).
//
// Doctrine carried from scoring.ts: Sandy-born rows only (high id range —
// Railway-born coachings render read-only and 409 every mutation); actor
// identity = CF-Access email; queues are predicates, not tables.

import { accessEmail, canCoach, resolveAccess, selfAgentFor } from "../lib/rbac.js";
import { extractEvalId } from "../lib/records.js";

const SANDY_BASE = 10_000_000;

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const now = () => new Date().toISOString();

// LA calendar day — the queue's "deadline has passed" clock (BUCKET_TZ,
// matching the stats engine's bucketing).
function todayLA(): string {
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone: "America/Los_Angeles",
  }).format(new Date());
}

async function nextSandyId(db: D1Database, table: string): Promise<number> {
  const row = await db
    .prepare(`SELECT COALESCE(MAX(id) + 1, ?) AS v FROM ${table} WHERE id >= ?`)
    .bind(SANDY_BASE, SANDY_BASE)
    .first<any>();
  return row?.v ?? SANDY_BASE;
}

async function rosterAgent(db: D1Database, teamId: string, agentName: string) {
  const a = agentName.trim().toLowerCase();
  return db
    .prepare(
      `SELECT id, name, canonical_name, email FROM qa_agents
       WHERE team_id = ? AND active = 1
         AND (LOWER(name) = ? OR LOWER(canonical_name) = ?) LIMIT 1`
    )
    .bind(teamId, a, a)
    .first<any>();
}

const ROLE_ENUM = new Set(["team_lead", "manager", "hr", "external"]);
const ATTITUDE_ENUM = new Set([
  "receptive", "engaged", "neutral", "defensive", "dismissive", "mixed",
]);
const VERDICT_ENUM = new Set(["met", "partially_met", "not_met", "waived"]);

async function coachingById(db: D1Database, teamId: string, id: number) {
  return db
    .prepare("SELECT * FROM qa_coachings WHERE id = ? AND team_id = ?")
    .bind(id, teamId)
    .first<any>();
}

const railwayBorn409 = () =>
  json(
    { detail: "This coaching is Railway-born — read-only on Sandy (act on Railway during the shadow period)." },
    409
  );

// Resolve builder call refs to persisted evaluations. The records payload
// never exposes numeric eval ids (parity-frozen shape), so refs are the
// linking currency — but a record's `eval_id` is the stored LINK's trailing
// segment (Sheets-era truth), which on legacy rows can be the MASTER call
// id or arbitrary link junk rather than call/entry-point ids (first real
// builder attempt failed on exactly this). Resolution therefore mirrors
// the datapoint page: exact id columns first (call | entry-point | master),
// then the link-derived fallback (LIKE-prefiltered, verified with the same
// extractEvalId the records read path uses).
const EVAL_REF_COLS = `id, agent_id, agent_name_raw, opportunities, overall_score,
                dialpad_call_id, dialpad_entry_point_call_id, call_connected_at`;

async function resolveEvalRefs(
  db: D1Database,
  teamId: string,
  refs: string[]
): Promise<{ resolved: any[]; missing: string[] }> {
  const resolved: any[] = [];
  const missing: string[] = [];
  for (const ref of refs) {
    const r = String(ref).trim();
    if (!r) continue;
    let ev = await db
      .prepare(
        `SELECT ${EVAL_REF_COLS}
         FROM qa_evaluations
         WHERE team_id = ? AND (dialpad_call_id = ? OR dialpad_entry_point_call_id = ?
               OR dialpad_master_call_id = ?)
         ORDER BY id DESC LIMIT 1`
      )
      .bind(teamId, r, r, r)
      .first<any>();
    if (!ev) {
      const candidates = await db
        .prepare(
          `SELECT ${EVAL_REF_COLS}, dialpad_link
           FROM qa_evaluations
           WHERE team_id = ? AND dialpad_link LIKE ? ORDER BY id DESC LIMIT 25`
        )
        .bind(teamId, `%${r}%`)
        .all<any>();
      ev = candidates.results.find((c: any) => extractEvalId(c.dialpad_link ?? "") === r) ?? null;
    }
    if (ev) resolved.push({ ...ev, ref: r });
    else missing.push(r);
  }
  return { resolved, missing };
}

// Single redaction seam (§4.5): the agent sees their commitments and linked
// calls, never the supervisor-side judgment fields. New fields default to
// the redacted path by being added HERE, not at call sites.
function redactForSelf(session: any): any {
  const { agent_attitude, outcome_note, ...rest } = session;
  return {
    ...rest,
    agent_attitude: null,
    outcome_note: null,
    commitments: (session.commitments ?? []).map((c: any) => ({
      ...c,
      confirmation_note: null,
    })),
    evaluations: (session.evaluations ?? []).map((e: any) => ({
      ...e,
      per_eval_note: null,
    })),
  };
}

async function attachChildren(db: D1Database, sessions: any[]): Promise<void> {
  if (!sessions.length) return;
  const ids = sessions.map((s) => s.id);
  const byId = new Map(sessions.map((s) => [s.id, s]));
  for (const s of sessions) {
    s.commitments = [];
    s.evaluations = [];
  }
  for (let i = 0; i < ids.length; i += 80) {
    const chunk = ids.slice(i, i + 80);
    const ph = chunk.map(() => "?").join(",");
    const commits = await db
      .prepare(
        `SELECT id, coaching_id, commitment, section_id, status, confirmed_by,
                confirmed_at, confirmation_note, created_at
         FROM qa_coaching_commitments WHERE coaching_id IN (${ph}) ORDER BY id`
      )
      .bind(...chunk)
      .all<any>();
    for (const c of commits.results) byId.get(c.coaching_id)?.commitments.push(c);
    // LEFT JOIN: a Railway-side delete leaves the snapshot columns (the
    // junction's evaluation_id is a soft ref since 0007).
    const links = await db
      .prepare(
        `SELECT j.coaching_id, j.evaluation_id, j.opportunities_snapshot,
                j.per_eval_note, j.linked_at,
                e.dialpad_call_id, e.dialpad_entry_point_call_id,
                e.call_connected_at, e.overall_score, e.agent_name_raw
         FROM qa_coaching_evaluations j
         LEFT JOIN qa_evaluations e ON e.id = j.evaluation_id
         WHERE j.coaching_id IN (${ph}) ORDER BY j.id`
      )
      .bind(...chunk)
      .all<any>();
    for (const l of links.results) {
      byId.get(l.coaching_id)?.evaluations.push({
        evaluation_id: l.evaluation_id,
        ref: l.dialpad_entry_point_call_id || l.dialpad_call_id || null,
        call_connected_at: l.call_connected_at ?? null,
        overall_score: l.overall_score ?? null,
        opportunities_snapshot: l.opportunities_snapshot,
        per_eval_note: l.per_eval_note,
        linked_at: l.linked_at,
        eval_missing: l.dialpad_call_id === null && l.dialpad_entry_point_call_id === null,
      });
    }
  }
}

// ── GET /api/{t}/agents/{n}/coachings ──────────────────────────────────────

export async function listAgentCoachings(
  request: Request,
  db: D1Database,
  teamId: string,
  agentName: string,
  url: URL,
  lookupAllow?: string
): Promise<Response> {
  const access = await resolveAccess(request, db, lookupAllow);
  const coach = canCoach(access, teamId);
  const agent = await rosterAgent(db, teamId, agentName);
  if (!agent)
    return json({ detail: `'${agentName}' is not on the ${teamId} roster` }, 404);
  const self = coach ? null : await selfAgentFor(request, db, teamId);
  const selfView = !coach && self !== null && self.id === agent.id;
  if (!coach && !selfView)
    return json({ detail: "Coaching records are restricted." }, 403);

  const days = Math.min(730, Math.max(1, parseInt(url.searchParams.get("days") ?? "90", 10) || 90));
  const cutoff = new Date(Date.now() - days * 86_400_000).toISOString();
  // Pending/unconfirmed sessions always show; closed history windows out.
  // Sandy-born only (CoachingTagsSpec §1.2 — Railway-era coachings are
  // deprecated read-side; the shadow sync would resurrect a delete, so
  // the hard delete waits for cutover).
  const rows = await db
    .prepare(
      `SELECT * FROM qa_coachings
       WHERE team_id = ? AND agent_id = ? AND id >= ?
         AND (status = 'pending' OR outcome IS NULL
              OR COALESCE(completed_at, created_at) >= ?)
       ORDER BY created_at DESC`
    )
    .bind(teamId, agent.id, SANDY_BASE, cutoff)
    .all<any>();
  const sessions = rows.results.map((s) => ({ ...s, sandy_born: s.id >= SANDY_BASE }));
  await attachChildren(db, sessions);
  return json({
    agent: agent.canonical_name || agent.name,
    can_coach: coach,
    self_view: selfView,
    sessions: selfView ? sessions.map(redactForSelf) : sessions,
  });
}

// ── POST /api/{t}/coachings (builder create) ───────────────────────────────

export async function createCoaching(
  request: Request,
  db: D1Database,
  teamId: string,
  lookupAllow?: string
): Promise<Response> {
  const access = await resolveAccess(request, db, lookupAllow);
  if (!canCoach(access, teamId))
    return json({ detail: "Creating coaching sessions is restricted to QA staff and team managers." }, 403);
  let body: any = {};
  try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }

  const agent = await rosterAgent(db, teamId, (body.agent_name ?? "").toString());
  if (!agent)
    return json({ detail: `agent '${body.agent_name ?? ""}' not on the ${teamId} roster` }, 422);
  const deadline = (body.deadline ?? "").toString().trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(deadline))
    return json({ detail: "deadline (YYYY-MM-DD) is required" }, 422);
  const commitments = Array.isArray(body.commitments) ? body.commitments : [];
  const texts = commitments
    .map((c: any) => ({
      text: (c.text ?? c.commitment ?? "").toString().trim(),
      section_id: c.section_id ? String(c.section_id) : null,
    }))
    .filter((c: any) => c.text);
  if (!texts.length)
    return json({ detail: "at least one commitment is required" }, 422);
  const role = ROLE_ENUM.has(body.conducted_by_role) ? body.conducted_by_role : "manager";

  const refs = Array.isArray(body.eval_refs) ? body.eval_refs.map(String) : [];
  const { resolved, missing } = await resolveEvalRefs(db, teamId, refs);
  if (missing.length)
    return json({ detail: `no evaluation matches call id(s): ${missing.join(", ")}` }, 422);
  const foreign = resolved.filter(
    (e) => e.agent_id !== agent.id &&
      e.agent_name_raw.trim().toLowerCase() !== agent.name.trim().toLowerCase()
  );
  if (foreign.length)
    return json(
      { detail: `call(s) belong to a different agent: ${foreign.map((e) => e.ref).join(", ")}` },
      422
    );

  const cid = await nextSandyId(db, "qa_coachings");
  await db
    .prepare(
      `INSERT INTO qa_coachings (id, agent_id, team_id, conducted_by_role,
        conducted_by_email, status, action_plan, action_plan_deadline, scheduled_at)
       VALUES (?,?,?,?,?, 'pending', ?, ?, ?)`
    )
    .bind(
      cid, agent.id, teamId, role,
      access.email || accessEmail(request) || null,
      (body.action_plan ?? "").toString().trim() || null,
      deadline,
      (body.scheduled_at ?? "").toString().trim() || null
    )
    .run();
  const notes = body.per_eval_notes ?? {};
  let jid = await nextSandyId(db, "qa_coaching_evaluations");
  for (const ev of resolved) {
    await db
      .prepare(
        `INSERT INTO qa_coaching_evaluations (id, coaching_id, evaluation_id,
          opportunities_snapshot, per_eval_note) VALUES (?,?,?,?,?)`
      )
      .bind(
        jid++, cid, ev.id, ev.opportunities ?? null,
        (notes[ev.ref] ?? notes[String(ev.id)] ?? "").toString().trim() || null
      )
      .run();
  }
  for (const c of texts) {
    await db
      .prepare(
        "INSERT INTO qa_coaching_commitments (coaching_id, commitment, section_id) VALUES (?,?,?)"
      )
      .bind(cid, c.text, c.section_id)
      .run();
  }
  return json({
    ok: true, coaching_id: cid, status: "pending",
    linked_evaluations: resolved.length, commitments: texts.length,
  });
}

// ── PATCH /api/{t}/coachings/{id} (pending-only edits / receipt upgrade) ───

export async function patchCoaching(
  request: Request,
  db: D1Database,
  teamId: string,
  id: number,
  lookupAllow?: string
): Promise<Response> {
  const access = await resolveAccess(request, db, lookupAllow);
  if (!canCoach(access, teamId))
    return json({ detail: "Editing coaching sessions is restricted to QA staff and team managers." }, 403);
  const c = await coachingById(db, teamId, id);
  if (!c) return json({ detail: `no coaching ${id} on ${teamId}` }, 404);
  if (c.id < SANDY_BASE) return railwayBorn409();
  if (c.status !== "pending")
    return json({ detail: `coaching is '${c.status}' — only pending sessions are editable` }, 409);
  let body: any = {};
  try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }

  const sets: string[] = [];
  const vals: any[] = [];
  if (body.deadline !== undefined) {
    const d = (body.deadline ?? "").toString().trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) return json({ detail: "deadline must be YYYY-MM-DD" }, 422);
    sets.push("action_plan_deadline = ?"); vals.push(d);
  }
  if (body.scheduled_at !== undefined) {
    sets.push("scheduled_at = ?"); vals.push((body.scheduled_at ?? "").toString().trim() || null);
  }
  if (body.action_plan !== undefined) {
    sets.push("action_plan = ?"); vals.push((body.action_plan ?? "").toString().trim() || null);
  }
  if (body.conducted_by_role !== undefined) {
    if (!ROLE_ENUM.has(body.conducted_by_role))
      return json({ detail: "conducted_by_role must be team_lead|manager|hr|external" }, 422);
    sets.push("conducted_by_role = ?"); vals.push(body.conducted_by_role);
  }
  if (sets.length) {
    await db.prepare(`UPDATE qa_coachings SET ${sets.join(", ")} WHERE id = ?`).bind(...vals, id).run();
  }

  // Full-replace semantics for children (pre-conduct, every commitment is
  // still 'open' — nothing confirmed can be lost here).
  let linked: number | undefined;
  if (Array.isArray(body.eval_refs)) {
    const { resolved, missing } = await resolveEvalRefs(db, teamId, body.eval_refs.map(String));
    if (missing.length)
      return json({ detail: `no evaluation matches call id(s): ${missing.join(", ")}` }, 422);
    await db.prepare("DELETE FROM qa_coaching_evaluations WHERE coaching_id = ?").bind(id).run();
    const notes = body.per_eval_notes ?? {};
    let jid = await nextSandyId(db, "qa_coaching_evaluations");
    for (const ev of resolved) {
      await db
        .prepare(
          `INSERT INTO qa_coaching_evaluations (id, coaching_id, evaluation_id,
            opportunities_snapshot, per_eval_note) VALUES (?,?,?,?,?)`
        )
        .bind(jid++, id, ev.id, ev.opportunities ?? null,
          (notes[ev.ref] ?? notes[String(ev.id)] ?? "").toString().trim() || null)
        .run();
    }
    linked = resolved.length;
  }
  let ncommit: number | undefined;
  if (Array.isArray(body.commitments)) {
    const texts = body.commitments
      .map((x: any) => ({
        text: (x.text ?? x.commitment ?? "").toString().trim(),
        section_id: x.section_id ? String(x.section_id) : null,
      }))
      .filter((x: any) => x.text);
    if (!texts.length) return json({ detail: "at least one commitment is required" }, 422);
    await db.prepare("DELETE FROM qa_coaching_commitments WHERE coaching_id = ?").bind(id).run();
    for (const t of texts) {
      await db
        .prepare("INSERT INTO qa_coaching_commitments (coaching_id, commitment, section_id) VALUES (?,?,?)")
        .bind(id, t.text, t.section_id)
        .run();
    }
    ncommit = texts.length;
  }
  return json({ ok: true, coaching_id: id, updated_fields: sets.length, linked_evaluations: linked, commitments: ncommit });
}

// ── POST conduct / cancel ──────────────────────────────────────────────────

export async function conductCoaching(
  request: Request,
  db: D1Database,
  teamId: string,
  id: number,
  lookupAllow?: string
): Promise<Response> {
  const access = await resolveAccess(request, db, lookupAllow);
  if (!canCoach(access, teamId))
    return json({ detail: "Documenting coaching sessions is restricted to QA staff and team managers." }, 403);
  const c = await coachingById(db, teamId, id);
  if (!c) return json({ detail: `no coaching ${id} on ${teamId}` }, 404);
  if (c.id < SANDY_BASE) return railwayBorn409();
  if (c.status !== "pending")
    return json({ detail: `coaching is '${c.status}' — only pending sessions can be conducted` }, 409);
  let body: any = {};
  try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }
  const summary = (body.coaching_summary ?? "").toString().trim();
  if (!summary) return json({ detail: "coaching_summary is required — document what was discussed" }, 422);
  if (!ATTITUDE_ENUM.has(body.agent_attitude))
    return json({ detail: "agent_attitude must be receptive|engaged|neutral|defensive|dismissive|mixed" }, 422);
  // A deadline-less receipt would never reach the confirmation queue —
  // require one before (or with) conduct.
  let deadline = c.action_plan_deadline;
  if (body.deadline !== undefined) {
    const d = (body.deadline ?? "").toString().trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) return json({ detail: "deadline must be YYYY-MM-DD" }, 422);
    deadline = d;
  }
  if (!deadline)
    return json({ detail: "a commitment deadline is required before conducting (PATCH it or pass deadline here)" }, 422);
  const completedBy = access.email || accessEmail(request);
  if (!completedBy) return json({ detail: "No SSO identity on this request." }, 401);
  const ts = now();
  await db
    .prepare(
      `UPDATE qa_coachings SET status='completed', coaching_summary=?,
        agent_attitude=?, action_plan_deadline=?, completed_at=?, completed_by=?
       WHERE id=?`
    )
    .bind(summary, body.agent_attitude, deadline, ts, completedBy, id)
    .run();
  const agentRow = await db
    .prepare("SELECT COALESCE(canonical_name, name) AS n FROM qa_agents WHERE id = ?")
    .bind(c.agent_id)
    .first<any>();
  await db
    .prepare("INSERT INTO qa_events (team_id, type, payload) VALUES (?, 'coaching_logged', ?)")
    .bind(
      teamId,
      JSON.stringify({
        coaching_id: id, agent: agentRow?.n ?? "", completed_by: completedBy,
        deadline, timestamp: ts,
      })
    )
    .run();
  return json({ ok: true, coaching_id: id, status: "completed", completed_at: ts, deadline });
}

export async function cancelCoaching(
  request: Request,
  db: D1Database,
  teamId: string,
  id: number,
  lookupAllow?: string
): Promise<Response> {
  const access = await resolveAccess(request, db, lookupAllow);
  if (!canCoach(access, teamId))
    return json({ detail: "Cancelling coaching sessions is restricted to QA staff and team managers." }, 403);
  const c = await coachingById(db, teamId, id);
  if (!c) return json({ detail: `no coaching ${id} on ${teamId}` }, 404);
  if (c.id < SANDY_BASE) return railwayBorn409();
  if (c.status === "cancelled") return json({ ok: true, coaching_id: id, status: "cancelled" });
  if (c.outcome !== null)
    return json({ detail: "outcome already confirmed — the record is final" }, 409);
  await db.prepare("UPDATE qa_coachings SET status='cancelled' WHERE id=?").bind(id).run();
  return json({ ok: true, coaching_id: id, status: "cancelled" });
}

// ── GET /api/{t}/coaching-queue (§6 — the queue is a predicate) ────────────

// Deterministic facts for the confirm decision (§11.4 windows): before =
// the 30 days leading into the session; during = completed_at → deadline
// end-of-day (the commitment-verdict window). Pure D1, always free.
async function verdictFacts(
  db: D1Database,
  teamId: string,
  agentId: number,
  completedAt: string,
  deadline: string
) {
  const stat = async (fromIso: string, toIso: string) => {
    const r = await db
      .prepare(
        `SELECT COUNT(*) AS n, AVG(overall_score) AS avg FROM qa_evaluations
         WHERE team_id = ? AND agent_id = ? AND state = 'finalized'
           AND COALESCE(call_connected_at, created_at) >= ?
           AND COALESCE(call_connected_at, created_at) < ?`
      )
      .bind(teamId, agentId, fromIso, toIso)
      .first<any>();
    return {
      n: r?.n ?? 0,
      avg: r?.avg !== null && r?.avg !== undefined ? Math.round(r.avg * 10) / 10 : null,
    };
  };
  const preStart = new Date(Date.parse(completedAt) - 30 * 86_400_000).toISOString();
  const deadlineEnd = `${deadline}T23:59:59Z`;
  return {
    before: await stat(preStart, completedAt),
    during: await stat(completedAt, deadlineEnd),
  };
}

export async function coachingQueue(
  request: Request,
  db: D1Database,
  teamId: string,
  lookupAllow?: string
): Promise<Response> {
  const access = await resolveAccess(request, db, lookupAllow);
  if (!canCoach(access, teamId))
    return json({ detail: "The coaching queue is restricted to QA staff and team managers." }, 403);
  const today = todayLA();
  const rows = await db
    .prepare(
      `SELECT c.id, c.agent_id, c.conducted_by_role, c.conducted_by_email,
              c.action_plan_deadline, c.completed_at, c.completed_by,
              COALESCE(a.canonical_name, a.name, '?') AS agent_name,
              (SELECT COUNT(*) FROM qa_coaching_commitments k
                WHERE k.coaching_id = c.id AND k.status = 'open') AS open_commitments
       FROM qa_coachings c LEFT JOIN qa_agents a ON a.id = c.agent_id
       WHERE c.team_id = ? AND c.status = 'completed' AND c.outcome IS NULL
         AND c.id >= ? AND c.action_plan_deadline IS NOT NULL
       ORDER BY c.action_plan_deadline`
    )
    .bind(teamId, SANDY_BASE)
    .all<any>();
  const due = rows.results.filter((r) => r.action_plan_deadline <= today);
  const upcoming = rows.results.filter((r) => r.action_plan_deadline > today);
  // Confirm-flow payload: commitments to verdict + the facts panel.
  await attachChildren(db, due);
  for (const r of due) {
    r.facts = await verdictFacts(
      db, teamId, r.agent_id, r.completed_at, r.action_plan_deadline
    );
  }
  return json({ team_id: teamId, today, count: due.length, due, upcoming });
}

// ── GET /api/{t}/coachings (team-wide list for the /coaching page) ─────────

export async function listTeamCoachings(
  request: Request,
  db: D1Database,
  teamId: string,
  url: URL,
  lookupAllow?: string
): Promise<Response> {
  const access = await resolveAccess(request, db, lookupAllow);
  if (!canCoach(access, teamId))
    return json({ detail: "Coaching records are restricted to QA staff and team managers." }, 403);
  const p = url.searchParams;
  const days = Math.min(730, Math.max(1, parseInt(p.get("days") ?? "180", 10) || 180));
  const cutoff = new Date(Date.now() - days * 86_400_000).toISOString();
  const status = p.get("status");
  const agent = (p.get("agent") ?? "").trim().toLowerCase();
  // Sandy-born only (CoachingTagsSpec §1.2 — Railway-era deprecated).
  let where = `c.team_id = ? AND c.id >= ? AND (c.status = 'pending' OR c.outcome IS NULL
               OR COALESCE(c.completed_at, c.created_at) >= ?)`;
  const binds: any[] = [teamId, SANDY_BASE, cutoff];
  if (status && ["pending", "completed", "cancelled"].includes(status)) {
    where += " AND c.status = ?";
    binds.push(status);
  }
  if (agent) {
    where += " AND (LOWER(a.name) LIKE ? OR LOWER(a.canonical_name) LIKE ?)";
    binds.push(`%${agent}%`, `%${agent}%`);
  }
  const rows = await db
    .prepare(
      `SELECT c.*, COALESCE(a.canonical_name, a.name, '?') AS agent_name
       FROM qa_coachings c LEFT JOIN qa_agents a ON a.id = c.agent_id
       WHERE ${where} ORDER BY c.created_at DESC LIMIT 200`
    )
    .bind(...binds)
    .all<any>();
  const sessions = rows.results.map((s) => ({ ...s, sandy_born: s.id >= SANDY_BASE }));
  await attachChildren(db, sessions);
  return json({ team_id: teamId, count: sessions.length, sessions });
}

// ── POST confirm — supervisor verdicts → outcome + the §6.5 T2 chiclet ─────

export async function confirmCoaching(
  request: Request,
  db: D1Database,
  teamId: string,
  id: number,
  lookupAllow?: string
): Promise<Response> {
  const access = await resolveAccess(request, db, lookupAllow);
  if (!canCoach(access, teamId))
    return json({ detail: "Confirming commitments is restricted to QA staff and team managers." }, 403);
  const c = await coachingById(db, teamId, id);
  if (!c) return json({ detail: `no coaching ${id} on ${teamId}` }, 404);
  if (c.id < SANDY_BASE) return railwayBorn409();
  if (c.status !== "completed")
    return json({ detail: `coaching is '${c.status}' — conduct (document) it before confirming` }, 409);
  if (c.outcome !== null)
    return json({ detail: `outcome already confirmed (${c.outcome}) by ${c.outcome_confirmed_by}` }, 409);
  let body: any = {};
  try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }
  const confirmer = access.email || accessEmail(request);
  if (!confirmer) return json({ detail: "No SSO identity on this request." }, 401);

  const open = (
    await db
      .prepare("SELECT id, commitment FROM qa_coaching_commitments WHERE coaching_id = ? AND status = 'open'")
      .bind(id)
      .all<any>()
  ).results;
  const verdicts = new Map<number, any>(
    (Array.isArray(body.commitments) ? body.commitments : []).map((v: any) => [Number(v.id), v])
  );
  const unknown = [...verdicts.keys()].filter((k) => !open.some((o) => o.id === k));
  if (unknown.length)
    return json({ detail: `unknown/closed commitment id(s): ${unknown.join(", ")}` }, 422);
  const missing = open.filter((o) => !verdicts.has(o.id));
  if (missing.length)
    return json(
      { detail: `every open commitment needs a verdict — missing: ${missing.map((m) => m.id).join(", ")}` },
      422
    );
  for (const [k, v] of verdicts) {
    if (!VERDICT_ENUM.has(v.status))
      return json({ detail: `commitment ${k}: status must be met|partially_met|not_met|waived` }, 422);
  }
  const voting = [...verdicts.values()].filter((v) => v.status !== "waived");
  if (!voting.length)
    return json({ detail: "all commitments waived — cancel the session instead of confirming" }, 422);
  const outcome = voting.every((v) => v.status === "met")
    ? "met"
    : voting.every((v) => v.status === "not_met")
      ? "not_met"
      : "partially_met";

  const ts = now();
  for (const [k, v] of verdicts) {
    await db
      .prepare(
        "UPDATE qa_coaching_commitments SET status=?, confirmed_by=?, confirmed_at=?, confirmation_note=? WHERE id=?"
      )
      .bind(v.status, confirmer, ts, (v.note ?? "").toString().trim() || null, k)
      .run();
  }
  await db
    .prepare(
      "UPDATE qa_coachings SET outcome=?, outcome_confirmed_by=?, outcome_confirmed_at=?, outcome_note=? WHERE id=?"
    )
    .bind(outcome, confirmer, ts, (body.outcome_note ?? "").toString().trim() || null, id)
    .run();

  // §6.5: the confirmation IS the first Sandy-side T2 chiclet. cc_chiclets
  // is Sandy-owned (empty at 0007, outside the cc sync) — AUTOINCREMENT ids.
  const agentRow = await db
    .prepare("SELECT COALESCE(canonical_name, name) AS n FROM qa_agents WHERE id = ?")
    .bind(c.agent_id)
    .first<any>();
  const agentName = agentRow?.n ?? "";
  const counts = { met: 0, partially_met: 0, not_met: 0, waived: 0 } as Record<string, number>;
  for (const v of verdicts.values()) counts[v.status] += 1;
  const outcomeLabel = outcome.replace(/_/g, " ");
  const chicletData = {
    coaching_id: id, outcome, commitments: counts,
    deadline: c.action_plan_deadline, confirmed_by: confirmer, agent: agentName,
  };
  const ins = await db
    .prepare(
      `INSERT INTO cc_chiclets (team_id, type, tier, status, agent_name, summary, data)
       VALUES (?, 'coaching', 'T2', 'active', ?, ?, ?)`
    )
    .bind(
      teamId, agentName,
      `Coaching outcome: ${outcomeLabel} — ${agentName}, ${open.length} commitment${open.length === 1 ? "" : "s"}`,
      JSON.stringify(chicletData)
    )
    .run();
  const chicletId = Number(ins.meta.last_row_id);
  await db
    .prepare("INSERT INTO cc_chiclet_events (chiclet_id, event_type, payload) VALUES (?, 'created', ?)")
    .bind(chicletId, JSON.stringify({ coaching_id: id }))
    .run();
  // SSE, in the CC §6 protocol shape — the future ported CC page consumes
  // this stream unchanged; team_dashboard's rail strip is the first reader.
  const chicletRow = await db
    .prepare("SELECT * FROM cc_chiclets WHERE id = ?")
    .bind(chicletId)
    .first<any>();
  await db
    .prepare("INSERT INTO qa_events (team_id, type, payload) VALUES (?, 'chiclet_created', ?)")
    .bind(teamId, JSON.stringify({ id: chicletId, tier: 2, type: "coaching", chiclet: chicletRow }))
    .run();
  await db
    .prepare("INSERT INTO qa_events (team_id, type, payload) VALUES (?, 'coaching_confirmed', ?)")
    .bind(
      teamId,
      JSON.stringify({ coaching_id: id, agent: agentName, outcome, confirmed_by: confirmer, timestamp: ts })
    )
    .run();

  return json({ ok: true, coaching_id: id, outcome, chiclet_id: chicletId, confirmed_at: ts });
}

// ── chiclet read + resolve (rail contract; strip UI lands CL3) ─────────────

export async function listChiclets(
  db: D1Database,
  teamId: string,
  url: URL
): Promise<Response> {
  // Team-visible like the rest of the dashboard APIs (the rail renders on
  // the viewer-open team dashboard).
  const status = url.searchParams.get("status") === "resolved" ? "resolved" : "active";
  const rows = await db
    .prepare(
      "SELECT * FROM cc_chiclets WHERE team_id = ? AND status = ? ORDER BY created_at DESC LIMIT 50"
    )
    .bind(teamId, status)
    .all<any>();
  return json({ team_id: teamId, status, chiclets: rows.results });
}

export async function resolveChiclet(
  request: Request,
  db: D1Database,
  teamId: string,
  id: number,
  lookupAllow?: string
): Promise<Response> {
  const access = await resolveAccess(request, db, lookupAllow);
  if (!canCoach(access, teamId))
    return json({ detail: "Acknowledging chiclets is restricted to QA staff and team managers." }, 403);
  const resolver = access.email || accessEmail(request);
  if (!resolver) return json({ detail: "No SSO identity on this request." }, 401);
  const row = await db
    .prepare("SELECT id, status FROM cc_chiclets WHERE id = ? AND team_id = ?")
    .bind(id, teamId)
    .first<any>();
  if (!row) return json({ detail: `no chiclet ${id} on ${teamId}` }, 404);
  if (row.status === "resolved") return json({ ok: true, chiclet_id: id, status: "resolved" });
  const ts = now();
  await db
    .prepare("UPDATE cc_chiclets SET status='resolved', resolved_at=?, resolved_by=? WHERE id=?")
    .bind(ts, resolver, id)
    .run();
  await db
    .prepare("INSERT INTO cc_chiclet_events (chiclet_id, event_type, payload) VALUES (?, 'resolved', ?)")
    .bind(id, JSON.stringify({ resolved_by: resolver }))
    .run();
  await db
    .prepare("INSERT INTO qa_events (team_id, type, payload) VALUES (?, 'chiclet_resolved', ?)")
    .bind(teamId, JSON.stringify({ id, resolved_by: resolver, timestamp: ts }))
    .run();
  return json({ ok: true, chiclet_id: id, status: "resolved", resolved_at: ts });
}
