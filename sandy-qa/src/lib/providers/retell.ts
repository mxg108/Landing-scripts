// Retell provider — Sofia AI's calls (SofiaRetellSpec §2/§4, RetellAPI.md).
// GET /v2/get-call/{call_id}; unlike dialpad (empty-on-fail), Retell failures
// throw ProviderCallError so the console shows the real reason (not-found,
// not-ended, voicemail, no recording) instead of scoring an empty call.
//
// Retell-only exploitation (the point of the slice):
//   - transcript_object word timings → deterministic response-gap /
//     interruption / silence / talk-ratio markers, a SYSTEM-VERIFIED tier
//     Dialpad's "moments" never had (§4.1)
//   - latency percentiles → objective human-likeness evidence (§4.2)
//   - call_analysis → grounding block + SOP query (no dispositions) (§4.3)
//   - recording_multi_channel_url preferred → clean speaker separation
//   - public_log_url → review link; agent_version → per-build trend stamp

import { ProviderCallError, type CallProvider, type NormalizedCall } from "./types.js";

const RETELL = "https://api.retellai.com";

const label = (role: string) =>
  role === "agent" ? "Sofia" : role === "transfer_target" ? "Transfer" : "Caller";

// Retell word timings are SECONDS from call start (float).
const mmss = (sec: number | null | undefined): string => {
  if (sec === null || sec === undefined || !Number.isFinite(sec)) return "";
  const t = Math.trunc(Math.max(0, sec));
  return `${Math.trunc(t / 60)}:${String(t % 60).padStart(2, "0")}`;
};

const pct = (sorted: number[], p: number): number =>
  sorted.length ? sorted[Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1))] : 0;

interface Turn {
  role: string;
  content: string;
  start: number | null;
  end: number | null;
}

function toTurns(transcriptObject: any[]): Turn[] {
  return (transcriptObject ?? []).map((u: any) => {
    const words = u.words ?? [];
    return {
      role: u.role ?? "user",
      content: (u.content ?? "").trim(),
      start: Number.isFinite(words[0]?.start) ? words[0].start : null,
      end: Number.isFinite(words[words.length - 1]?.end) ? words[words.length - 1].end : null,
    };
  });
}

// §4.1 — system-verified conversational-dynamics markers, computed from word
// timings (deterministic, unlike the annotator's observational hold guesses).
export function computeSignalMarkers(turns: Turn[], latency: any): any[] {
  const markers: any[] = [];

  const gaps: number[] = [];
  for (let i = 1; i < turns.length; i++) {
    const prev = turns[i - 1];
    const cur = turns[i];
    if (prev.end === null || cur.start === null) continue;
    const gapS = cur.start - prev.end;
    if (cur.role === "agent" && prev.role === "user" && gapS >= 0) {
      gaps.push(Math.round(gapS * 1000));
    }
    if (gapS < -0.1) {
      markers.push({
        type: "interruption",
        timestamp: mmss(cur.start),
        by: label(cur.role),
        overlap_ms: Math.round(-gapS * 1000),
      });
    } else if (gapS > 3) {
      markers.push({
        type: "silence",
        from: mmss(prev.end),
        to: mmss(cur.start),
        seconds: Math.round(gapS * 10) / 10,
      });
    }
  }
  if (gaps.length) {
    const sorted = [...gaps].sort((a, b) => a - b);
    markers.unshift({
      type: "sofia_response_gap_stats",
      note: "measured gap between the caller finishing and Sofia starting to speak",
      samples: sorted.length,
      p50_ms: pct(sorted, 50),
      p95_ms: pct(sorted, 95),
      min_ms: sorted[0],
      max_ms: sorted[sorted.length - 1],
    });
  }

  let agentMs = 0;
  let callerMs = 0;
  for (const t of turns) {
    if (t.start === null || t.end === null) continue;
    const dur = Math.max(0, t.end - t.start) * 1000;
    if (t.role === "agent") agentMs += dur;
    else if (t.role === "user") callerMs += dur;
  }
  if (agentMs + callerMs > 0) {
    markers.push({
      type: "talk_ratio",
      sofia_pct: Math.round((agentMs / (agentMs + callerMs)) * 100),
      caller_pct: Math.round((callerMs / (agentMs + callerMs)) * 100),
    });
  }

  const e2e = latency?.e2e;
  if (e2e && Number.isFinite(e2e.p50)) {
    markers.push({
      type: "retell_latency_ms",
      note: "platform-measured Sofia response latency (end of caller speech to first audio)",
      e2e: { p50: e2e.p50, p90: e2e.p90 ?? null, p95: e2e.p95 ?? null, max: e2e.max ?? null },
    });
  }
  return markers;
}

const compactJson = (obj: Record<string, unknown>): string | null => {
  const entries = Object.entries(obj).filter(
    ([, v]) => v && typeof v === "object" && Object.keys(v as object).length
  );
  if (!entries.length) return null;
  return JSON.stringify(Object.fromEntries(entries)).slice(0, 2000);
};

