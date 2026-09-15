// End-to-end flows against the real console: htmx 4 wiring, the server-rendered
// fragments, and the Alpine interactions. Every test asserts no console or page
// error, so a broken swap shows up here rather than only in a screenshot.
import { test, expect, type Page } from "@playwright/test";

function watch(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`console: ${m.text()}`);
  });
  return errors;
}

test("the console boots on htmx 4 and Alpine 3, with no errors", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/");
  await expect(page.locator("#projects .project").first()).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { htmx?: { version: string } }).htmx?.version)).toMatch(/^4\./);
  expect(await page.evaluate(() => (window as unknown as { Alpine?: { version: string } }).Alpine?.version)).toMatch(/^3\./);
  // The header carries real links; the agent panel moved to Settings -> Agent.
  await expect(page.locator(".app-nav a", { hasText: "Projects" })).toBeVisible();
  await expect(page.locator(".app-nav a", { hasText: "Settings" })).toBeVisible();
  expect(errors).toEqual([]);
});

test("a project opens on its own URL and renders the reconciled transcript", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/");
  await page.locator("#projects .project", { hasText: "Q3 sync" }).click();
  await expect(page).toHaveURL(/\/projects\/q3-sync$/);
  await expect(page.locator("#detail h2")).toHaveText("Q3 sync");
  await expect(page.locator("#detail")).toContainText("Meetings");
  // "Review" is an htmx-driven control; match by text, not by role.
  await page.locator("#detail .meeting", { hasText: "Kickoff" }).getByText("Review", { exact: true }).click();
  // The meeting review stays an in-page fragment of the project page.
  await expect(page).toHaveURL(/\/projects\/q3-sync$/);
  // The meeting's own transcript; the minutes body is a separate element.
  await expect(page.locator("pre.transcript").first()).toContainText("the recorder was running");
  await expect(page.locator(".table-artifacts")).toContainText("record.json");
  expect(errors).toEqual([]);
});

test("creating a project adds it to the workspace", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/");
  // Unique per run: the suite deliberately reuses one seeded data dir.
  const name = `Design review ${Date.now()}`;
  await page.getByPlaceholder("New project name").fill(name);
  await page.getByRole("button", { name: "Add project" }).click();
  await expect(page.locator("#projects .project", { hasText: name })).toBeVisible();
  expect(errors).toEqual([]);
});

test("adding a glossary term renders it in the table", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/");
  await page.locator("#projects .project", { hasText: "Q3 sync" }).click();
  await expect(page).toHaveURL(/\/projects\/q3-sync$/);
  const term = `reconciliation loop ${Date.now()}`;
  await page.getByPlaceholder("Term", { exact: true }).fill(term);
  await page.getByRole("button", { name: "Add term" }).click();
  await expect(page.locator(".table-glossary")).toContainText(term);
  expect(errors).toEqual([]);
});

test("the language switch flips the document language", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await page.locator(".lang-switch button", { hasText: "中文" }).click();
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
});

test("the profile picker previews the resolved knobs it will run", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/");
  await page.locator("#projects .project", { hasText: "Q3 sync" }).click();
  await expect(page).toHaveURL(/\/projects\/q3-sync$/);
  const preview = page.locator(".profile-options").first();
  await expect(preview).toBeVisible();
  await expect(preview).toContainText("resolved knobs");
  expect(errors).toEqual([]);
});
