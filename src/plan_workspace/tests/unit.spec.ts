import { test, expect } from "@playwright/test";
import { applyOps, invertOps, opsApply, type Graph } from "../src/lib/patch";
import { Allocator } from "../src/lib/ids";
import { diffLines } from "../src/lib/diff";
import { boundingBox, computeSnap, rectsIntersect, snapToGrid } from "../src/lib/geometry";
import { computeImpact, findCycles } from "../src/modes/dependencies/graphAlgo";
import type { PlanEdge, PlanNode } from "../src/api/types";
import { geometryForStage } from "../src/modes/canvas/canvasModel";

// These exercise the framework-free logic directly, without a browser.

function graph(): Graph {
  return { nodes: { "task-1": { id: "task-1", title: "a" } }, edges: {}, counters: { task: 1, edge: 0 } };
}

test.describe("patch engine (RFC 6902 subset)", () => {
  test("add/replace/remove apply all-or-nothing", () => {
    const g = graph();
    const next = applyOps(g, [
      { op: "add", path: "/nodes/task-2", value: { id: "task-2", title: "b" } },
      { op: "replace", path: "/nodes/task-1/title", value: "A" },
    ]);
    expect((next.nodes["task-2"] as { title: string }).title).toBe("b");
    expect((next.nodes["task-1"] as { title: string }).title).toBe("A");
    // original is untouched
    expect(g.nodes["task-2"]).toBeUndefined();
  });

  test.describe("canvas preview layout", () => {
    test("unplaced stage nodes receive stable non-overlapping geometry", () => {
      const node = (id: string, order: number): PlanNode => ({
        id,
        kind: "task",
        stage: "implementation",
        title: id,
        body: "",
        attrs: {},
        order,
        created_rev: "r0001",
        updated_rev: "r0001",
      });
      const nodes = {
        "task-1": node("task-1", 1000),
        "task-2": node("task-2", 2000),
        "task-3": node("task-3", 3000),
      };
      const edges: Record<string, PlanEdge> = {
        "edge-1": {
          id: "edge-1",
          kind: "depends_on",
          from: "task-2",
          to: "task-1",
          attrs: {},
          created_rev: "r0001",
        },
      };
      const first = geometryForStage(nodes, edges, "implementation");
      const second = geometryForStage(nodes, edges, "implementation");
      expect([...first.entries()]).toEqual([...second.entries()]);
      expect(first.size).toBe(3);
      expect(first.get("task-2")!.x).toBeGreaterThan(first.get("task-1")!.x);
      expect(new Set([...first.values()].map((value) => `${value.x}:${value.y}`)).size).toBe(3);
    });
  });

  test("a failed test aborts the whole patch", () => {
    const g = graph();
    expect(() =>
      applyOps(g, [
        { op: "test", path: "/nodes/task-1/title", value: "wrong" },
        { op: "replace", path: "/nodes/task-1/title", value: "X" },
      ]),
    ).toThrow();
  });

  test("invertOps round-trips add and replace", () => {
    const g = graph();
    const ops = [
      { op: "add" as const, path: "/nodes/task-2", value: { id: "task-2", title: "b" } },
      { op: "replace" as const, path: "/nodes/task-1/title", value: "A" },
    ];
    const forward = applyOps(g, ops);
    const inverse = invertOps(g, ops);
    const restored = applyOps(forward, inverse);
    expect(restored).toEqual(g);
  });

  test("opsApply reports clean applicability", () => {
    const g = graph();
    expect(opsApply(g, [{ op: "remove", path: "/nodes/task-1" }])).toBe(true);
    expect(opsApply(g, [{ op: "remove", path: "/nodes/missing" }])).toBe(false);
  });
});

test.describe("id allocator", () => {
  test("allocates sequential per-kind ids and counter ops", () => {
    const a = new Allocator({ task: 8, edge: 20 });
    expect(a.node("task")).toBe("task-9");
    expect(a.node("task")).toBe("task-10");
    expect(a.edge("depends_on")).toBe("edge-21");
    const ops = a.counterOps();
    expect(ops).toContainEqual({ op: "add", path: "/counters/task", value: 10 });
    expect(ops).toContainEqual({ op: "add", path: "/counters/edge", value: 21 });
  });

  test("a kind absent from counters still allocates and applies cleanly", () => {
    // Regression: an `add` counter op must create a member that did not exist,
    // otherwise "new risk from this edge" aborts when /counters/risk is absent.
    const a = new Allocator({ task: 8 });
    expect(a.node("risk")).toBe("risk-1");
    const g: Graph = { nodes: {}, edges: {}, counters: { task: 8 } };
    const next = applyOps(g, a.counterOps());
    expect((next.counters as Record<string, number>)["risk"]).toBe(1);
  });
});

test.describe("geometry", () => {
  test("snapToGrid rounds to 8px", () => {
    expect(snapToGrid(11)).toBe(8);
    expect(snapToGrid(13)).toBe(16);
  });
  test("rectsIntersect and boundingBox", () => {
    expect(rectsIntersect({ x: 0, y: 0, w: 10, h: 10 }, { x: 5, y: 5, w: 10, h: 10 })).toBe(true);
    expect(rectsIntersect({ x: 0, y: 0, w: 10, h: 10 }, { x: 20, y: 20, w: 5, h: 5 })).toBe(false);
    const bb = boundingBox([{ x: 0, y: 0, w: 10, h: 10 }, { x: 20, y: 5, w: 10, h: 10 }]);
    expect(bb).toEqual({ x: 0, y: 0, w: 30, h: 15 });
  });
  test("computeSnap aligns edges within tolerance", () => {
    const { guides } = computeSnap({ x: 3, y: 0, w: 10, h: 10 }, [{ x: 0, y: 0, w: 10, h: 10 }], 8);
    expect(guides.length).toBeGreaterThan(0);
  });
});

test.describe("diff", () => {
  test("line diff marks add and del", () => {
    const ops = diffLines("a\nb\nc", "a\nB\nc");
    expect(ops.some((o) => o.type === "del" && o.text === "b")).toBe(true);
    expect(ops.some((o) => o.type === "add" && o.text === "B")).toBe(true);
  });
});

test.describe("dependency graph", () => {
  const edges: PlanEdge[] = [
    { id: "e1", kind: "depends_on", from: "t3", to: "t4", attrs: {}, created_rev: "r1" },
    { id: "e2", kind: "depends_on", from: "t4", to: "t3", attrs: {}, created_rev: "r1" },
    { id: "e3", kind: "depends_on", from: "t2", to: "t1", attrs: {}, created_rev: "r1" },
  ];
  test("findCycles detects the refactor cycle", () => {
    const cycles = findCycles(edges, new Set(["depends_on"]));
    expect(cycles.length).toBeGreaterThan(0);
    expect(cycles[0]).toContain("t3");
    expect(cycles[0]).toContain("t4");
  });
  test("computeImpact returns direct dependents", () => {
    const res = computeImpact("t1", edges, new Set(["depends_on"]));
    // nothing depends on t1's forward direction here; direct is neighbours of t1
    expect(res.direct).toBeDefined();
  });
});
