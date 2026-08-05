// Font asset registry + serving.
//
// The Landing woff2 files are bundled into the worker at build time as base64
// data URIs (Vite's `?inline` query), then served as real binary responses at
// /ds/fonts/<name>.woff2. colors_and_type.css references these URLs from its
// @font-face rules, so the browser fetches each font once and caches it — the
// HTML response stays small (vs. inlining ~350KB of base64 on every request).

// Eagerly import every woff2 as a base64 data URI string. Keys look like
// "./fonts/SaansRegular.woff2".
const fontModules = import.meta.glob("./fonts/*.woff2", {
  query: "?inline",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Map bare filename -> base64 data URI.
const fontsByName = new Map<string, string>();
for (const [path, dataUri] of Object.entries(fontModules)) {
  const name = path.split("/").pop()!;
  fontsByName.set(name, dataUri);
}

// Decode a `data:...;base64,<b64>` URI into raw bytes.
function dataUriToBytes(dataUri: string): Uint8Array {
  const b64 = dataUri.slice(dataUri.indexOf(",") + 1);
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

// Serve GET /ds/fonts/<name>.woff2. Returns null for unknown paths so the caller
// can fall through to its other routes.
export function serveFont(pathname: string): Response | null {
  const prefix = "/ds/fonts/";
  if (!pathname.startsWith(prefix)) return null;
  const name = pathname.slice(prefix.length);
  const dataUri = fontsByName.get(name);
  if (!dataUri) return new Response("Not found", { status: 404 });
  return new Response(dataUriToBytes(dataUri), {
    headers: {
      "Content-Type": "font/woff2",
      // Fonts are content-addressed by the design system; safe to cache hard.
      "Cache-Control": "public, max-age=31536000, immutable",
    },
  });
}
