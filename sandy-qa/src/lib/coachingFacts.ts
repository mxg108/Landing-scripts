// Coaching facts — CoachingLoopSpec §8 layer 1. Deterministic D1
// aggregation only: every number an AI narrative cites is computed here
// first and travels with the prompt, so trainers can always check the
// prose against the figures. No AI calls in this module (or this app).
//
// Scopes: agent (progression prompts + EOM), coaching (per-session
// readouts — verdictFacts in coaching.ts covers the queue's lighter
// need). Team scope lands with CL5.

import type { TeamConfig } from "./teamConfig.js";
import { fetchRecords } from "./records.js";

const r1 = (n: number) => Math.round(n * 10) / 10;
const mean = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;
const SANDY_BASE = 10_000_000;

// Finalized-eval stats in [fromIso, toIso) — shared window primitive.
export async function evalWindowStat(
  db: D1Database,
  teamId: string,
  agentId: number,
  fromIso: string,
  toIso: string
): Promise<{ n: number; avg: number | null }> {
  const r = await db
    .prepare(
      `SELECT COUNT(*) AS n, AVG(overall_score) AS avg FROM qa_evaluations
       WHERE team_id = ? AND agent_id = ? AND state = 'finalized'
         AND COALESCE(call_connected_at, created_at) >= ?
         AND COALESCE(call_connected_at, created_at) < ?`
    )
    .bind(teamId, agentId, fromIso, toIso)
    .first<any>();
  return { n: r?.n ?? 0, avg: r?.avg != null ? r1(r.avg) : null };
}

// Per-section stat over provider-shaped records (same half-vs-half trend
// the one-pager renders — one methodology, two surfaces).
function sectionStat(values: (string | null)[]) {
  const strs = values.map((v) => String(v ?? "").trim());
  const isBinary = strs.some((v) => ["Yes", "Y", "No", "N"].includes(v));
  const na = strs.filter((v) => v === "Not Applicable" || v === "NA").length;
  if (isBinary) {
    const map = (half: string[]) =>
      half
        .map((v) => (["Yes", "Y"].includes(v) ? 1 : ["No", "N"].includes(v) ? 0 : null))
        .filter((v): v is number => v !== null);
    const all = map(strs);
    const mid = Math.floor(strs.length / 2);
    const f = map(strs.slice(0, mid)), s = map(strs.slice(mid));
    return {
      kind: "binary" as const, na,
      pass_pct: all.length ? r1(mean(all) * 100) : null,
      delta: f.length && s.length ? r1((mean(s) - mean(f)) * 100) : null,
    };
  }
  const nums = strs.map((v) => parseFloat(v)).filter((v) => Number.isFinite(v));
  const mid = Math.floor(nums.length / 2);
  const f = nums.slice(0, mid), s = nums.slice(mid);
  return {
    kind: "numeric" as const, na,
    avg: nums.length ? r1(mean(nums)) : null,
    delta: f.length && s.length ? r1(mean(s) - mean(f)) : null,
  };
}

export interface AgentFactsWindow {
  fromMs: number;
  toMs: number;
  label: string; // e.g. "last 30 days" | "July 2026"
}

