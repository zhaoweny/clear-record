// The pipeline status page (RUN-03): the shared queue's live runs across
// projects, the history behind them, and the header chip fed from the same
// reads. The seeded console always has one run genuinely in flight and one
// queued behind it (see seed.py and run_owner.py).
import { test, expect, type Page } from "@playwright/test";

function watch(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`console: ${m.text()}`);
  });
  return errors;
}

test("the nav reaches the status page", async ({ page }) => {
  await page.goto("/");
  await page.locator(".app-nav a", { hasText: "Activity" }).click();
  await expect(page).toHaveURL(/\/activity$/);
  await expect(page.getByRole("heading", { name: "Activity" })).toBeVisible();
});

test("the seeded running and queued runs render, across projects", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/activity");

  // In flight: the run the seed's owner holds, with the stage and progress its
  // own event stream last reported.
  const running = page.locator(".activity .run", { hasText: "Interview 04" });
  await expect(running.locator(".badge", { hasText: "running" })).toBeVisible();
  await expect(running.locator(".run-stage")).toHaveText("transcribe");
  // The progress its own event stream reported: a chunk count of the plan,
  // which advances while the suite runs (the exact rate so far is pinned by
  // tests/web/test_web_activity.py, where the clock is a seeded event).
  await expect(running.locator(".run-count").first()).toHaveText(/\d+ \/ 240/);
  await expect(running).toContainText("whisper-large-v3");
  await expect(running).toContainText("cli"); // the surface that started it
  // Its meeting is a real link into the project it belongs to.
  await expect(running.locator("a", { hasText: "Interview 04" })).toHaveAttribute(
    "href",
    "/projects/field-interviews/meetings/interview-04",
  );

  // Queued behind it, in a different project: the queue runs one thing at a
  // time, so this one cannot start.
  const queued = page.locator(".activity .run", { hasText: "Design review" });
  await expect(queued.locator(".badge", { hasText: "queued" })).toBeVisible();
  await expect(queued).toContainText("position 1");
  await expect(queued).toContainText("mcp"); // an agent's surface started it
  await expect(queued.locator("a", { hasText: "Design review" })).toHaveAttribute(
    "href",
    "/projects/q3-sync/meetings/design-review",
  );

  // History: the seeded finished runs, with the figures their records carry.
  const finished = page.locator(".activity", { hasText: "Recently finished" });
  await expect(finished.locator(".badge", { hasText: "done" }).first()).toBeVisible();
  expect(errors).toEqual([]);
});

test("the header chip reflects the live state and links to the page", async ({ page }) => {
  const chip = page.locator("#status");

  // On the landing page the chip is fed by the same registry, not by a literal:
  // one run is in flight, so it says so.
  await page.goto("/");
  await expect(chip).toHaveText("running 1");
  await expect(chip).toHaveAttribute("href", "/activity");
  await expect(chip).toHaveClass(/status-running/);

  // And the same answer on the status page itself.
  await page.goto("/activity");
  await expect(chip).toHaveText("running 1");
});
