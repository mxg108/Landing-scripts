// Coaching tag vocabulary — CoachingTagsSpec §3 + §2.1 (T1). Three KINDS:
// SUPERTAG (the 0008 `type` CHECK enum), TAG (parent_tag_id NULL), SUBTAG
// (parent set, any depth — kind is derived, never stored). Globally-unique
// normalized names carrying the parent prefix, soft deprecation that
// CASCADES down the family (restore doesn't, and is blocked under a
// deprecated ancestor), session↔tag links that FREEZE once the session's
// outcome is confirmed. Cross-team vocabulary: the {team} in the URL is
// routing convention; scoping is a RAG-time concern (owner §8.4). Every
// route is gated `coach`.

import { accessEmail, canCoach, resolveAccess } from "../lib/rbac.js";

export const TAG_TYPES = ["sop", "system_skills", "soft_skills", "effectiveness"] as const;
const SANDY_BASE = 10_000_000;

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const now = () => new Date().toISOString();

// snake_case normalization: lowercase, trim, spaces/hyphens → underscores,
// strip anything else, collapse runs. "Cancellation Policy" → cancellation_policy.
export function normalizeTagName(raw: string): string {
  return String(raw ?? "")
    .toLowerCase()
    .trim()
    .replace(/[\s\-]+/g, "_")
    .replace(/[^a-z0-9_]/g, "")
    .replace(/_+/g, "_")
    .replace(/^_|_$/g, "");
}

async function gate(request: Request, db: D1Database, teamId: string, lookupAllow?: string) {
  const access = await resolveAccess(request, db, lookupAllow);
  if (!canCoach(access, teamId)) return { deny: json({ detail: "Coaching tags are restricted to QA staff and team managers." }, 403) };
  const email = access.email || accessEmail(request);
  if (!email) return { deny: json({ detail: "No SSO identity on this request." }, 401) };
  return { email };
}

// ── GET /api/{t}/coach-tags ────────────────────────────────────────────────

export async function listTags(
  request: Request,
  db: D1Database,
  teamId: string,
  url: URL,
  lookupAllow?: string
): Promise<Response> {
  const g = await gate(request, db, teamId, lookupAllow);
  if ("deny" in g) return g.deny;
  const includeDeprecated = url.searchParams.get("include") === "deprecated";
  const rows = (
    await db
      .prepare(
        `SELECT t.*, r.name AS replaced_by_name, p.name AS parent_name,
                CASE WHEN t.parent_tag_id IS NULL THEN 'tag' ELSE 'subtag' END AS kind,
                (SELECT COUNT(*) FROM qa_coach_tags ch
                  WHERE ch.parent_tag_id = t.id AND ch.status = 'active') AS active_children,
                (SELECT COUNT(*) FROM qa_coaching_tag_links l
                  JOIN qa_coachings c ON c.id = l.coaching_id
                  WHERE l.tag_id = t.id AND c.id >= ?) AS sessions
         FROM qa_coach_tags t
         LEFT JOIN qa_coach_tags r ON r.id = t.replaced_by_tag_id
         LEFT JOIN qa_coach_tags p ON p.id = t.parent_tag_id
         ${includeDeprecated ? "" : "WHERE t.status = 'active'"}
         ORDER BY t.type, t.name`
      )
      .bind(SANDY_BASE)
      .all<any>()
  ).results;
  // by_type keeps name-sorted rows — the parent prefix means a family
  // always groups contiguously under its root, so the picker can render
  // trees (or indented lists) straight off this order.
  const byType: Record<string, any[]> = {};
  for (const t of TAG_TYPES) byType[t] = [];
  for (const r of rows) (byType[r.type] ??= []).push(r);
  return json({ types: [...TAG_TYPES], tags: rows, by_type: byType });
}

// ── POST /api/{t}/coach-tags ───────────────────────────────────────────────

