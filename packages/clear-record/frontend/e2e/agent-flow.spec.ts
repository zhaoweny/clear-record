// Ticket 05: the reusable agent flow and its hello-world acceptance test.
//
// The check's contract is "a transcript, or a finding that names the leg", so the
// test accepts EITHER: a machine with a working voice and an ASR backend reaches
// "ok" (as a full-access macOS does), a bare CI box lands on a leg-naming finding.
// Neither outcome is a crash and neither makes a model call -- the ASR is local.
// A real agent answer is the separate optional just agent-drive workflow.
import { test, expect, type Page } from "@playwright/test";

function watch(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push("console: " + m.text());
  });
  return errors;
}

// Every outcome the check may report: "ok", or the leg that stopped the chain.
const LEGS = ["ok", "tts", "backend", "model", "transcribe"];

test("the Try it check reports a transcript or a leg-naming finding", async ({ page }) => {
  const errors = watch(page);

  await page.goto("/settings/status");
  await expect(page.locator("#hello-check")).toBeVisible();
  await page.getByRole("button", { name: "Run the check" }).click();

  const result = page.locator("#hello-check .hello-result");
  await expect(result).toBeVisible();
  const leg = await result.getAttribute("data-leg");
  expect(LEGS).toContain(leg);
  if (leg === "ok") {
    // A machine with a voice and an ASR backend runs the whole chain here.
    await expect(result.locator(".hello-transcript")).toBeVisible();
  } else {
    await expect(result).toHaveClass(/hello-finding/);
    // The finding names the leg in the copy too, not only in the attribute.
    await expect(result.locator(".hello-leg")).toContainText(String(leg));
  }
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
  expect(LEGS).toContain(leg);
  expect(errors).toEqual([]);
});

test("the setup wizard's Agent step mounts the same four-stage flow", async ({ page }) => {
  await page.goto("/setup");

  await expect(page.locator("#setup-agent .agent-flow .agent-stage")).toHaveCount(4);
  await expect(page.locator("#setup-agent #hello-check")).toBeVisible();
  // The final step points at the permanent check, not a second implementation.
  await expect(page.locator('#setup-try a[href="/settings/status"]')).toBeVisible();
});
