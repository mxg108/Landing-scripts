// Provider selection (SofiaRetellSpec §2), keyed off teams.provider
// (migration 0005; absent/legacy rows default dialpad).

import { makeDialpadProvider } from "./dialpad.js";
import { makeRetellProvider } from "./retell.js";
import type { CallProvider } from "./types.js";

export { ProviderCallError } from "./types.js";
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
  if (id === "retell") {
    if (!secrets.RETELL_API_KEY)
      throw new Error("RETELL_API_KEY app secret not configured");
    return makeRetellProvider(secrets.RETELL_API_KEY);
  }
  throw new Error(`unknown call provider ${id}`);
}