// The full agent-progression fact sheet the prompt serializes.
export async function agentFacts(
  db: D1Database,
  config: TeamConfig,
  agentRow: { id: number; name: string; canonical_name: string | null },
  win: AgentFactsWindow
): Promise<any | null> {
  const records = await fetchRecords(db, config, {
    agentRaw: agentRow.name,
    fromMs: win.fromMs,
    toMs: win.toMs,
  });
  if (!records.length) return null;

  const overall = records.map((r) => r.overall_score);
  const mid = Math.floor(overall.length / 2);
  const sections = config.sections_by_number
    .filter((s) => !s.auto_value)
    .map((s) => ({
      id: s.id,
      name: s.name,
      ...sectionStat(records.map((r) => (r.sections?.[s.history_id] ?? {}).score ?? null)),
    }));

  // Coachings intersecting the window (conducted inside it, or whose
  // deadline lands in it) + each session's before/after around conduct.
  const coachRows = (
    await db
      .prepare(
        `SELECT * FROM qa_coachings
         WHERE team_id = ? AND agent_id = ? AND status != 'cancelled'
           AND ((completed_at IS NOT NULL AND completed_at >= ? AND completed_at < ?)
                OR (action_plan_deadline IS NOT NULL
                    AND action_plan_deadline >= ? AND action_plan_deadline <= ?))
         ORDER BY COALESCE(completed_at, created_at)`
      )
      .bind(
        config.team_id, agentRow.id,
        new Date(win.fromMs).toISOString(), new Date(win.toMs).toISOString(),
        new Date(win.fromMs).toISOString().slice(0, 10),
        new Date(win.toMs).toISOString().slice(0, 10)
      )
      .all<any>()
  ).results;
  const coachings: any[] = [];
  for (const c of coachRows) {
    const commits = (
      await db
        .prepare(
          "SELECT commitment, section_id, status, confirmation_note FROM qa_coaching_commitments WHERE coaching_id = ? ORDER BY id"
        )
        .bind(c.id)
        .all<any>()
    ).results;
    let pre: any = null, post: any = null;
    if (c.completed_at) {
      const preStart = new Date(Date.parse(c.completed_at) - 30 * 86_400_000).toISOString();
      const postEnd = c.action_plan_deadline
        ? `${c.action_plan_deadline}T23:59:59Z`
        : new Date().toISOString();
      pre = await evalWindowStat(db, config.team_id, agentRow.id, preStart, c.completed_at);
      post = await evalWindowStat(db, config.team_id, agentRow.id, c.completed_at, postEnd);
    }
    coachings.push({
      coaching_id: c.id,
      conducted_at: c.completed_at,
      deadline: c.action_plan_deadline,
      outcome: c.outcome, // null = not yet confirmed by the supervisor
      outcome_note: c.outcome_note,
      agent_attitude: c.agent_attitude,
      commitments: commits.map((k) => ({
        text: k.commitment,
        section_id: k.section_id,
        status: k.status, // open | met | partially_met | not_met | waived
        note: k.confirmation_note,
      })),
      before_coaching: pre,   // 30d leading in
      after_coaching: post,   // conduct → deadline (or now)
    });
  }

  return {
    agent: agentRow.canonical_name || agentRow.name,
    team_id: config.team_id,
    window: win.label,
    evaluations: {
      n: records.length,
      avg: r1(mean(overall)),
      first_half_avg: mid ? r1(mean(overall.slice(0, mid))) : null,
      second_half_avg: mid ? r1(mean(overall.slice(mid))) : null,
      min: Math.min(...overall),
      max: Math.max(...overall),
    },
    sections,
    coachings,
  };
}

// ── team scope (CL5) — "which areas of opportunity to tackle most" ─────────
// Takes only (teamId, section-name map) rather than a full TeamConfig so
// the harness can exercise it without a rubric fixture; the route passes
// real config-derived names.

