// Shared paths for the e2e suite. Kept out of playwright.config so a spec can
// import it without pulling in the config's default export.
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../../..");

// The recipe passes repo-relative paths (".local/e2e/..."); the Playwright
// process runs from the frontend dir, so resolve them against the repo root
// here — and leave an absolute override untouched.
const under = (value: string | undefined, fallback: string): string =>
  !value ? fallback : isAbsolute(value) ? value : resolve(repoRoot, value);

export const shotsDir = under(
  process.env.E2E_SHOTS,
  resolve(repoRoot, ".local/e2e/screenshots"),
);
export const dataDir = under(
  process.env.CR_DATA_DIR,
  resolve(repoRoot, ".local/e2e/data"),
);
export const port = Number(process.env.E2E_PORT ?? 8973);
export const baseURL = `http://127.0.0.1:${port}`;
