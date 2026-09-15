// The component gallery: every component and status hue on one page, loading the
// same committed app.css the console ships. This is where a palette or spacing
// change is reviewed as a whole rather than inferred from three pages.
import { test, type Page } from "@playwright/test";
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { shotsDir } from "./paths";

const fixture = readFileSync(new URL("./fixtures/components.html", import.meta.url), "utf8");

test.beforeAll(() => mkdirSync(shotsDir, { recursive: true }));

async function render(page: Page, scheme: "light" | "dark", width: number, tag: string) {
  await page.goto("/");
  await page.setContent(fixture, { waitUntil: "load" });
  await page.setViewportSize({ width, height: 1000 });
  await page.emulateMedia({ colorScheme: scheme });
  await page.waitForTimeout(200);
  await page.screenshot({ path: join(shotsDir, `gallery-${tag}-${scheme}.png`), fullPage: true });
}

test("gallery light", async ({ page }) => render(page, "light", 1280, "desktop"));
test("gallery dark", async ({ page }) => render(page, "dark", 1280, "desktop"));
test("gallery light mobile", async ({ page }) => render(page, "light", 390, "mobile"));