export async function createTag(
  request: Request,
  db: D1Database,
  teamId: string,
  lookupAllow?: string
): Promise<Response> {
  const g = await gate(request, db, teamId, lookupAllow);
  if ("deny" in g) return g.deny;
  let body: any = {};
  try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }

  // §2.1: a parent makes this a SUBTAG — type is INHERITED from the parent
  // chain (a passed type must agree), the name auto-prefixes with the
  // parent's, and the parent must be active. No parent = TAG (type required).
  let parent: any = null;
  let type: string;
  if (body.parent_tag_id !== undefined && body.parent_tag_id !== null) {
    parent = await db
      .prepare("SELECT id, type, name, status FROM qa_coach_tags WHERE id = ?")
      .bind(Number(body.parent_tag_id))
      .first<any>();
    if (!parent) return json({ detail: `parent_tag_id ${body.parent_tag_id} does not exist` }, 422);
    if (parent.status !== "active")
      return json({ detail: `parent '${parent.name}' is deprecated — restore it before adding subtags` }, 422);
    if (body.type !== undefined && String(body.type) !== parent.type)
      return json(
        { detail: `subtags inherit the supertag — '${parent.name}' is ${parent.type}, not ${body.type}` },
        422
      );
    type = parent.type;
  } else {
    type = String(body.type ?? "");
    if (!(TAG_TYPES as readonly string[]).includes(type))
      return json({ detail: `type must be one of ${TAG_TYPES.join("|")}` }, 422);
  }

  let name = normalizeTagName(body.name);
  if (!name) return json({ detail: "name is required (letters, digits, underscores)" }, 422);
  // Leaf-or-full: "cold" and "dialpad_transfers_cold" both land on the
  // prefixed name; the prefix keeps the whole family adjacent in listings.
  if (parent && name !== parent.name && !name.startsWith(parent.name + "_"))
    name = `${parent.name}_${name}`;
  if (parent && name === parent.name)
    return json({ detail: "subtag needs a leaf name beyond its parent's" }, 422);

  const existing = await db
    .prepare("SELECT id, type, status, parent_tag_id FROM qa_coach_tags WHERE name = ?")
    .bind(name)
    .first<any>();
  if (existing) {
    return json(
      {
        detail: existing.status === "deprecated"
          ? `'${name}' exists but is deprecated — restore it or pick a new name`
          : `'${name}' already exists (type ${existing.type})`,
        tag_id: existing.id,
        status: existing.status,
        type: existing.type,
        parent_tag_id: existing.parent_tag_id,
      },
      409
    );
  }
  const ins = await db
    .prepare("INSERT INTO qa_coach_tags (type, name, description, created_by, parent_tag_id) VALUES (?,?,?,?,?)")
    .bind(type, name, (body.description ?? "").toString().trim() || null, g.email, parent?.id ?? null)
    .run();
  const id = Number(ins.meta.last_row_id);
  const row = await db.prepare("SELECT * FROM qa_coach_tags WHERE id = ?").bind(id).first<any>();
  return json({ ok: true, tag: { ...row, kind: parent ? "subtag" : "tag", parent_name: parent?.name ?? null } }, 201);
}

// ── POST deprecate / restore ───────────────────────────────────────────────

