// CC call-context grounding — port of backend/services/cc_context.py
// (DispositionDesign §5). Triple-key match against cc_calls, hold cycles
// from cc_hold_intervals, and the EMPHASIZED "CALL CONTEXT (VERIFIED
// SYSTEM DATA)" block that rides AHEAD of the transcript / annotated
// record in the scoring prompt. Non-fatal by doctrine: any failure
// returns null and scoring proceeds ungrounded.
//
// Freshness note: cc_calls is fed by the shadow sync (and the hourly
// Railway stats pull upstream of it) — a just-ended call may not be
// matched yet; the absence path words that case safely.

export interface CallContext {
  // null when the context was synthesized from the nightly sweep's Stats
  // CSV (NightlyScoring §5.3) — no cc_calls row existed at enqueue time.
  cc_call_id: number | null;
  matched_by: string;
  disposition_category: string | null;
  disposition: string | null;
  ai_csat: number | null;
  total_hold_seconds: number;
  connected_at: string | null;
  started_at: string | null;
  holds: { started_at: string; ended_at: string; seconds: number }[];
  has_hold_truth: boolean;
}

const MATCH_ORDER = ["entry_point", "call_id", "master"] as const;

const MATCH_SQL = `
SELECT id, disposition_category, disposition, ai_csat,
       total_hold_seconds, connected_at, started_at, seen_via
FROM cc_calls
WHERE team_id = ?
  AND (dialpad_call_id = ? OR dialpad_entry_point_call_id = ? OR dialpad_master_call_id = ?)
LIMIT 1`;

export async function fetchCallContext(
  db: D1Database,
  teamId: string,
  ids: { entry_point?: string; call_id?: string; master?: string }
): Promise<CallContext | null> {
  try {
    let row: any = null;
    let matchedBy = "";
    for (const key of MATCH_ORDER) {
      const id = (ids[key] ?? "").trim();
      if (!id) continue;
      row = await db.prepare(MATCH_SQL).bind(teamId, id, id, id).first<any>();
      if (row) {
        matchedBy = key;
        break;
      }
    }
    if (!row) return null;
    const holds = await db
      .prepare(
        "SELECT started_at, ended_at, seconds FROM cc_hold_intervals WHERE call_id = ? ORDER BY started_at"
      )
      .bind(row.id)
      .all<any>();
    return {
      cc_call_id: row.id,
      matched_by: matchedBy,
      disposition_category: row.disposition_category ?? null,
      disposition: row.disposition ?? null,
      ai_csat: row.ai_csat !== null && row.ai_csat !== undefined ? Number(row.ai_csat) : null,
      total_hold_seconds: row.total_hold_seconds ?? 0,
      connected_at: row.connected_at ?? null,
      started_at: row.started_at ?? null,
      holds: holds.results,
      has_hold_truth: row.seen_via === "webhook",
    };
  } catch {
    return null; // grounding is an enhancement, never a scoring blocker
  }
}

function mmss(totalSeconds: number): string {
  const t = Math.max(0, Math.trunc(totalSeconds));
  return `${Math.trunc(t / 60)}:${String(t % 60).padStart(2, "0")}`;
}

function absenceSentence(language: string | null): string {
  if (language === "es")
    return (
      "No disposition was captured for this call (back-to-back handling) — " +
      "score on the AUDIO content: for Spanish calls the audio is the source " +
      "of truth and the transcript is unreliable."
    );
  if (language === "en")
    return (
      "No disposition was captured for this call (back-to-back handling) — " +
      "score on transcript evidence alone."
    );
  return (
    "No disposition was captured for this call (back-to-back handling) — " +
    "score on transcript evidence alone, OR on the audio content if the call " +
    "is in Spanish (for Spanish calls the audio is the source of truth)."
  );
}

export function buildCallContextBlock(
  ctx: CallContext | null,
  language: string | null = null
): string {
  if (ctx === null) return "";
  const lines = [
    "",
    "=== CALL CONTEXT (VERIFIED SYSTEM DATA) ===",
    "IMPORTANT: the facts below are verified Dialpad system records for " +
      "THIS call. They are ground truth — do not second-guess them from " +
      "the transcript or audio.",
    "",
  ];

  if (ctx.disposition_category) {
    const label =
      ctx.disposition_category + (ctx.disposition ? ` — ${ctx.disposition}` : "");
    lines.push(
      `- The agent classified this call as: ${label}. Score the call ` +
        "within reason FOR that disposition — the expectations of each " +
        "section apply as they pertain to this call type."
    );
  } else {
    lines.push(`- ${absenceSentence(language)}`);
  }

  const anchor = ctx.connected_at ?? ctx.started_at;
  if (!ctx.has_hold_truth) {
    lines.push(
      "- No verified hold record is available for this call. Do NOT " +
        "state specific hold counts or durations as verified fact."
    );
  } else if (ctx.holds.length) {
    const described = ctx.holds.map((h) => {
      const duration = mmss(h.seconds);
      if (anchor !== null) {
        const offset = mmss((Date.parse(h.started_at) - Date.parse(anchor)) / 1000);
        return `${duration} at ${offset}`;
      }
      return duration;
    });
    const n = ctx.holds.length;
    lines.push(
      `- Verified hold record: ${n} hold${n !== 1 ? "s" : ""} ` +
        `(${described.join(", ")}). Do NOT infer or report holds beyond this record.`
    );
  } else if (ctx.total_hold_seconds > 0) {
    lines.push(
      `- Verified total hold time: ${mmss(ctx.total_hold_seconds)}. ` +
        "Do NOT infer additional holds beyond this total."
    );
  } else {
    lines.push(
      "- Verified: no holds occurred on this call. Do NOT report or " +
        "penalize hold time."
    );
  }

  if (ctx.ai_csat !== null) {
    const csat = String(ctx.ai_csat).replace(/\.0$/, "");
    lines.push(
      `- Dialpad Ai CSAT estimate for this call: ${csat}/5 ` +
        "(context only — do not let it anchor your section scores)."
    );
  }

  lines.push("");
  return lines.join("\n");
}
