import { test, expect } from "@playwright/test";
import { openApp } from "./helpers";

test.describe("boot and shell", () => {
  test("boots into a plan-scoped mode with the shell chrome", async ({ page }) => {
    await openApp(page);
    await expect(page.locator(".shell-header")).toBeVisible();
    await expect(page.locator(".statusbar")).toBeVisible();
    // Skip links are the first focusable elements.
    await page.keyboard.press("Tab");
    await expect(page.locator("a.skip-link:focus")).toBeVisible();
  });

  test("Control room is reachable and shows the seven state badges", async ({ page }) => {
    await openApp(page);
    await page.locator(".mode-tab-control").click();
    await expect(page.locator(".control-room")).toBeVisible();
    const cards = page.locator(".agent-card");
    await expect(cards).toHaveCount(7);
    // Every badge word from the design appears.
    for (const word of ["Live", "Idle", "Stuck", "Finished", "Disconnected", "Error", "Waiting"]) {
      await expect(page.locator(".agent-card", { hasText: word }).first()).toBeVisible();
    }
    // Provenance footer is present and verbatim.
    await expect(page.locator(".audit-provenance")).toContainText(
      "Grogu never records prompts, chain-of-thought, or raw tool arguments",
    );
  });

  test("mode switching by keyboard preserves the shell", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+2");
    await expect(page.locator(".canvas-wrap")).toBeVisible();
    await page.keyboard.press("Meta+3");
    await expect(page.locator(".deps-wrap")).toBeVisible();
    await page.keyboard.press("Meta+4");
    await expect(page.locator(".revision-mode")).toBeVisible();
    await page.keyboard.press("Meta+1");
    await expect(page.locator(".reading-column")).toBeVisible();
  });

  test("React Flow attribution stays visible on the canvas", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+2");
    await expect(page.locator(".react-flow__attribution")).toBeVisible();
  });

  test("command palette opens and filters", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+k");
    await expect(page.locator(".palette-input")).toBeFocused();
    await page.locator(".palette-input").fill("dark");
    await expect(page.locator(".palette-row", { hasText: "Dark theme" })).toBeVisible();
  });

  test("compiled preview shows role, stage and digest", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+e");
    await expect(page.locator(".compiled-modal .modal-title")).toContainText("Compiled projection");
    // The digest lands once /api/projection resolves; allow for load.
    await expect(page.locator(".compiled-modal .modal-title")).toContainText("Digest: sha256:", {
      timeout: 15_000,
    });
  });
});

test.describe("document mode", () => {
  test("renders directives lead and a reading column with marks", async ({ page }) => {
    await openApp(page);
    await expect(page.locator(".doc-section-directives")).toBeVisible();
    // A thread mark chip exists for an anchored node.
    await expect(page.locator(".doc-mark").first()).toBeVisible();
  });

  test("inline edit of a title writes one revision", async ({ page }) => {
    await openApp(page);
    const firstTitle = page.locator(".doc-title").first();
    await firstTitle.dblclick();
    const input = page.locator(".doc-title-edit");
    await input.fill("Edited by test");
    await input.press("Enter");
    // Status bar advances to a Saved revision.
    await expect(page.locator(".status-save")).toContainText("Saved · r", { timeout: 10_000 });
  });
});

test.describe("dependencies mode", () => {
  test("shows a cycle in the cycles drawer", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+3");
    await expect(page.locator(".cycles-drawer")).toBeVisible();
    await expect(page.locator(".cycle-item").first()).toBeVisible();
  });
});

test.describe("revision mode", () => {
  test("timeline shows origin chips and a diff", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+4");
    await expect(page.locator(".rev-timeline")).toBeVisible();
    await expect(page.locator(".origin-migration").first()).toBeVisible();
    await expect(page.locator(".compiled-diff")).toBeVisible();
  });
});
