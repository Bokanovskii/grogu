import { test, expect } from "@playwright/test";
import { openApp } from "./helpers";

test.describe("boot and shell", () => {
  test("boots into a plan-scoped mode with the shell chrome", async ({ page }) => {
    await openApp(page);
    await expect(page.locator(".shell-header")).toBeVisible();
    await expect(page.locator(".statusbar")).toBeVisible();
    await expect(page.getByRole("tab", { name: "UX" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "design" })).toHaveCount(0);
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
    await expect(page.locator(".react-flow__minimap")).toHaveCount(0);
  });

  test("Tidy reports only the failed local apply when layout ops are invalid", async ({ page }) => {
    await openApp(page);
    await page.route("**/api/layout", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ops: [{ op: "replace", path: "/nodes/missing/title", value: "invalid" }],
        }),
      });
    });
    await page.keyboard.press("Meta+2");
    await page.getByRole("button", { name: "Tidy" }).click();
    await expect(page.getByText("That change could not be applied locally.")).toBeVisible();
    await expect(page.getByText("Applied auto-layout.")).toHaveCount(0);
  });

  test("a successful Tidy creates one undoable revision", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+2");
    await page.getByRole("button", { name: "Tidy" }).click();
    await expect(page.getByText("Applied auto-layout.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Undo" })).toBeEnabled();
    await page.getByRole("button", { name: "Undo" }).click();
    await expect(page.locator(".status-save")).toContainText("Saved · r");
    await expect(page.getByRole("button", { name: "Redo" })).toBeEnabled();
  });

  test("Control-drag grabs and pans the canvas without switching tools", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+2");
    const pane = page.locator(".react-flow__pane");
    const viewport = page.locator(".react-flow__viewport");
    const before = await viewport.getAttribute("style");
    const box = await pane.boundingBox();
    expect(box).not.toBeNull();
    await page.keyboard.down("Control");
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await page.mouse.down();
    await page.mouse.move(box!.x + box!.width / 2 + 80, box!.y + box!.height / 2 + 48);
    await page.mouse.up();
    await page.keyboard.up("Control");
    await expect(viewport).not.toHaveAttribute("style", before ?? "");
    await expect(page.locator(".tool-select")).toBeVisible();
  });

  test("Canvas shows only the selected stage", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+2");
    await expect(page.locator("#cv-title-goal-1")).toBeVisible();
    await expect(page.locator("#cv-title-task-implementation")).toHaveCount(0);
    await page.getByRole("tab", { name: "Implementation" }).click();
    await expect(page.locator("#cv-title-task-implementation")).toBeVisible();
    await expect(page.locator("#cv-title-goal-1")).toHaveCount(0);
  });

  test("switching to Canvas with a document node selected does not crash", async ({ page }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await openApp(page);
    const nodeWithoutGeometry = page.locator(".doc-node").filter({
      has: page.locator(".doc-node-kind", { hasText: "Directive" }),
    }).first();
    await nodeWithoutGeometry.click();
    await page.keyboard.press("Meta+2");
    await page.waitForTimeout(100);
    expect(pageErrors).toEqual([]);
    await expect(page.locator(".canvas-wrap")).toBeVisible();
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
    await expect(page.locator(".react-flow__edge-path").first()).toHaveAttribute(
      "d",
      /^M /,
    );
    await expect(page.locator(".react-flow__edge-text").first()).toBeVisible();
    await expect(page.locator(".react-flow__minimap")).toHaveCount(0);
    await expect(page.getByRole("tab", { name: "Left-right" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const firstNode = page.locator(".dep-node").first();
    await firstNode.getByRole("button", { name: /^Expand / }).click();
    await expect(firstNode).toHaveClass(/is-expanded/);
    await expect(firstNode.locator(".dep-node-body")).toBeVisible();
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
