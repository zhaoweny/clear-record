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
  await page.goto("/web/");
  await expect(page.locator("#projects .project").first()).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { htmx?: { version: string } }).htmx?.version)).toMatch(/^4\./);
  expect(await page.evaluate(() => (window as unknown as { Alpine?: { version: string } }).Alpine?.version)).toMatch(/^3\./);
  // The header carries real links; the agent panel moved to Settings -> Agent.
  await expect(page.locator(".app-nav a", { hasText: "Projects" })).toBeVisible();
  await expect(page.locator(".app-nav a", { hasText: "Settings" })).toBeVisible();
  expect(errors).toEqual([]);
});

test("a project opens on its own URL and the review is its own page", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/web/");
  await page.locator("#projects .project", { hasText: "Q3 sync" }).click();
  await expect(page).toHaveURL(/\/web\/projects\/q3-sync$/);
  await expect(page.locator("#detail h2")).toHaveText("Q3 sync");
  // The Meetings tab is a real link; the review is a real page under it.
  await page.locator(".project-tabs a", { hasText: "Meetings" }).click();
  await expect(page).toHaveURL(/\/web\/projects\/q3-sync\/meetings$/);
  // "Review" is a real link; match by text, not by role.
  await page.locator("#detail .meeting", { hasText: "Kickoff" }).getByText("Review", { exact: true }).click();
  await expect(page).toHaveURL(/\/web\/projects\/q3-sync\/meetings\/kickoff$/);
  // The meeting's own transcript; the minutes body is a separate element.
  await expect(page.locator("pre.transcript").first()).toContainText("the recorder was running");
  await expect(page.locator(".table-artifacts")).toContainText("record.json");
  expect(errors).toEqual([]);
});

test("creating a project adds it to the workspace", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/web/");
  // Unique per run: the suite deliberately reuses one seeded data dir.
  const name = `Design review ${Date.now()}`;
  const field = page.getByPlaceholder("New project name");
  await field.fill(name);
  await page.getByRole("button", { name: "Add project" }).click();
  await expect(page.locator("#projects .project", { hasText: name })).toBeVisible();
  // Only a real success signals the form to clear (HX-Trigger).
  await expect(field).toHaveValue("");
  expect(errors).toEqual([]);
});

test("a refused project name stays in the form", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/web/");
  const field = page.getByPlaceholder("New project name");
  await field.fill("   ");
  await page.getByRole("button", { name: "Add project" }).click();
  // The refusal re-renders #projects but carries no success signal, so the
  // form is not replaced and keeps the whitespace-only name.
  await expect(page.locator(".project-error")).toBeVisible();
  await expect(field).toHaveValue("   ");
  expect(errors).toEqual([]);
});

test("adding a glossary term renders it in the table", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/web/projects/q3-sync/glossary");
  await expect(page.locator(".project-tabs a[aria-current='page']")).toHaveText("Glossary");
  const term = `reconciliation loop ${Date.now()}`;
  await page.getByPlaceholder("Term", { exact: true }).fill(term);
  await page.getByRole("button", { name: "Add term" }).click();
  await expect(page.locator(".table-glossary")).toContainText(term);
  expect(errors).toEqual([]);
});

test("the language switch flips the document language", async ({ page }) => {
  await page.goto("/web/");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await page.locator(".lang-switch button", { hasText: "中文" }).click();
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
});

test("the profile picker previews the resolved knobs it will run", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/web/projects/q3-sync/meetings");
  const preview = page.locator(".profile-options").first();
  await expect(preview).toBeVisible();
  await expect(preview).toContainText("resolved knobs");
  expect(errors).toEqual([]);
});