export async function deprecateTag(
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
  const tag = await db.prepare("SELECT * FROM qa_coach_tags WHERE id = ?").bind(id).first<any>();
  if (!tag) return json({ detail: `no tag ${id}` }, 404);
  if (tag.status === "deprecated") return json({ ok: true, tag_id: id, status: "deprecated", idempotent: true });
  let replacedBy: number | null = null;
  if (body.replaced_by_tag_id !== undefined && body.replaced_by_tag_id !== null) {
    replacedBy = Number(body.replaced_by_tag_id);
    if (replacedBy === id) return json({ detail: "a tag cannot replace itself" }, 422);
    const target = await db
      .prepare("SELECT id, status FROM qa_coach_tags WHERE id = ?")
      .bind(replacedBy)
      .first<any>();
    if (!target) return json({ detail: `replaced_by_tag_id ${replacedBy} does not exist` }, 422);
    if (target.status !== "active") return json({ detail: "replacement tag must be active" }, 422);
  }
  const ts = now();
  const note = (body.note ?? "").toString().trim() || null;
  await db
    .prepare(
      "UPDATE qa_coach_tags SET status='deprecated', deprecated_by=?, deprecated_at=?, deprecation_note=?, replaced_by_tag_id=? WHERE id=?"
    )
    .bind(g.email, ts, note, replacedBy, id)
    .run();
  // §2.1: deprecation CASCADES down the family — an axis retired at the
  // root retires its whole subtree (each level in turn, depth-safe).
  const cascaded: number[] = [];
  let frontier = [id];
  while (frontier.length) {
    const ph = frontier.map(() => "?").join(",");
    const kids = (
      await db
        .prepare(`SELECT id FROM qa_coach_tags WHERE parent_tag_id IN (${ph}) AND status = 'active'`)
        .bind(...frontier)
        .all<any>()
    ).results.map((r) => r.id);
    if (!kids.length) break;
    await db
      .prepare(
        `UPDATE qa_coach_tags SET status='deprecated', deprecated_by=?, deprecated_at=?,
          deprecation_note=? WHERE id IN (${kids.map(() => "?").join(",")})`
      )
      .bind(g.email, ts, `cascade: parent ${tag.name} deprecated${note ? ` (${note})` : ""}`, ...kids)
      .run();
    cascaded.push(...kids);
    frontier = kids;
  }
  return json({ ok: true, tag_id: id, status: "deprecated", replaced_by_tag_id: replacedBy, cascaded });
}

export async function restoreTag(
  request: Request,
  db: D1Database,
  teamId: string,
  id: number,
  lookupAllow?: string
): Promise<Response> {
  const g = await gate(request, db, teamId, lookupAllow);
  if ("deny" in g) return g.deny;
  const tag = await db.prepare("SELECT * FROM qa_coach_tags WHERE id = ?").bind(id).first<any>();
  if (!tag) return json({ detail: `no tag ${id}` }, 404);
  if (tag.status === "active") return json({ ok: true, tag_id: id, status: "active", idempotent: true });
  // §2.1: restore is per-node and blocked under a deprecated ancestor —
  // a live subtag inside a dead family would be unreachable in pickers
  // and incoherent in rollups. Restore the ancestors first (each restore
  // is one node; deprecation's cascade is deliberately asymmetric).
  let pid = tag.parent_tag_id;
  while (pid) {
    const anc = await db
      .prepare("SELECT id, name, status, parent_tag_id FROM qa_coach_tags WHERE id = ?")
      .bind(pid)
      .first<any>();
    if (!anc) break;
    if (anc.status !== "active")
      return json({ detail: `ancestor '${anc.name}' is deprecated — restore it first` }, 409);
    pid = anc.parent_tag_id;
  }
  await db
    .prepare(
      "UPDATE qa_coach_tags SET status='active', deprecated_by=NULL, deprecated_at=NULL, deprecation_note=NULL, replaced_by_tag_id=NULL WHERE id=?"
    )
    .bind(id)
    .run();
  return json({ ok: true, tag_id: id, status: "active" });
}

// ── session tag set (shared by PUT /coachings/{id}/tags and the conduct/
//    confirm bodies' optional tag_ids) ──────────────────────────────────────

