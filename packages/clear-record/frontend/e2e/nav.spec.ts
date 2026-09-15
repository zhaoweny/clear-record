// The console shell: real URLs, a real nav, boosted links, and page-level 404s.
// This is the shell ticket's own spec — it asserts the *URL* the click produces,
// not merely that some panel appeared, and it checks a plain link still
// navigates with JavaScript switched off.
import { test, expect } from "@playwright/test";

test("the header nav reaches Settings and Setup", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".app-nav a", { hasText: "Projects" })).toBeVisible();

  await page.locator(".app-nav a", { hasText: "Settings" }).click();
  await expect(page).toHaveURL(/\/settings$/);
  // Settings lands on the first of its pinned sections (models).
  await expect(page.locator("#detail h2")).toHaveText("Models");

  // Setup is in the nav exactly while setup is incomplete. The machine surface
  // is the source of truth, so a configured developer box does not flake this.
  const setup = await page.request.get("/api/agent/setup");
  const configured = (await setup.json()).configured;
  const setupLink = page.locator(".app-nav a", { hasText: "Setup" });
  if (configured) {
    await expect(setupLink).toHaveCount(0);
  } else {
    await expect(setupLink).toBeVisible();
    await setupLink.click();
    await expect(page).toHaveURL(/\/setup$/);
    await expect(page.locator(".agent-setup")).toBeVisible();
  }
});

test("a project opens on its own URL and the back button returns", async ({ page }) => {
  await page.goto("/");
  await page.locator("#projects .project", { hasText: "Q3 sync" }).click();
  await expect(page).toHaveURL(/\/projects\/q3-sync$/);
  await expect(page.locator("#detail h2")).toHaveText("Q3 sync");

  await page.goBack();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator("#projects .project").first()).toBeVisible();
});

test("an unknown project is a page, not a fragment", async ({ page }) => {
  const response = await page.goto("/projects/does-not-exist");
  expect(response?.status()).toBe(404);
  await expect(page.getByRole("heading", { name: "Not found" })).toBeVisible();
});

test.describe("with JavaScript disabled", () => {
  test.use({ javaScriptEnabled: false });

  test("server-rendered links navigate on their own", async ({ page }) => {
    await page.goto("/");
    const project = page.locator("#projects .project", { hasText: "Q3 sync" });
    await expect(project).toBeVisible();
    await project.click();
    await expect(page).toHaveURL(/\/projects\/q3-sync$/);
    await expect(page.locator("#detail h2")).toHaveText("Q3 sync");
  });
});
