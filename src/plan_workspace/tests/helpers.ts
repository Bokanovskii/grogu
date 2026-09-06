import { type Page, expect } from "@playwright/test";

// Boot the app through a fresh single-use launch token, exactly as
// `grogu plan doc open` would. Waits until the shell is ready (SSE connected,
// control room or a plan mode painted).
export async function openApp(page: Page): Promise<void> {
  const res = await page.request.get("/__new_session");
  const { url } = await res.json();
  await page.goto(url);
  await expect(page.locator(".shell")).toBeVisible({ timeout: 15_000 });
}

export async function setViewport(page: Page, width: number, height = 900): Promise<void> {
  await page.setViewportSize({ width, height });
}

export async function setTheme(page: Page, theme: "light" | "dark"): Promise<void> {
  await page.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
}
