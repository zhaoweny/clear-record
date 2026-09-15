// The project page's sub-tabs (ticket 02): each is a real, deep-linkable URL,
// a boosted `<a href>` marks the active tab with `aria-current` (never colour
// alone), and the Media tab is the read-only tape/transcript inventory.
import { test, expect, type Page } from "@playwright/test";

function watch(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`console: ${m.text()}`);
  });
  return errors;
}

test("each project tab is a real, deep-linkable URL", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/projects/q3-sync");
  await expect(page.locator(".project-tabs a[aria-current='page']")).toHaveText("Overview");

  // Deep link straight to a tab, then refresh: the URL keeps the choice.
  await page.goto("/projects/q3-sync/media");
  await expect(page.locator(".project-tabs a[aria-current='page']")).toHaveText("Media");
  await page.reload();
  await expect(page.locator(".project-tabs a[aria-current='page']")).toHaveText("Media");
  await expect(page.locator(".media-meeting", { hasText: "Kickoff" })).toBeVisible();

  // A click is a real (boosted) navigation to the tab's URL.
  await page.locator(".project-tabs a", { hasText: "Glossary" }).click();
  await expect(page).toHaveURL(/\/projects\/q3-sync\/glossary$/);
  await expect(page.locator(".project-tabs a[aria-current='page']")).toHaveText("Glossary");
  expect(errors).toEqual([]);
});

test("the meetings tab keeps the run form and the review links", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/projects/q3-sync/meetings");
  const kickoff = page.locator("#detail .meeting", { hasText: "Kickoff" });
  await expect(kickoff.locator(".run-form")).toBeVisible();
  await expect(kickoff.locator(".profile-options")).toContainText("resolved knobs");
  await kickoff.getByText("Review", { exact: true }).click();
  await expect(page).toHaveURL(/\/projects\/q3-sync\/meetings\/kickoff$/);
  expect(errors).toEqual([]);
});

test("the media tab inventories tapes and transcripts", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/projects/q3-sync/media");
  const kickoff = page.locator(".media-meeting", { hasText: "Kickoff" });
  await expect(kickoff).toBeVisible();
  await expect(kickoff).toContainText("segments");
  await expect(kickoff.locator("code")).toContainText(/^[0-9a-f]{12}$/);
  await kickoff.getByText("Review", { exact: true }).click();
  await expect(page).toHaveURL(/\/projects\/q3-sync\/meetings\/kickoff$/);
  await expect(page.locator("pre.transcript").first()).toContainText("the recorder was running");
  expect(errors).toEqual([]);
});

test("the landing shows the newest meetings across projects", async ({ page }) => {
  await page.goto("/");
  const recent = page.locator(".recent-activity");
  await expect(recent).toBeVisible();
  await expect(recent).toContainText("Kickoff");
  await recent.getByRole("link", { name: "Kickoff" }).click();
  await expect(page).toHaveURL(/\/projects\/q3-sync\/meetings\/kickoff$/);
});
