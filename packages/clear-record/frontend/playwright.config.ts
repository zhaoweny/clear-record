// Playwright e2e for the console.
//
// The suite drives the *real* server: `webServer` boots `clear-record web`
// against a data directory seeded by `e2e/seed.py` (the `just e2e` recipe runs
// the seed first), so the tests exercise the same htmx 4 + Jinja surface a
// person uses — no mocks, no mounts.
//
// Screenshots are the visual-inspection artefact: `visual.spec.ts` and
// `gallery.spec.ts` write light/dark and desktop/mobile captures to E2E_SHOTS
// (default `.local/e2e/screenshots`), which is gitignored.
import { defineConfig, devices } from "@playwright/test";
import { resolve } from "node:path";

import { baseURL, dataDir, port, repoRoot, sessionStatePath } from "./e2e/paths";

export default defineConfig({
  testDir: "./e2e",
  outputDir: resolve(repoRoot, ".local/e2e/test-results"),
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  reporter: [["list"]],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  // The seed leaves a process holding one run in flight for the whole suite
  // (RUN-03); this ends it after the last test.
  globalTeardown: "./e2e/teardown.ts",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    // The console has a credential (ADR-0033), so every spec starts from the
    // session `seed.py` minted and wrote beside the data directory: the console
    // answers a page or an API call only to a live session. A spec that needs
    // the gate's own states drives them in the unit suite, not here.
    storageState: sessionStatePath,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `uv run --all-packages clear-record web --no-browser --host 127.0.0.1 --port ${port}`,
    cwd: repoRoot,
    // Readiness is the liveness route, not the origin: nothing answers at the
    // root any more (the console is under `/web/`), and `/health` is the one
    // anonymous 200 that needs no session.
    url: `${baseURL}/health`,
    reuseExistingServer: false,
    timeout: 120_000,
    // Fully isolate the run: without these the server adopts the operator's
    // real state/log/cache dirs and reads their models, which is neither
    // hermetic nor quiet.
    env: {
      ...process.env,
      CR_DATA_DIR: dataDir,
      CR_STATE_DIR: resolve(repoRoot, ".local/e2e/state"),
      CR_LOG_DIR: resolve(repoRoot, ".local/e2e/logs"),
      CR_CACHE_DIR: resolve(repoRoot, ".local/e2e/cache"),
      CR_MODELS_DIR: resolve(repoRoot, ".local/e2e/models"),
    } as Record<string, string>,
  },
});
