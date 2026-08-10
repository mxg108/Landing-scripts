// Scorecard email dispatch — port of the Railway finalize-email seam
// (backend/routes/scoring.py::_dispatch_finalize_email + the GAS trigger in
// services/sheets_service.py::trigger_apps_script).
//
// Railway POSTs { historyRowNumber } and the Apps Script reads the
// Analyst_History sheet row. Sandy-born evaluations never reach that sheet
// (the shadow sync is one-way into D1), so Sandy uses the GAS webapp's
// PAYLOAD MODE (qa-automation/src/Main.js): the full entry + progression
// history inline, built from D1 — which covers Railway-born AND Sandy-born
// evals, more completely than the sheet.
//
// Secrets (Dashboard-only, ≤20-char names): GAS_WEBAPP_URL_MS /
// GAS_WEBAPP_URL_SALES. Absent → {status:'skipped'} — setting the secret is
// the go-live switch for real agent-facing email.

const MAX_HISTORY = 5; // CONFIG.EMAIL.MAX_HISTORY in the GAS Branding.js

export type EmailDispatchResult = { status: string; message: string };

// Explicit per-team map — a team with no GAS webapp (sofia, any future team)
// must resolve to undefined → 'skipped', never fall into another team's
// inbox (the old sales-else-MS ternary would have emailed Sofia scorecards
// to the Member Support webapp).
export function gasUrlForTeam(
  gasUrls: { member_support?: string; sales?: string } | undefined,
  teamId: string
): string | undefined {
  if (!gasUrls) return undefined;
  if (teamId === "sales") return gasUrls.sales;
  if (teamId === "member_support") return gasUrls.member_support;
  return undefined;
}

// Mirrors sheets_projection._render_disposition — "Category — Sub".
function renderDisposition(ev: any): string {
  const category = ev.dialpad_disposition_category || "";
  const sub = ev.dialpad_disposition || "";
  if (category && sub) return `${category} — ${sub}`;
  return category || sub;
}

// Mirrors sheets_projection._render_sop_references — "SOP n: Title" lines,
// numbering matching the [SOP n] citations in the AI reasoning.
function renderSopReferences(metadata: any): string[] {
  const docs = metadata?.pulpo_docs;
  if (Array.isArray(docs) && docs.length) {
    return docs
      .map((d: any, i: number) => (d?.title ? `SOP ${i + 1}: ${d.title}` : ""))
      .filter(Boolean);
  }
  return metadata?.sop_used ? [String(metadata.sop_used)] : [];
}

function buildEntry(ev: any, sections: any[]): any {
  const numericScores: Record<string, number | null> = {};
  const binaryChecks: Record<string, boolean | null> = {};
  const aiReasoning: Record<string, string> = {};
  const aiConfidence: Record<string, string> = {};
  for (const s of sections) {
    const isNa = s.binary_value === "NA";
    numericScores[s.section_id] = isNa ? null : (s.numeric_score ?? 0);
    binaryChecks[s.section_id] = isNa
      ? null
      : s.binary_value === null
        ? false
        : String(s.binary_value).toUpperCase().startsWith("Y");
    aiReasoning[s.section_id] = s.reasoning ?? "";
    aiConfidence[s.section_id] = String(s.confidence ?? "").toLowerCase();
  }
  let metadata: any = {};
  try {
    metadata =
      typeof ev.dialpad_call_metadata === "string"
        ? JSON.parse(ev.dialpad_call_metadata)
        : ev.dialpad_call_metadata ?? {};
  } catch {}
  return {
    agentName: ev.agent_name_raw ?? "",
    agentEmail: ev.agent_email ?? "",
    managerEmail: ev.evaluator_email ?? "",
    dialpadLink: ev.dialpad_link ?? "",
    timestamp: ev.call_connected_at ?? ev.created_at ?? new Date().toISOString(),
    overallScore: ev.overall_score ?? 0,
    numericScores,
    binaryChecks,
    aiReasoning,
    aiConfidence,
    strengths: ev.key_strengths ?? "",
    improvements: ev.opportunities ?? "",
    callSummary: ev.call_summary ?? "",
    callerName: ev.caller_name ?? "",
    callerPhone: ev.caller_phone ?? "",
    disposition: renderDisposition(ev),
    aiCsat: ev.ai_csat != null ? String(ev.ai_csat) : "",
    sopReferences: renderSopReferences(metadata),
  };
}

