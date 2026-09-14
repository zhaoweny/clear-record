// Build config for the console's compiled assets.
//
// Vite is the bundler (ADR-0023): it is the SPA-shaped default, so the owner's
// stated reason for a real toolchain — "the path to some more complicated SPA
// and web apps" — stays open. Today it bundles exactly what the console needs:
// htmx + Alpine as real npm dependencies (no hand-committed vendor files) and
// Tailwind v4 through its first-party Vite plugin.
//
// The output lands in the Python package's `static/` directory, which the wheel
// already ships as package data. The compiled result is committed, so end users
// never need Node; `just web-assets-check` is the freshness guard.
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  root,
  // The console mounts its assets at /static (see web/app.py); this keeps any
  // URL the bundler writes into the output relative to that mount.
  base: "/static/",
  plugins: [tailwindcss()],
  build: {
    outDir: resolve(root, "../src/clear_record/web/static"),
    // The static directory contains only build output, so wiping it keeps a
    // removed source from leaving an orphaned asset behind.
    emptyOutDir: true,
    rollupOptions: {
      input: resolve(root, "src/main.js"),
      output: {
        // Stable, unhashed filenames: `base.html` references literal paths and
        // the wheel serves them straight out of the package. The console is a
        // localhost-only tool, so content-hash cache-busting buys nothing and
        // would need a manifest plus a Jinja helper. See frontend/README.md.
        entryFileNames: "app.js",
        chunkFileNames: "[name].js",
        assetFileNames: (assetInfo) => {
          const name = assetInfo.names?.[0] ?? assetInfo.name ?? "";
          return name.endsWith(".css") ? "app.css" : "assets/[name][extname]";
        },
      },
    },
  },
});
