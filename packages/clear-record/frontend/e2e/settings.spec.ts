// Settings is the control plane (ADR-0027): one section per URL, read-mostly,
// and one agent panel with two entry points. This spec drives every section and
// asserts the read sections carry no write.
import { test, expect, type Page } from "@playwright/test";

function watch(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push("console: " + m.text());
  });
  return errors;
}

type Section = { slug: string; heading: string; marker: string };

const SECTIONS: Section[] = [
  { slug: "models", heading: "Models", marker: ".table-settings" },
  { slug: "backends", heading: "Backends", marker: ".backend-list" },
  { slug: "agent", heading: "Agent", marker: ".agent-setup" },
  { slug: "mcp", heading: "MCP", marker: ".mcp-setup" },
  { slug: "webhooks", heading: "Webhooks", marker: ".webhooks" },
  { slug: "storage", heading: "Storage", marker: ".table-settings" },
  { slug: "status", heading: "Status", marker: "#hello-check" },
];

test("every settings section is a real, deep-linkable URL", async ({ page }) => {
  const errors = watch(page);

  await page.goto("/settings");
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.locator("#detail h2")).toHaveText("Models");
  await expect(page.locator(".settings-nav a[aria-current='page']")).toHaveText("Models");

  for (const section of SECTIONS) {
    await page.goto("/settings/" + section.slug);
    await expect(page.locator("#detail h2")).toHaveText(section.heading);
    await expect(page.locator(".settings-nav a[aria-current='page']")).toHaveText(section.heading);
    await expect(page.locator(section.marker).first()).toBeVisible();
  }
  expect(errors).toEqual([]);
});

test("a section is refresh-safe and an unknown one is a page 404", async ({ page }) => {
  await page.goto("/settings/storage");
  await page.reload();
  await expect(page.locator(".settings-nav a[aria-current='page']")).toHaveText("Storage");
  await expect(page.locator("#detail h2")).toHaveText("Storage");

  const response = await page.goto("/settings/does-not-exist");
  expect(response?.status()).toBe(404);
  await expect(page.getByRole("heading", { name: "Not found" })).toBeVisible();
});

test("the read sections carry no write control, except the Models download", async ({ page }) => {
  for (const slug of ["backends", "storage"]) {
    await page.goto("/settings/" + slug);
    await expect(page.locator("#detail [hx-post]")).toHaveCount(0);
    await expect(page.locator("#detail form")).toHaveCount(0);
  }
  // Models is the one read section that writes: the user-triggered, verified
  // model download. Every control there POSTs to that one route.
  await page.goto("/settings/models");
  const forms = page.locator('#detail form[hx-post="/ui/settings/models/download"]');
  expect(await forms.count()).toBeGreaterThan(0);
});

test("status carries the hello-world check and the setup knob", async ({ page }) => {
  await page.goto("/settings/status");
  // Two writes: ticket 05's diagnostic POST, and walking setup again (which
  // only forgets the marker).
  await expect(page.locator('#detail [hx-post="/ui/hello-check"]')).toHaveCount(1);
  await expect(page.locator('#detail form[action="/setup/restart"]')).toHaveCount(1);
  await expect(page.locator("#hello-check")).toBeVisible();
});

test("the agent flow is one four-stage panel with two entry points", async ({ page }) => {
  for (const path of ["/settings/agent", "/setup/agent"]) {
    await page.goto(path);
    await expect(page.locator(".agent-setup")).toBeVisible();
    await expect(page.locator(".agent-flow .agent-stage")).toHaveCount(4);
    await expect(page.locator("#agent-stage-endpoint")).toContainText("Endpoint");
    await expect(page.locator("#agent-stage-try")).toContainText("Try it");
    await expect(page.locator("#agent-stage-try #hello-check")).toBeVisible();
  }
});

test("the MCP section exposes the harness and client-config writes", async ({ page }) => {
  const errors = watch(page);
  await page.goto("/settings/mcp");
  await expect(page.locator(".mcp-setup")).toBeVisible();
  await expect(page.getByPlaceholder("path to your MCP client config (.json)")).toBeVisible();
  expect(errors).toEqual([]);
});

test("the webhooks section shows the delivery panel", async ({ page }) => {
  await page.goto("/settings/webhooks");
  await expect(page.locator(".webhooks")).toBeVisible();
});

test("the models section shows the model directory and the defaults", async ({ page }) => {
  await page.goto("/settings/models");
  await expect(page.getByText("Model directory")).toBeVisible();
  await expect(page.getByText("Built-in profiles")).toBeVisible();
  await expect(page.getByText("the built-in checkpoint")).toBeVisible();
});

test("the status section carries the diagnostics download and the hello check", async ({ page }) => {
  await page.goto("/settings/status");
  await expect(page.locator("#hello-check")).toBeVisible();
  await expect(page.locator('#detail a[href="/ui/diagnostics"]')).toBeVisible();
});
