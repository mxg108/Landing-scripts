// Provider selection (SofiaRetellSpec §2). R1: every team is dialpad — the
// teams.provider column, provider_config, and retell.ts arrive with
// migration 0005 in R2.

import { makeDialpadProvider } from "./dialpad.js";
import type { CallProvider } from "./types.js";

export type {
  AudioSource,
  CallGrounding,
  CallProvider,
  DisplayLine,
  NormalizedCall,
} from "./types.js";

export function getProvider(
  providerId: string | null | undefined,
  secrets: { DIALPAD_API_KEY?: string; RETELL_API_KEY?: string }
): CallProvider {
  const id = providerId || "dialpad";
  if (id === "dialpad") {
    if (!secrets.DIALPAD_API_KEY)
      throw new Error("DIALPAD_API_KEY app secret not configured");
    return makeDialpadProvider(secrets.DIALPAD_API_KEY);
  }
  throw new Error(`unknown call provider ${id}`);
}
