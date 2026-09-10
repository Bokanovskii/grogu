import { test, expect } from "@playwright/test";
import { openApp } from "./helpers";

test.describe("boot and shell", () => {
  test("restored edits wait for consent and Discard clears persisted state", async ({ page }) => {
    await openApp(page);
    const doc = await page.evaluate(async () => {
      const response = await fetch("/api/doc?scope=all");
      return await response.json();
    });
    const key = `grogu.plan-workspace.queue.${doc.plan}`;
    await page.evaluate(
      ({ storageKey, base }) => {
        localStorage.setItem(
          storageKey,
          JSON.stringify([
            {
              id: "q-restored",
              ops: [
                {
                  op: "replace",
                  path: "/nodes/goal-1/title",
                  value: "Restored title",
                },
              ],
              base,
              intent: "restore title",
              origin: "workspace",
              at: Date.now(),
            },
          ]),
        );
      },
      { storageKey: key, base: doc.revision },
    );
    const requests: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().endsWith("/api/patch")) {
        requests.push(request.postData() ?? "");
      }
    });
    const session = await page.request.get("/__new_session");
    const { url } = await session.json();
    await page.goto(url);
    await expect(page.getByText("Restore unsaved edits?")).toBeVisible();
    await page.waitForTimeout(300);
    expect(requests).toEqual([]);
    await page.getByRole("button", { name: "Discard" }).click();
    expect(await page.evaluate((storageKey) => localStorage.getItem(storageKey), key)).toBeNull();
    expect(requests).toEqual([]);
  });

  test("restored edits submit against the revision originally observed", async ({ page }) => {
    await openApp(page);
    const plan = await page.evaluate(async () => {
      const response = await fetch("/api/doc?scope=all");
      return (await response.json()).plan as string;
    });
    const key = `grogu.plan-workspace.queue.${plan}`;
    await page.evaluate((storageKey) => {
      localStorage.setItem(
        storageKey,
        JSON.stringify([
          {
            id: "q-stale",
            ops: [
              {
                op: "replace",
                path: "/nodes/goal-1/title",
                value: "Restored stale title",
              },
            ],
            base: "r0000",
            intent: "restore stale title",
            origin: "workspace",
            at: Date.now(),
          },
        ]),
      );
    }, key);
    const bases: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().endsWith("/api/patch")) {
        const body = request.postDataJSON() as { base?: string };
        if (body.base) bases.push(body.base);
      }
    });
    const session = await page.request.get("/__new_session");
    const { url } = await session.json();
    await page.goto(url);
    await page.getByRole("button", { name: "Apply" }).click();
    await expect.poll(() => bases.length).toBeGreaterThan(0);
    expect(bases[0]).toBe("r0000");
  });

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

  test("Open timeline expands, focuses, and loads the audit panel", async ({ page }) => {
    await openApp(page);
    await page.locator(".mode-tab-control").click();
    await page.getByRole("button", { name: "Collapse Audit timeline panel" }).click();
    await expect(page.getByRole("button", { name: "Expand Audit timeline panel" })).toBeVisible();
    await page.route(/\/api\/control\/[^/?]+$/, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 300));
      await route.continue();
    });
    await page.locator(".agent-card").first().getByRole("button", { name: "Open timeline" }).click();
    await expect(page.locator(".panel-right")).toBeVisible();
    await expect(page.getByText("Loading timeline…")).toBeVisible();
    await expect(page.locator(".audit-title")).not.toHaveText("Audit timeline");
    await expect(page.locator("#control-agents")).toBeFocused();
  });

  test("Open timeline reports an unavailable drill-down", async ({ page }) => {
    await openApp(page);
    await page.locator(".mode-tab-control").click();
    await page.route(/\/api\/control\/[^/?]+$/, async (route) => {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          error: "unavailable",
          message: "The registered activity source could not be read.",
        }),
      });
    });
    await page.locator(".agent-card").first().getByRole("button", { name: "Open timeline" }).click();
    await expect(page.getByText("Timeline unavailable.")).toBeVisible();
    await expect(page.getByText("The registered activity source could not be read.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  });

  test("an empty timeline explains why a Live agent can have no events", async ({ page }) => {
    await openApp(page);
    await page.locator(".mode-tab-control").click();
    await page.route(/\/api\/control\/[^/?]+$/, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({
        response,
        json: { ...body, activity: [] },
      });
    });
    await page.locator(".agent-card").first().getByRole("button", { name: "Open timeline" }).click();
    await expect(page.getByText("No detailed events recorded.")).toBeVisible();
    await expect(page.getByText(/Live badge can come from recent Grogu command activity/)).toBeVisible();
  });

  test("feedback scopes name concrete audiences without redundant actions", async ({ page }) => {
    await openApp(page);
    await page.locator(".mode-tab-control").click();
    await page.locator(".agent-card").first().getByRole("button", { name: "Send feedback" }).click();
    await expect(page.getByRole("radio", { name: "This agent" })).toBeVisible();
    await expect(page.getByRole("radio", { name: "All engineers" })).toBeVisible();
    await expect(page.getByRole("radio", { name: "Everyone on this plan" })).toBeVisible();
    await expect(page.getByRole("radio", { name: "Engineers on this plan" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Save as draft" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Abandon feedback" })).toHaveCount(0);
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

  test("drawn regions persist after reload", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+2");
    const regions = page.locator(".react-flow__node-region");
    const before = await regions.count();
    await page
      .locator(".tool-palette")
      .getByRole("button", { name: "Rectangle", exact: true })
      .click();
    const pane = page.locator(".react-flow__pane");
    const box = await pane.boundingBox();
    expect(box).not.toBeNull();
    await page.mouse.move(box!.x + 420, box!.y + 280);
    await page.mouse.down();
    await page.mouse.move(box!.x + 620, box!.y + 400);
    await page.mouse.up();
    await expect(page.locator(".status-save")).toContainText("Saved · r");
    await expect(regions).toHaveCount(before + 1);
    await expect(page.getByText(/^Comment on reg-/)).toBeVisible();
    await page.keyboard.press("Escape");

    await page.reload();
    await expect(page.locator(".shell")).toBeVisible();
    await page.keyboard.press("Meta+2");
    await expect(page.locator(".react-flow__node-region")).toHaveCount(before + 1);
  });

  test("Frame creates a grouping container without opening a comment composer", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+2");
    const frames = page.locator(".cv-region-frame");
    const before = await frames.count();
    await page.locator(".tool-palette").getByRole("button", { name: "Frame" }).click();
    const pane = page.locator(".react-flow__pane");
    const box = await pane.boundingBox();
    expect(box).not.toBeNull();
    await page.mouse.move(box!.x + 360, box!.y + 240);
    await page.mouse.down();
    await page.mouse.move(box!.x + 660, box!.y + 440);
    await page.mouse.up();
    await expect(page.locator(".status-save")).toContainText("Saved · r");
    await expect(frames).toHaveCount(before + 1);
    await expect(page.getByText(/^Comment on reg-/)).toHaveCount(0);
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

  test("compiled preview reports failures and retries", async ({ page }) => {
    let attempts = 0;
    await page.route("**/api/projection?*", async (route) => {
      attempts += 1;
      if (attempts === 1) {
        await route.fulfill({
          status: 422,
          contentType: "application/json",
          body: JSON.stringify({
            error: "compile_failed",
            message: "Projection temporarily unavailable.",
          }),
        });
      } else {
        await route.fallback();
      }
    });
    await openApp(page);
    await page.keyboard.press("Meta+e");
    await expect(page.getByText("Could not compile this preview.")).toBeVisible();
    await expect(page.getByText("Projection temporarily unavailable.")).toBeVisible();
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.locator(".compiled-modal .modal-title")).toContainText("Digest: sha256:");
  });

  test("comment actions stay inside the rail and Reply reflects composer state", async ({ page }) => {
    await openApp(page);
    const card = page.locator(".thread-card").first();
    const reply = card.getByRole("button", { name: "Reply" });
    await expect(reply).toBeDisabled();
    await card.getByRole("textbox", { name: "Reply to thread" }).fill("A response");
    await expect(reply).toBeEnabled();
    const cardBox = await card.boundingBox();
    expect(cardBox).not.toBeNull();
    for (const button of await card.locator(".thread-actions button").all()) {
      const box = await button.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(cardBox!.x);
      expect(box!.x + box!.width).toBeLessThanOrEqual(cardBox!.x + cardBox!.width);
    }
  });

  test("Ask Grogu routes a revision request instead of creating an empty proposal", async ({ page }) => {
    await openApp(page);
    const card = page.locator(".thread-card").first();
    await card.getByRole("button", { name: "Ask Grogu to revise" }).click();
    await page.getByRole("textbox", { name: "Instruction" }).fill(
      "Split this work into two migration steps.",
    );
    await page.getByRole("button", { name: "Send request" }).click();
    await expect(
      page.getByText(
        /Revision request f-[^ ]+ recorded; implementation waits for architect acknowledgement/,
      ),
    ).toBeVisible();
    await expect(page.locator(".proposal-modal")).toHaveCount(0);
    await expect(card.getByText("Revision requested")).toBeVisible();
    await expect(card.getByText(/Waiting for architect/)).toBeVisible();
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
  test("uses the session access scope and functional stage filters", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+3");
    await expect(page.getByRole("heading", { name: "Role context" })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Access scope" })).toBeVisible();
    await expect(page.locator(".deps-scope-note")).toContainText("architect");
    await expect(page.locator(".dep-node")).toHaveCount(22);

    await page.getByRole("button", { name: "Goal" }).click();
    await expect(page.locator(".dep-node")).toHaveCount(21);
    await page.getByRole("tab", { name: "Implementation" }).click();
    await expect(page.locator(".dep-node")).toHaveCount(1);
    await expect(page.locator(".dep-node")).toContainText("Implementation-only canvas task");
    await page.getByRole("tab", { name: "All stages" }).click();
    await expect(page.locator(".dep-node")).toHaveCount(22);
    await expect(page.locator(".deps-scope-note")).toContainText("All readable stages");
  });

  test("shows a cycle in the cycles drawer", async ({ page }) => {
    await openApp(page);
    await page.keyboard.press("Meta+3");
    await expect(page.locator(".cycles-drawer")).toBeVisible();
    await expect(page.locator(".cycle-item").first()).toBeVisible();
    await expect(page.locator(".react-flow__edge-path").first()).toHaveAttribute(
      "d",
      /^M/,
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
