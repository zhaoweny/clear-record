// Setup is system readiness (ADR-0027, ticket 04): a numbered, skippable
// sequence on /setup, and a dismissible update notice when the recorded version
// marker is stale. The seed records the current version; these tests rewrite it
// stale and restore it through the UI's own DISMISS and COMPLETE, so the seeded
// suite still lands on Projects.
import { test, expect } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { stateDir } from "./paths";

// The service's own record: <state>/agent-setup.json (SETUP_FILENAME).
const SETUP_FILE = resolve(stateDir, "agent-setup.json");

function state(): Record<string, unknown> {
  return existsSync(SETUP_FILE)
    ? (JSON.parse(readFileSync(SETUP_FILE, "utf-8")) as Record<string, unknown>)
    : {};
}

function seenVersion(): string {
  return String(state().seen_version);
}

/** Rewrite the marker to a deliberately stale version, preserving everything else. */
function writeMarker(version: string): void {
  mkdirSync(stateDir, { recursive: true });
  writeFileSync(
    SETUP_FILE,
    JSON.stringify({ ...state(), seen_version: version }, null, 2) + "\n",
    "utf-8",
  );
}

test("the setup page renders the numbered steps and the shared agent panel", async ({
  page,
}) => {
  await page.goto("/setup");

  await expect(page.locator(".setup-step")).toHaveCount(4);
  await expect(page.locator("#setup-welcome")).toContainText("Welcome");
  await expect(page.locator("#setup-transcription")).toContainText("Transcription");
  // Try it points at the one acceptance test (the flow's Try it stage)
  // and the permanent copy in Settings -> Status; it is not a second tape flow.
  await expect(page.locator("#setup-try")).toContainText("Run it in the Agent step");
  await expect(page.locator('#setup-try a[href="/settings/status"]')).toBeVisible();
  // The agent step is the one panel Settings -> Agent mounts, loaded by htmx.
  await expect(page.locator("#setup-agent #agent-setup .agent-setup")).toBeVisible();
  await expect(page.locator("#setup-agent .agent-flow .agent-stage")).toHaveCount(3);
});

test("a stale marker shows the update notice and the nav Setup link", async ({
  page,
}) => {
  const current = seenVersion();
  writeMarker("0.0.0-stale");

  await page.goto("/");
  const notice = page.locator(".setup-notice");
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("updated to");
  // The notice links to the update reason, and the nav link follows the marker.
  await expect(notice.locator('a[href="/setup?reason=update"]')).toBeVisible();
  await expect(page.locator(".app-nav a", { hasText: "Setup" })).toBeVisible();

  // Dismissing records the current version, so neither comes back on reload.
  await notice.getByRole("button", { name: "Dismiss" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator(".setup-notice")).toHaveCount(0);
  await expect(page.locator(".app-nav a", { hasText: "Setup" })).toHaveCount(0);
  expect(seenVersion()).toBe(current);
});

test("the update entry point shows its copy and COMPLETE records the version", async ({
  page,
}) => {
  const current = seenVersion();
  writeMarker("0.0.0-stale");

  await page.goto("/setup?reason=update");

  await expect(page.locator(".setup-update")).toBeVisible();
  await expect(page.locator(".setup-update")).toContainText("was updated to");
  // The page carries the update copy, not the band that links to itself.
  await expect(page.locator(".setup-notice")).toHaveCount(0);

  await page.getByRole("button", { name: "Finish setup" }).click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator(".app-nav a", { hasText: "Setup" })).toHaveCount(0);
  expect(seenVersion()).toBe(current);
});
