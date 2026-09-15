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
    test(`home ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/");
      await page.locator("#projects .project").first().waitFor();
      await shoot(page, "home", size, scheme);
    });

    test(`project ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/");
      await page.locator("#projects .project", { hasText: "Q3 sync" }).click();
      await page.locator("#detail .meeting").first().waitFor();
      await shoot(page, "project", size, scheme);
    });

    test(`meeting ${size.tag} ${scheme}`, async ({ page }) => {
      await page.goto("/");
      await page.locator("#projects .project", { hasText: "Q3 sync" }).click();
      await page.locator("#detail .meeting", { hasText: "Kickoff" }).getByText("Review", { exact: true }).click();
      await page.locator("pre.transcript").first().waitFor();
      await shoot(page, "meeting", size, scheme);
    });
  }
}
