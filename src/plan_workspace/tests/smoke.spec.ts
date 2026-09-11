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

  test("Document presents a human summary, contents, and collapsible deep dives", async ({ page }) => {
    await openApp(page);
    await expect(page.getByRole("heading", { name: "Migrate storage backend", exact: true })).toBeVisible();
    await expect(page.getByText("In this stage")).toBeVisible();
    await expect(page.getByText("Table of contents")).toBeVisible();
    await expect(page.locator(".doc-overview")).toBeVisible();
    await expect(page.locator(".doc-node").first()).toBeVisible();
    await expect(page.locator(".doc-node-id:visible")).toHaveCount(0);
  });

  test("Control room is reachable and separates lifecycle, activity, and connection", async ({ page }) => {
    await openApp(page);
    await page.locator(".mode-tab-control").click();
    await expect(page.locator(".control-room")).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Expand Filters panel" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Expand Agent timeline panel" }),
    ).toBeVisible();
    const rows = page.locator("[data-control-agent-row]:visible");
    await expect(rows).toHaveCount(7);
    for (const word of ["Running", "Finished", "Failed", "Possibly stuck", "Disconnected"]) {
      await expect(rows.filter({ hasText: word }).first()).toBeVisible();
    }
    await page.getByRole("tab", { name: "Topology", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "Agent and session topology" }),
    ).toBeVisible();
    await expect(page.locator(".cr-topology-node")).toHaveCount(7);
    await expect(page.locator(".cr-topology-branch", { hasText: "●" })).toHaveCount(0);
    await expect
      .poll(() =>
        page.locator(".cr-topology-branch").evaluateAll((branches) =>
          branches.every(
            (branch) =>
              branch.offsetParent instanceof HTMLButtonElement &&
              branch.offsetParent.closest(".cr-topology") !== null,
          ),
        ),
      )
      .toBe(true);
    await page
      .getByRole("button", { name: "Expand Agent timeline panel" })
      .click();
    await expect(page.locator(".audit-provenance")).toContainText(
      "Prompts, model reasoning, tool arguments and tool results are never recorded",
    );
    await page.getByRole("tab", { name: "Document", exact: true }).click();
    await expect(page.getByRole("region", { name: "Outline" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Comments" })).toBeVisible();
  });

  test("Open timeline expands, focuses, and loads the audit panel", async ({ page }) => {
    await openApp(page);
    await page.locator(".mode-tab-control").click();
    await expect(page.getByRole("button", { name: "Expand Agent timeline panel" })).toBeVisible();
    await page.route(/\/api\/control\/[^/?]+$/, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 300));
      await route.continue();
    });
    await page.locator("[data-control-agent-row]:visible").first().getByRole("button", { name: "Watch" }).click();
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
    await page.locator("[data-control-agent-row]:visible").first().getByRole("button", { name: "Watch" }).click();
    await expect(page.getByText("Timeline disconnected.")).toBeVisible();
    await expect(page.getByText(/Previously observed lifecycle remains unchanged/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  });

  test("an empty registered timeline is distinct from unavailable", async ({ page }) => {
    await openApp(page);
    await page.locator(".mode-tab-control").click();
    await page.route(/\/api\/control\/[^/?]+$/, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({
        response,
        json: {
          ...body,
          activity: [],
          recent_events: [],
          events_status: "empty",
          agent: {
            ...body.agent,
            events_status: "empty",
            event_source_registered: true,
          },
        },
      });
    });
    await page.locator("[data-control-agent-row]:visible").first().getByRole("button", { name: "Watch" }).click();
    await expect(page.getByText("No events in the last 24h.")).toBeVisible();
    await expect(page.getByText("Widen the range to see older activity.")).toBeVisible();
  });

  test("feedback scopes name concrete audiences without redundant actions", async ({ page }) => {
    await openApp(page);
    await page.locator(".mode-tab-control").click();
    await page.locator("[data-control-agent-row]:visible").first().getByRole("button", { name: /Feedback/ }).click();
    const dialog = page.getByRole("dialog", { name: "Send feedback" });
    const targets = dialog.getByRole("radio");
    await expect(targets).toHaveCount(3);
    await expect(targets.nth(0)).toHaveText("This agent");
    await expect(targets.nth(1)).toHaveText(/^All \w+s on this plan$/);
    await expect(targets.nth(2)).toHaveText("Everyone on this plan");
    await expect(dialog.getByRole("button", { name: "Save as draft" })).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: "Abandon feedback" })).toHaveCount(0);
  });

  test("selected-plan filters and timeline drawer never overlap program content", async ({ page }) => {
    await openApp(page);
    await page.locator(".mode-tab-control").click();
    await page.locator(".cr-program-details > summary").click();
    await page.locator(".cr-plan-card").first().click();
    await expect(page.locator(".cr-active-filters")).toBeVisible();
    const filters = await page.locator(".cr-active-filters").boundingBox();
    const program = await page.locator(".cr-program").boundingBox();
    expect(filters).not.toBeNull();
    expect(program).not.toBeNull();
    expect(filters!.y + filters!.height).toBeLessThanOrEqual(program!.y + 1);

    await page
      .locator("[data-control-agent-row]:visible")
      .first()
      .getByRole("button", { name: "Watch" })
      .click();
    await expect(page.locator(".audit-body")).toBeVisible();
    await expect(page.locator(".audit-composer")).not.toHaveAttribute("open", "");
    await expect(page.locator(".audit-empty-title")).toBeVisible();
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
    const initialCount = await page.locator(".dep-node").count();
    expect(initialCount).toBeGreaterThan(0);

    await page.getByRole("button", { name: "Goal" }).click();
    await expect(page.locator(".dep-node")).toHaveCount(initialCount - 1);
    await page.getByRole("tab", { name: "Implementation" }).click();
    await expect(page.locator(".dep-node")).toHaveCount(1);
    await expect(page.locator(".dep-node")).toContainText("Implementation-only canvas task");
    await page.getByRole("tab", { name: "All stages" }).click();
    await expect.poll(() => page.locator(".dep-node").count()).toBeGreaterThan(1);
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
    await expect(page.getByRole("tab", { name: "Top-down" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const firstNode = page.locator(".dep-node").first();
    await expect(firstNode.locator(".dep-node-summary")).toBeVisible();
    await firstNode.click();
    await expect(page.getByRole("tab", { name: "Inspector" })).toBeVisible();
    await expect(page.locator(".inspector")).toBeVisible();
    await expect(firstNode).not.toContainText(/task-\d+/);
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