// §4.3 — the CC/disposition-grounding analog for a team with no CC rows.
function buildContextBlock(raw: any, markers: any[]): string {
  const ca = raw.call_analysis ?? {};
  const lines = ["CALL PLATFORM ANALYSIS (Retell post-call, informational — verify against the audio):"];
  if (ca.call_summary) lines.push(`- Summary: ${ca.call_summary}`);
  lines.push(
    `- Caller sentiment: ${ca.user_sentiment ?? "Unknown"} | Call successful: ${ca.call_successful ?? "unknown"}`
  );
  if (raw.disconnection_reason) lines.push(`- Disconnection: ${raw.disconnection_reason}`);
  if (raw.agent_version !== undefined) lines.push(`- Sofia build: agent_version ${raw.agent_version}`);
  const dyn = compactJson({
    metadata: raw.metadata,
    given: raw.retell_llm_dynamic_variables,
    collected: raw.collected_dynamic_variables,
  });
  if (dyn) lines.push(`- Dynamic context given/collected: ${dyn}`);
  const gap = markers.find((m) => m.type === "sofia_response_gap_stats");
  if (gap)
    lines.push(
      `- Measured response gaps: p50 ${gap.p50_ms}ms / p95 ${gap.p95_ms}ms over ${gap.samples} turns (see PLATFORM SIGNAL MARKERS)`
    );
  return lines.join("\n");
}

export function makeRetellProvider(key: string): CallProvider {
  return {
    id: "retell",
    markers_header: "PLATFORM SIGNAL MARKERS (system-verified measurements from Retell word timings)",
    async fetchCall(callId: string): Promise<NormalizedCall> {
      const res = await fetch(`${RETELL}/v2/get-call/${encodeURIComponent(callId)}`, {
        headers: { authorization: `Bearer ${key}` },
      });
      if (res.status === 401)
        throw new ProviderCallError("Retell auth failed — check the RETELL_API_KEY app secret", 503);
      if (res.status === 404 || res.status === 422)
        throw new ProviderCallError(`Retell call ${callId} not found`, 422);
      if (!res.ok) throw new ProviderCallError(`Retell get-call HTTP ${res.status}`, 502);
      const raw = (await res.json()) as any;

      if (raw.call_status !== "ended")
        throw new ProviderCallError(
          `call ${callId} is '${raw.call_status}' — only ended calls can be scored`, 422);
      if (raw.call_analysis?.in_voicemail === true)
        throw new ProviderCallError(`call ${callId} reached voicemail — not scoreable`, 422);
      const recordingUrl = raw.recording_multi_channel_url || raw.recording_url;
      if (!recordingUrl)
        throw new ProviderCallError(`no recording available for call ${callId}`, 422);

      const turns = toTurns(raw.transcript_object ?? []);
      const markers = computeSignalMarkers(turns, raw.latency);
      const transcriptDisplay = turns
        .filter((t) => t.content)
        .map((t) => ({ timestamp: mmss(t.start), speaker: label(t.role), text: t.content }));

      const durationMs = Number(
        raw.duration_ms ??
          (raw.end_timestamp && raw.start_timestamp ? raw.end_timestamp - raw.start_timestamp : 0)
      );
      // Phone calls: the human is from_number on inbound, to_number outbound.
      const callerPhone =
        raw.direction === "outbound" ? raw.to_number ?? "" : raw.from_number ?? "";

      return {
        call_id: callId,
        transcript_text: transcriptDisplay.map((l) => `${l.speaker}: ${l.text}`).join("\n"),
        transcript_display: transcriptDisplay,
        moments_display: markers,
        caller_name: "",
        caller_phone: String(callerPhone),
        connected_at: raw.start_timestamp ? new Date(raw.start_timestamp).toISOString() : null,
        ended_at: raw.end_timestamp ? new Date(raw.end_timestamp).toISOString() : null,
        duration_ms: Number.isFinite(durationMs) ? durationMs : 0,
        entry_point_call_id: null,
        master_call_id: null,
        review_link: raw.public_log_url ?? "",
        was_recorded: true,
        // Fresh 24h-signed URL fetched at trigger time; the workflow only
        // downloads it — no Retell key exists workflow-side (§5).
        audio: { source: "url", url: recordingUrl, mime: "audio/wav" },
        grounding: {
          context_block: buildContextBlock(raw, markers),
          sop_query: raw.call_analysis?.call_summary ?? null,
          stamps: {
            provider: "retell",
            agent_id: raw.agent_id ?? null,
            agent_version: raw.agent_version ?? null,
            call_status: raw.call_status,
            direction: raw.direction ?? null,
            disconnection_reason: raw.disconnection_reason ?? null,
            user_sentiment: raw.call_analysis?.user_sentiment ?? null,
            call_successful: raw.call_analysis?.call_successful ?? null,
            in_voicemail: raw.call_analysis?.in_voicemail ?? null,
            latency_e2e_ms: raw.latency?.e2e
              ? {
                  p50: raw.latency.e2e.p50 ?? null,
                  p90: raw.latency.e2e.p90 ?? null,
                  p95: raw.latency.e2e.p95 ?? null,
                  max: raw.latency.e2e.max ?? null,
                }
              : null,
          },
        },
        agent_version: raw.agent_version !== undefined ? String(raw.agent_version) : null,
      };
    },
  };
}