export async function teamFacts(
  db: D1Database,
  teamId: string,
  sectionNameById: Record<string, string>,
  windowDays: number
): Promise<any> {
  const fromIso = new Date(Date.now() - windowDays * 86_400_000).toISOString();
  const secName = (id: string | null) =>
    id ? (sectionNameById[id] ?? id) : "(no section linked)";

  const base = await db
    .prepare(
      `SELECT COUNT(*) AS n, AVG(overall_score) AS avg FROM qa_evaluations
       WHERE team_id = ? AND state = 'finalized'
         AND COALESCE(call_connected_at, created_at) >= ?`
    )
    .bind(teamId, fromIso)
    .first<any>();

  // Sandy-born only (CoachingTagsSpec §1.2): Railway-era coachings are
  // deprecated read-side — they also produced the "overdue confirmations"
  // noise the Sandy queue could never act on.
  const sessions = (
    await db
      .prepare(
        `SELECT c.*, COALESCE(a.canonical_name, a.name, '?') AS agent_name
         FROM qa_coachings c LEFT JOIN qa_agents a ON a.id = c.agent_id
         WHERE c.team_id = ? AND c.id >= ? AND c.status != 'cancelled'
           AND COALESCE(c.completed_at, c.created_at) >= ?`
      )
      .bind(teamId, SANDY_BASE, fromIso)
      .all<any>()
  ).results;

  const commits = sessions.length
    ? (
        await db
          .prepare(
            `SELECT k.*, c.agent_id, c.completed_at, c.action_plan_deadline
             FROM qa_coaching_commitments k JOIN qa_coachings c ON c.id = k.coaching_id
             WHERE c.team_id = ? AND c.id >= ? AND c.status != 'cancelled'
               AND COALESCE(c.completed_at, c.created_at) >= ?`
          )
          .bind(teamId, SANDY_BASE, fromIso)
          .all<any>()
      ).results
    : [];

  // Most-coached sections + their post-coaching movement: for every
  // commitment linked to a section, the agent's overall before vs after
  // that session (windows as everywhere: 30d-in → conduct → deadline/now).
  const bySection = new Map<string, any>();
  for (const k of commits) {
    const key = k.section_id ?? "__none__";
    let g = bySection.get(key);
    if (!g)
      bySection.set(key, (g = {
        section_id: k.section_id, section: secName(k.section_id),
        commitments: 0, met: 0, partially_met: 0, not_met: 0, open: 0, waived: 0,
        deltas: [] as number[],
      }));
    g.commitments++;
    g[k.status] = (g[k.status] ?? 0) + 1;
    if (k.completed_at) {
      const preStart = new Date(Date.parse(k.completed_at) - 30 * 86_400_000).toISOString();
      const postEnd = k.action_plan_deadline
        ? `${k.action_plan_deadline}T23:59:59Z`
        : new Date().toISOString();
      const pre = await evalWindowStat(db, teamId, k.agent_id, preStart, k.completed_at);
      const post = await evalWindowStat(db, teamId, k.agent_id, k.completed_at, postEnd);
      if (pre.avg !== null && post.avg !== null) g.deltas.push(r1(post.avg - pre.avg));
    }
  }
  const coachedSections = [...bySection.values()]
    .map((g) => ({
      ...g,
      avg_overall_delta_after_coaching: g.deltas.length ? r1(mean(g.deltas)) : null,
      deltas: undefined,
    }))
    .sort((a, b) => b.commitments - a.commitments);

  // Coverage + hygiene + met-rate.
  const roster = (
    await db
      .prepare("SELECT COALESCE(canonical_name, name) AS n FROM qa_agents WHERE team_id = ? AND active = 1")
      .bind(teamId)
      .all<any>()
  ).results.map((r) => r.n);
  const coachedAgents = [...new Set(sessions.map((s) => s.agent_name))];
  const today = new Intl.DateTimeFormat("sv-SE", { timeZone: "America/Los_Angeles" }).format(new Date());
  const overdue = sessions.filter(
    (s) => s.status === "completed" && !s.outcome &&
      s.action_plan_deadline && s.action_plan_deadline <= today
  ).length;
  const neverConducted = sessions.filter((s) => s.status === "pending").length;
  const closed = commits.filter((k) => k.status !== "open" && k.status !== "waived");
  const metRate = closed.length
    ? r1((closed.filter((k) => k.status === "met").length / closed.length) * 100)
    : null;

  return {
    team_id: teamId,
    window_days: windowDays,
    evaluations: { n: base?.n ?? 0, avg: base?.avg != null ? r1(base.avg) : null },
    coaching: {
      sessions: sessions.length,
      confirmed: sessions.filter((s) => s.outcome).length,
      outcomes: {
        met: sessions.filter((s) => s.outcome === "met").length,
        partially_met: sessions.filter((s) => s.outcome === "partially_met").length,
        not_met: sessions.filter((s) => s.outcome === "not_met").length,
      },
      commitment_met_rate_pct: metRate,
      overdue_confirmations: overdue,
      pending_never_conducted: neverConducted,
    },
    coached_sections: coachedSections,
    coverage: {
      active_agents: roster.length,
      agents_coached: coachedAgents.length,
      agents_not_coached: roster.filter((n) => !coachedAgents.includes(n)),
    },
  };
}
