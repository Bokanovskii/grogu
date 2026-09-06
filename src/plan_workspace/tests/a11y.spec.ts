import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { openApp, setTheme } from "./helpers";

// Automated accessibility scans on every mode, at the four widths and both
// themes. We assert no serious or critical violations; colour-contrast is
// included because the token palette claims AAA/AA.
const WIDTHS = [1440, 1024, 768, 390];
const THEMES: ("light" | "dark")[] = ["light", "dark"];

async function scan(page: import("@playwright/test").Page, context: string) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(serious, `${context}: ${serious.map((v) => v.id).join(", ")}`).toEqual([]);
}

for (const theme of THEMES) {
  for (const width of WIDTHS) {
    test(`control room passes axe (${theme}, ${width})`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await openApp(page);
      await setTheme(page, theme);
      await page.locator(".mode-tab-control").click();
      await expect(page.locator(".control-room")).toBeVisible();
      await scan(page, `control ${theme} ${width}`);
    });
  }
}

for (const theme of THEMES) {
  test(`document mode passes axe (${theme})`, async ({ page }) => {
    await openApp(page);
    await setTheme(page, theme);
    await page.keyboard.press("Meta+1");
    await expect(page.locator(".reading-column")).toBeVisible();
    await scan(page, `document ${theme}`);
  });

  test(`revision mode passes axe (${theme})`, async ({ page }) => {
    await openApp(page);
    await setTheme(page, theme);
    await page.keyboard.press("Meta+4");
    await expect(page.locator(".revision-mode")).toBeVisible();
    await scan(page, `revision ${theme}`);
  });
}
