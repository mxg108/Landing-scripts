// Call-provider seam (SofiaRetellSpec §2) — every scored call reaches the
// scoring path as a NormalizedCall regardless of which telephony platform
// recorded it. dialpad.ts is a behavior-preserving extract of the prefetch
// that lived at the top of routes/scoring.ts; retell.ts lands in R2.

export interface DisplayLine {
  timestamp: string;
  speaker: string;
  text: string;
}

export type AudioSource =
  // The workflow's audio leg fetches meta + recording via DIALPAD_API_KEY.
  | { source: "dialpad" }
  // Pre-signed URL fetched fresh app-side at trigger time (Retell WAV);
  // the workflow just downloads it — no provider secret workflow-side.
  | { source: "url"; url: string; mime: string };

export interface CallGrounding {
  // Prompt block replacing the CC/disposition grounding for teams with no
  // command-center rows (Retell: call_analysis + latency + dynamic vars).
  context_block: string | null;
  // SOP retrieval query when no disposition exists (Retell: call_summary).
  sop_query: string | null;
  // Provider stamps persisted into dialpad_call_metadata.
  stamps: Record<string, unknown>;
}

export interface NormalizedCall {
  call_id: string;
  transcript_text: string;
  transcript_display: DisplayLine[];
  moments_display: any[];
  caller_name: string;
  caller_phone: string;
  connected_at: string | null; // ISO
  ended_at: string | null; // ISO
  duration_ms: number;
  entry_point_call_id: string | null; // dialpad only
  master_call_id: string | null; // dialpad only
  review_link: string; // dialpad callreview URL | retell public_log_url
  was_recorded: boolean;
  audio: AudioSource;
  grounding: CallGrounding | null; // null ⇒ dialpad: fetchCallContext path
  agent_version: string | null; // retell only — Sofia build stamp
}

// Thrown by providers whose failures carry a user-actionable reason (Retell:
// not found / not ended / voicemail / no recording). The scoring trigger maps
// `status` onto the HTTP response. Dialpad keeps its empty-on-fail contract.
export class ProviderCallError extends Error {
  constructor(
    message: string,
    public status: number = 422
  ) {
    super(message);
    this.name = "ProviderCallError";
  }
}

export interface CallProvider {
  id: "dialpad" | "retell";
  // Header for the annotator's machine-detected markers block. The dialpad
  // wording predates the seam and must stay byte-identical — prompt drift
  // shifts MS/Sales scores.
  markers_header: string;
  fetchCall(callId: string): Promise<NormalizedCall>;
}