// Full-replace the tag set on a session. Rules (§3): allowed while the
// outcome is unconfirmed; 409 after confirm (tags freeze with the record);
// deprecated tags accepted only if ALREADY linked (history keeps them,
// nobody adds them). Returns {ok} | {error: Response}.
export async function setSessionTags(
  db: D1Database,
  coaching: { id: number; outcome: string | null },
  tagIds: unknown,
  linkedBy: string
): Promise<{ ok: true; linked: number } | { ok: false; error: Response }> {
  if (!Array.isArray(tagIds))
    return { ok: false, error: json({ detail: "tag_ids must be an array of tag ids" }, 422) };
  if (coaching.outcome !== null && coaching.outcome !== undefined)
    return { ok: false, error: json({ detail: "outcome already confirmed — tags are frozen with the record" }, 409) };
  const ids = [...new Set(tagIds.map((x: any) => Number(x)).filter((n: number) => Number.isInteger(n) && n > 0))];
  const current = new Set(
    (
      await db
        .prepare("SELECT tag_id FROM qa_coaching_tag_links WHERE coaching_id = ?")
        .bind(coaching.id)
        .all<any>()
    ).results.map((r) => r.tag_id)
  );
  if (ids.length) {
    const rows = (
      await db
        .prepare(`SELECT id, status FROM qa_coach_tags WHERE id IN (${ids.map(() => "?").join(",")})`)
        .bind(...ids)
        .all<any>()
    ).results;
    const found = new Map(rows.map((r) => [r.id, r.status]));
    const missing = ids.filter((i) => !found.has(i));
    if (missing.length)
      return { ok: false, error: json({ detail: `unknown tag id(s): ${missing.join(", ")}` }, 422) };
    const newlyDeprecated = ids.filter((i) => found.get(i) === "deprecated" && !current.has(i));
    if (newlyDeprecated.length)
      return { ok: false, error: json({ detail: `deprecated tag(s) can't be added: ${newlyDeprecated.join(", ")}` }, 422) };
  }
  await db.prepare("DELETE FROM qa_coaching_tag_links WHERE coaching_id = ?").bind(coaching.id).run();
  for (const tid of ids) {
    await db
      .prepare("INSERT INTO qa_coaching_tag_links (coaching_id, tag_id, linked_by) VALUES (?,?,?)")
      .bind(coaching.id, tid, linkedBy)
      .run();
  }
  return { ok: true, linked: ids.length };
}

export async function putSessionTags(
  request: Request,
  db: D1Database,
  teamId: string,
  coachingId: number,
  lookupAllow?: string
): Promise<Response> {
  const g = await gate(request, db, teamId, lookupAllow);
  if ("deny" in g) return g.deny;
  let body: any = {};
  try { body = await request.json(); } catch { return json({ detail: "JSON body required" }, 422); }
  const c = await db
    .prepare("SELECT id, outcome, status FROM qa_coachings WHERE id = ? AND team_id = ?")
    .bind(coachingId, teamId)
    .first<any>();
  if (!c) return json({ detail: `no coaching ${coachingId} on ${teamId}` }, 404);
  if (c.id < SANDY_BASE)
    return json({ detail: "This coaching is Railway-born — read-only on Sandy." }, 409);
  if (c.status === "cancelled") return json({ detail: "session is cancelled" }, 409);
  const res = await setSessionTags(db, c, body.tag_ids, g.email);
  if (!res.ok) return res.error;
  return json({ ok: true, coaching_id: coachingId, linked: res.linked });
}

// Tags for a set of sessions — used by attachChildren in coaching.ts.
export async function tagsForSessions(
  db: D1Database,
  coachingIds: number[]
): Promise<Map<number, any[]>> {
  const out = new Map<number, any[]>();
  for (let i = 0; i < coachingIds.length; i += 80) {
    const chunk = coachingIds.slice(i, i + 80);
    const rows = await db
      .prepare(
        `SELECT l.coaching_id, t.id, t.type, t.name, t.status, l.linked_by, l.linked_at
         FROM qa_coaching_tag_links l JOIN qa_coach_tags t ON t.id = l.tag_id
         WHERE l.coaching_id IN (${chunk.map(() => "?").join(",")})
         ORDER BY t.type, t.name`
      )
      .bind(...chunk)
      .all<any>();
    for (const r of rows.results) {
      let g = out.get(r.coaching_id);
      if (!g) out.set(r.coaching_id, (g = []));
      g.push({ id: r.id, type: r.type, name: r.name, status: r.status, linked_by: r.linked_by, linked_at: r.linked_at });
    }
  }
  return out;
}
