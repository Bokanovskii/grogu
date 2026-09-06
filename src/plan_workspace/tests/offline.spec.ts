import { test, expect, type Request } from "@playwright/test";
import { openApp } from "./helpers";

// Invariant I7: nothing leaves the machine. Record every request the page makes
// and fail on any destination that is not the bound loopback origin.
test("the app makes no non-loopback request across every mode", async ({ page }) => {
  const offending: string[] = [];
  const check = (req: Request) => {
    const u = new URL(req.url());
    const okHost = u.hostname === "127.0.0.1" || u.hostname === "localhost";
    const okScheme = u.protocol === "http:" || u.protocol === "https:" || u.protocol === "data:" || u.protocol === "blob:";
    if (!okHost && u.protocol !== "data:" && u.protocol !== "blob:") offending.push(req.url());
    if (!okScheme) offending.push(req.url());
  };
  page.on("request", check);

  await openApp(page);
  // Walk every mode so any lazy asset or telemetry beacon would fire.
  await page.locator(".mode-tab-control").click();
  await page.keyboard.press("Meta+1");
  await page.keyboard.press("Meta+2");
  await page.keyboard.press("Meta+3");
  await page.keyboard.press("Meta+4");
  await page.keyboard.press("Meta+k");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(500);

  expect(offending, `non-loopback requests: ${offending.join(", ")}`).toEqual([]);
});

test("no webfont is downloaded — system fonts only", async ({ page }) => {
  const fontRequests: string[] = [];
  page.on("request", (req) => {
    if (req.resourceType() === "font") fontRequests.push(req.url());
  });
  await openApp(page);
  await page.keyboard.press("Meta+2");
  await page.waitForTimeout(300);
  expect(fontRequests).toEqual([]);
});

test("a spent launch token shows the session-spent panel", async ({ page }) => {
  const res = await page.request.get("/__new_session");
  const { url } = await res.json();
  await page.goto(url); // first use consumes it
  await expect(page.locator(".shell")).toBeVisible();
  // Second use of the same token is refused.
  await page.goto(url);
  await expect(page.locator(".state-panel-title")).toContainText("already been used");
});
