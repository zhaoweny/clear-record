// Ticket 05: the reusable agent flow and its hello-world acceptance test.
//
// e2e stays offline and deterministic. This machine has no working system voice
// (macOS say exits 0 and writes a zero-frame WAV) and no ASR backend, so
// "Try it" must land in a FINDING that names the leg -- never a crash and never
// a real model call. The success path is asserted by the Python tests with the
// pipeline stubbed; a real agent answer is the separate optional just
// agent-drive workflow (ticket 07).
import { test, expect, type Page } from "@playwright/test";

function watch(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push("console: " + m.text());
  });
  return errors;
}

// Every leg a finding may name. A success here would mean this environment grew
// a voice and an ASR backend, which is exactly what the test must not assume.
const FINDING_LEGS = ["tts", "backend", "model", "transcribe"];

test("the Try it check reports a finding that names the leg", async ({ page }) => {
  const errors = watch(page);

  await page.goto("/settings/status");
  await expect(page.locator("#hello-check")).toBeVisible();
  await page.getByRole("button", { name: "Run the check" }).click();

  const result = page.locator("#hello-check .hello-result");
  await expect(result).toBeVisible();
  await expect(result).toHaveClass(/hello-finding/);
  const leg = await result.getAttribute("data-leg");
  expect(FINDING_LEGS).toContain(leg);
  // The finding names the leg in the copy too, not only in the attribute.
  await expect(result.locator(".hello-leg")).toContainText(String(leg));
  expect(errors).toEqual([]);
});

test("the same check runs from the agent flow's Try it stage", async ({ page }) => {
  const errors = watch(page);

  await page.goto("/settings/agent");
  const stage = page.locator("#agent-stage-try");
  await expect(stage.locator("#hello-check")).toBeVisible();
  await stage.getByRole("button", { name: "Run the check" }).click();

  const result = stage.locator("#hello-check .hello-result");
  await expect(result).toBeVisible();
  const leg = await result.getAttribute("data-leg");
  expect(FINDING_LEGS).toContain(leg);
  expect(errors).toEqual([]);
});

test("the setup wizard's Agent step mounts the same four-stage flow", async ({ page }) => {
  await page.goto("/setup");

  await expect(page.locator("#setup-agent .agent-flow .agent-stage")).toHaveCount(4);
  await expect(page.locator("#setup-agent #hello-check")).toBeVisible();
  // The final step points at the permanent check, not a second implementation.
  await expect(page.locator('#setup-first-record a[href="/settings/status"]')).toBeVisible();
});
