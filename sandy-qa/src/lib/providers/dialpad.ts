// Dialpad provider — verbatim extract of the prefetch that lived at the top
// of routes/scoring.ts (ports of Railway's get_transcript/get_call_details).
// R1's gate is a byte-identical MS scorecard: error semantics (empty-on-fail,
// never throw), field defaults, and the callreview link construction must
// not drift.

import type { CallProvider, NormalizedCall } from "./types.js";

const DP = "https://dialpad.com/api/v2";

function mmssFrom(callStart: number | null, tsRaw: string): string {
  if (!tsRaw || callStart === null) return "";
  const t = Date.parse(tsRaw);
  if (Number.isNaN(t)) return "";
  const elapsed = Math.trunc((t - callStart) / 1000);
  return `${Math.trunc(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")}`;
}

async function getTranscript(key: string, callId: string) {
  const empty = { transcript_text: "", transcript_display: [] as any[], moments_display: [] as any[] };
  try {
    const r = await fetch(`${DP}/transcripts/${encodeURIComponent(callId)}`, {
      headers: { authorization: `Bearer ${key}` },
    });
    if (!r.ok) return empty;
    const data = (await r.json()) as any;
    const transcriptLines: string[] = [];
    const transcriptDisplay: any[] = [];
    const momentsDisplay: any[] = [];
    let callStart: number | null = null;
    for (const line of data.lines ?? []) {
      const tsRaw = line.time ?? "";
      if (line.type === "transcript") {
        const name = line.name ?? "Unknown";
        const content = (line.content ?? "").trim();
        if (!content) continue;
        if (tsRaw && callStart === null) {
          const t = Date.parse(tsRaw);
          if (!Number.isNaN(t)) callStart = t;
        }
        transcriptLines.push(`${name}: ${content}`);
        transcriptDisplay.push({
          timestamp: mmssFrom(callStart, tsRaw),
          speaker: name,
          text: content,
        });
      } else if (["moment", "real_time_moment", "custom_moment"].includes(line.type)) {
        const momentType = line.moment_type || (line.content ?? "").trim();
        if (!momentType) continue;
        momentsDisplay.push({
          timestamp: mmssFrom(callStart, tsRaw),
          time: tsRaw,
          type: momentType,
          agent: line.name ?? "",
        });
      }
    }
    return {
      transcript_text: transcriptLines.join("\n"),
      transcript_display: transcriptDisplay,
      moments_display: momentsDisplay,
    };
  } catch {
    return empty;
  }
}

async function getCallDetails(key: string, callId: string) {
  try {
    const r = await fetch(`${DP}/call/${encodeURIComponent(callId)}`, {
      headers: { authorization: `Bearer ${key}` },
    });
    if (!r.ok) return {} as any;
    const raw = (await r.json()) as any;
    const contact = raw.contact ?? {};
    return {
      caller_name: contact.name ?? "",
      caller_phone: contact.phone ?? "",
      caller_email: contact.email ?? "",
      date_connected: raw.date_connected ?? "",
      date_ended: raw.date_ended ?? "",
      total_duration: raw.total_duration ?? raw.duration ?? 0,
      entry_point_call_id: String(raw.entry_point_call_id ?? ""),
      master_call_id: String(raw.master_call_id ?? ""),
      was_recorded: !!raw.recording_details,
      raw,
    } as any;
  } catch {
    return {} as any;
  }
}

const epochToIso = (v: any): string | null => {
  const n = Number(v);
  return v && Number.isFinite(n) ? new Date(n).toISOString() : null;
};

export function makeDialpadProvider(key: string): CallProvider {
  return {
    id: "dialpad",
    markers_header: "DIALPAD SIGNAL MARKERS (hints, machine-detected)",
    async fetchCall(callId: string): Promise<NormalizedCall> {
      const [transcript, details] = await Promise.all([
        getTranscript(key, callId),
        getCallDetails(key, callId),
      ]);
      const entryPoint = details.entry_point_call_id || null;
      return {
        call_id: callId,
        transcript_text: transcript.transcript_text,
        transcript_display: transcript.transcript_display,
        moments_display: transcript.moments_display,
        caller_name: details.caller_name ?? "",
        caller_phone: details.caller_phone ?? "",
        connected_at: epochToIso(details.date_connected),
        ended_at: epochToIso(details.date_ended),
        duration_ms: Number(details.total_duration ?? 0),
        entry_point_call_id: entryPoint,
        master_call_id: details.master_call_id || null,
        review_link: `https://dialpad.com/callhistory/callreview/${entryPoint || callId}`,
        was_recorded: !!details.was_recorded,
        audio: { source: "dialpad" },
        grounding: null,
        agent_version: null,
      };
    },
  };
}