// AnalystHistory.getHistory contract: newest-first, the CURRENT call
// included as the most-recent entry (we query after the eval is persisted,
// so it lands there naturally). ProgressionCard consumes timestamp +
// overallScore; source rides along for parity with the sheet reader.
async function fetchPastEntries(
  db: D1Database,
  teamId: string,
  agentNameRaw: string
): Promise<any[]> {
  const rows = await db
    .prepare(
      `SELECT call_connected_at, created_at, overall_score, source
       FROM qa_evaluations
       WHERE team_id = ? AND agent_name_raw = ? AND state = 'finalized'
         AND overall_score IS NOT NULL
       ORDER BY COALESCE(call_connected_at, created_at) DESC
       LIMIT ?`
    )
    .bind(teamId, agentNameRaw, MAX_HISTORY)
    .all<any>();
  return rows.results.map((r) => ({
    timestamp: r.call_connected_at ?? r.created_at,
    overallScore: r.overall_score,
    source: r.source ?? "",
  }));
}

// The one entry point actions call: re-reads the finalized evaluation +
// sections from D1 (so callers dispatch AFTER their writes commit), builds
// the payload, POSTs to the team's GAS webapp. Never throws — the result
// object is the caller's visible receipt (Railway's job.email_dispatch
// parity: ok | skipped | suppressed | error).
export async function dispatchScorecardEmail(
  db: D1Database,
  teamId: string,
  evaluationId: number,
  gasUrl: string | undefined,
  disclaimer?: string | null
): Promise<EmailDispatchResult> {
  if (!gasUrl) {
    const secretName =
      teamId === "sales"
        ? "GAS_WEBAPP_URL_SALES"
        : teamId === "member_support"
          ? "GAS_WEBAPP_URL_MS"
          : null;
    return {
      status: "skipped",
      message: secretName
        ? `${secretName} app secret not configured`
        : `no GAS webapp for team ${teamId} — email not applicable`,
    };
  }
  try {
    const ev = await db
      .prepare("SELECT * FROM qa_evaluations WHERE id = ?")
      .bind(evaluationId)
      .first<any>();
    if (!ev) return { status: "error", message: `evaluation ${evaluationId} not found` };
    const sections = await db
      .prepare(
        "SELECT section_id, numeric_score, binary_value, confidence, reasoning FROM qa_evaluation_sections WHERE evaluation_id = ? ORDER BY section_number"
      )
      .bind(evaluationId)
      .all<any>();
    const body: any = {
      entry: buildEntry(ev, sections.results),
      pastEntries: await fetchPastEntries(db, teamId, ev.agent_name_raw ?? ""),
    };
    if (disclaimer) body.disclaimer = disclaimer;

    // GAS /exec answers via a 302 to googleusercontent — follow it.
    const res = await fetch(gasUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      redirect: "follow",
      signal: AbortSignal.timeout(60_000),
    });
    const text = await res.text();
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed.status === "string") return parsed;
      return { status: "error", message: `unexpected GAS response: ${text.slice(0, 200)}` };
    } catch {
      // GAS surfaces script errors as an HTML page with HTTP 200.
      return {
        status: "error",
        message: `GAS webapp returned non-JSON (HTTP ${res.status}): ${text.slice(0, 200)}`,
      };
    }
  } catch (err) {
    return { status: "error", message: String((err as any)?.message ?? err).slice(0, 300) };
  }
}
