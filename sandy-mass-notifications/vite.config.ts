import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite config for building a Cloudflare Worker that serves a React SPA.
// Output: dist/worker.js — a single ES module Cloudflare Worker script.
export default defineConfig({
  plugins: [react()],
  build: {
    // Build as a Cloudflare Worker module
    lib: {
      entry: "src/index.tsx",
      name: "worker",
      fileName: "worker",
      formats: ["es"],
    },
    rollupOptions: {
      // Cloudflare Workers (ie. Sandy App) runtime modules — not bundled, resolved by the CF runtime at deploy time
      external: [/^cloudflare:/, /^node:/],
      output: {
        entryFileNames: "worker.js",
      },
    },
    minify: true,
    target: "es2022",
  },
});
