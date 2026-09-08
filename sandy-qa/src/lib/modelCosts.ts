// Per-eval API cost estimation from the pipeline result's usage stamps.
// Workflow v0.4 captures judge usage (anthropic `usage` / gemini diag) and
// the Gemini prompt modality split; older results carry only annotate_diag
// and estimate audio-vs-text by omission (audio tokens unknown → billed at
// the cheaper text rate, i.e. a LOWER bound — the audio split closes it).
//
// Price constants, standard tier, verified 2026-09-07:
// - Gemini: ai.google.dev/gemini-api/docs/pricing (2.5 Flash: $0.30/M
//   text-image-video input, $1.00/M audio input, $2.50/M output INCLUDING
//   thinking tokens; Batch/Flex tier is half — a queue-latency-tolerant
//   lever for the cost work).
// - Anthropic: claude-sonnet-5 $2.00/M in, $10.00/M out; haiku-4.5 $1/$5.
// Update on vendor reprice; unknown models contribute nothing (null-safe).

interface Price {
  in: number;
  in_audio?: number;
  out: number;
}

export const MODEL_PRICES_PER_MTOK: Record<string, Price> = {
  "gemini-2.5-flash": { in: 0.3, in_audio: 1.0, out: 2.5 },
  "claude-sonnet-5": { in: 2.0, out: 10.0 },
  "claude-haiku-4-5": { in: 1.0, out: 5.0 },
};

const M = 1_000_000;

function geminiLegUsd(model: string | null | undefined, diag: any): number | null {
  const price = MODEL_PRICES_PER_MTOK[model ?? ""];
  if (!price || !diag) return null;
  const prompt = diag.prompt_tokens ?? 0;
  const audio = Math.min(diag.prompt_tokens_audio ?? 0, prompt);
  const out = (diag.output_tokens ?? 0) + (diag.thoughts_tokens ?? 0);
  if (!prompt && !out) return null;
  return (
    (audio * (price.in_audio ?? price.in)) / M +
    ((prompt - audio) * price.in) / M +
    (out * price.out) / M
  );
}

function anthropicLegUsd(model: string | null | undefined, usage: any): number | null {
  const price = MODEL_PRICES_PER_MTOK[model ?? ""];
  if (!price || !usage) return null;
  const input =
    (usage.input_tokens ?? 0) +
    (usage.cache_creation_input_tokens ?? 0) +
    (usage.cache_read_input_tokens ?? 0);
  const out = usage.output_tokens ?? 0;
  if (!input && !out) return null;
  // Cache-tier pricing (1.25x write / 0.1x read) deliberately ignored —
  // no cache_control on the judge yet; revisit with the caching lever.
  return (input * price.in) / M + (out * price.out) / M;
}

// `p` is the qa-scoring-pipeline callback result. Returns USD rounded to
// 5 decimals, or null when no leg carried any usage (pre-v0.4 runs).
export function estimateEvalCost(p: any): number | null {
  const legs = [
    geminiLegUsd(p?.annotator_model, p?.annotate_diag),
    p?.scorer_provider === "anthropic"
      ? anthropicLegUsd(p?.scorer_model, p?.judge_usage)
      : geminiLegUsd(p?.scorer_model, p?.judge_diag),
    geminiLegUsd(p?.single_diag ? p?.scorer_model : null, p?.single_diag),
  ].filter((v): v is number => v !== null);
  if (!legs.length) return null;
  return Math.round(legs.reduce((a, b) => a + b, 0) * 1e5) / 1e5;
}
