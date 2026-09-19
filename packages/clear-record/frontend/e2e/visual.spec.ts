// Visual inspection: capture the real surfaces at the widths and schemes the
// design has to hold on. Output goes to E2E_SHOTS (gitignored); review the PNGs.
import { test, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

import { shotsDir } from "./paths";

test.beforeAll(() => mkdirSync(shotsDir, { recursive: true }));

type Scheme = "light" | "dark";
type Size = { width: number; height: number; tag: string };

const DESKTOP: Size = { width: 1440, height: 900, tag: "desktop" };
const MOBILE: Size = { width: 390, height: 844, tag: "mobile" };

async function shoot(page: Page, name: string, size: Size, scheme: Scheme) {
  await page.setViewportSize({ width: size.width, height: size.height });
  await page.emulateMedia({ colorScheme: scheme });
  // A full-page capture stitches scrolled frames, which leaves sticky elements
  // stuck mid-page; unpin them so the capture is one honest page.
  await page.addStyleTag({
    content: ".app-header,.sidebar,thead th{position:static !important}",
  });
  // Let htmx swaps and the stylesheet settle before the capture.
  await page.waitForTimeout(250);
  await page.screenshot({ path: join(shotsDir, `${name}-${size.tag}-${scheme}.png`), fullPage: true });
}

for (const scheme of ["light", "dark"] as const) {
  for (const size of [DESKTOP, MOBILE]) {
    test(`projects ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/");
      await page.locator("#projects .project").first().waitFor();
      await shoot(page, "projects", size, scheme);
    });

    // One capture per project tab: the split is the point of this ticket.
    test(`project overview ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/projects/q3-sync");
      await page.locator("#detail h2").waitFor();
      await shoot(page, "project-overview", size, scheme);
    });

    test(`project meetings ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/projects/q3-sync/meetings");
      await page.locator("#detail .meeting").first().waitFor();
      await shoot(page, "project-meetings", size, scheme);
    });

    test(`project glossary ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/projects/q3-sync/glossary");
      await page.locator(".table-glossary").waitFor();
      await shoot(page, "project-glossary", size, scheme);
    });

    test(`project media ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/projects/q3-sync/media");
      await page.locator(".media-meeting").first().waitFor();
      await shoot(page, "project-media", size, scheme);
    });

    test(`meeting ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/projects/q3-sync/meetings/kickoff");
      await page.locator("pre.transcript").first().waitFor();
      await shoot(page, "meeting", size, scheme);
    });

    // The pipeline status page (RUN-03): the live queue and the history, with
    // the header chip fed from the same reads.
    test(`activity ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/activity");
      await page.locator(".activity .run").first().waitFor();
      await shoot(page, "activity", size, scheme);
    });

    // One capture per Settings section: the split is the point of this ticket.
    for (const section of [
      { slug: "models", name: "models", marker: ".table-settings" },
      { slug: "backends", name: "backends", marker: ".backend-list" },
      { slug: "agent", name: "agent", marker: ".agent-setup" },
      { slug: "mcp", name: "mcp", marker: ".mcp-setup" },
      { slug: "webhooks", name: "webhooks", marker: ".webhooks" },
      { slug: "storage", name: "storage", marker: ".table-settings" },
      { slug: "status", name: "status", marker: "#hello-check" },
    ]) {
      test(`settings ${section.name} ${size.tag} ${scheme}`, async ({ page }) => {
        await page.goto(`/settings/${section.slug}`);
        await page.locator(section.marker).first().waitFor();
        await shoot(page, `settings-${section.name}`, size, scheme);
      });
    }

    test(`setup ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/setup");
      await page.locator(".agent-setup").waitFor();
      await shoot(page, "setup", size, scheme);
    });

    // Ticket 05: the agent flow as its own entry point, and the permanent
    // diagnostic at its deterministic finding state on this machine.
    test(`setup agent ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/setup/agent");
      await page.locator(".agent-flow").waitFor();
      await shoot(page, "setup-agent", size, scheme);
    });

    test(`settings status finding ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/settings/status");
      await page.getByRole("button", { name: "Run the check" }).click();
      await page.locator("#hello-check .hello-result").waitFor();
      await shoot(page, "settings-status-finding", size, scheme);
    });
  }
}
