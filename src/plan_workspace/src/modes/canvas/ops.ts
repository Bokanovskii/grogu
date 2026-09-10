import { api } from "../../api/client";
import type { EdgeKind, JsonValue, NodeKind, PatchOp, PlanNode } from "../../api/types";
import { useActions, useApp } from "../../state/store";
import { Allocator } from "../../lib/ids";
import { ri, boundingBox, type Rect } from "../../lib/geometry";

// Node/edge values are structurally JSON but carry optional fields TypeScript
// widens to `| undefined`, which JsonValue excludes. J() narrows the intent:
// "this really is serialisable JSON". It never changes runtime behaviour.
const J = (v: unknown): JsonValue => v as JsonValue;

// All canvas mutations funnel through here so every gesture is exactly one
// revision. Geometry is always integer canvas units. Auto-layout is an explicit
// action whose result is written back as a patch — never something that runs on
// render.
export function useCanvasOps() {
  const state = useApp();
  const actions = useActions();

  const selectedNodeIds = () =>
    state.selection
      .filter((s) => s.type !== "edge" && s.type !== "mark")
      .map((s) => s.id)
      .filter((id) => state.nodes[id]);

  const geomOf = (n: PlanNode): Rect =>
    n.geometry ? { x: n.geometry.x, y: n.geometry.y, w: n.geometry.w, h: n.geometry.h } : { x: 0, y: 0, w: 240, h: 120 };

  return {
    /** Persist positions from a drag gesture: one revision, integer geometry. */
    moveNodes(moves: { id: string; x: number; y: number }[], label = "move") {
      const ops: PatchOp[] = [];
      for (const m of moves) {
        const n = state.nodes[m.id];
        if (!n?.geometry) continue;
        if (ri(m.x) === n.geometry.x && ri(m.y) === n.geometry.y) continue;
        ops.push({ op: "replace", path: `/nodes/${m.id}/geometry`, value: J({ ...n.geometry, x: ri(m.x), y: ri(m.y) }) });
      }
      if (ops.length) void actions.writeGesture(ops, label, "workspace", label);
    },

    /** Commit a drag: geometry moves plus containment reconciliation (a node
     * dropped inside a region gains a `contains` edge; dragged out loses it),
     * all as one revision. */
    dragCommit(moves: { id: string; x: number; y: number }[]) {
      const ops: PatchOp[] = [];
      const alloc = new Allocator(state.counters);
      const regions = Object.values(state.nodes).filter(
        (n) =>
          n.kind === "region" &&
          n.stage === state.stage &&
          n.geometry &&
          n.attrs?.["shape"] !== "arrow",
      );
      for (const m of moves) {
        const n = state.nodes[m.id];
        if (!n?.geometry || n.kind === "region") continue;
        if (ri(m.x) !== n.geometry.x || ri(m.y) !== n.geometry.y) {
          ops.push({ op: "replace", path: `/nodes/${m.id}/geometry`, value: J({ ...n.geometry, x: ri(m.x), y: ri(m.y) }) });
        }
        const cx = m.x + n.geometry.w / 2;
        const cy = m.y + n.geometry.h / 2;
        const inside = regions.find(
          (r) => cx >= r.geometry!.x && cx <= r.geometry!.x + r.geometry!.w && cy >= r.geometry!.y && cy <= r.geometry!.y + r.geometry!.h,
        );
        const existing = Object.values(state.edges).find(
          (edge) =>
            edge.kind === "contains" &&
            edge.to === m.id &&
            state.nodes[edge.from]?.kind === "region" &&
            state.nodes[edge.from]?.stage === state.stage,
        );
        if (inside && (!existing || existing.from !== inside.id)) {
          if (existing) ops.push({ op: "remove", path: `/edges/${existing.id}` });
          const edgeId = alloc.edge("contains");
          ops.push({ op: "add", path: `/edges/${edgeId}`, value: J({ id: edgeId, kind: "contains", from: inside.id, to: m.id, attrs: {}, created_rev: state.base }) });
        } else if (!inside && existing) {
          ops.push({ op: "remove", path: `/edges/${existing.id}` });
        }
      }
      const all = [...alloc.counterOps(), ...ops];
      if (all.length) void actions.writeGesture(all, "move", "workspace", "move");
    },

    nudge(dx: number, dy: number) {
      const ids = selectedNodeIds();
      const moves = ids.map((id) => {
        const g = state.nodes[id]!.geometry!;
        return { id, x: g.x + dx, y: g.y + dy };
      });
      this.moveNodes(moves, "nudge");
    },

    connect(from: string, to: string, kind: EdgeKind = "depends_on") {
      const alloc = new Allocator(state.counters);
      const id = alloc.edge(kind);
      const ops: PatchOp[] = [
        ...alloc.counterOps(),
        { op: "add", path: `/edges/${id}`, value: J({ id, kind, from, to, attrs: {}, created_rev: state.base }) },
      ];
      void actions.writeGesture(ops, `link ${from}→${to}`, "workspace", `link ${from}→${to}`);
    },

    duplicate() {
      const ids = selectedNodeIds();
      if (!ids.length) return;
      const alloc = new Allocator(state.counters);
      const ops: PatchOp[] = [];
      for (const id of ids) {
        const n = state.nodes[id]!;
        const newId = alloc.node(n.kind);
        const g = n.geometry ? { ...n.geometry, x: n.geometry.x + 16, y: n.geometry.y + 16 } : undefined;
        ops.push({
          op: "add",
          path: `/nodes/${newId}`,
          value: J({ ...n, id: newId, ...(g ? { geometry: g } : {}), created_rev: state.base, updated_rev: state.base }),
        });
      }
      void actions.writeGesture([...alloc.counterOps(), ...ops], "duplicate", "workspace", "duplicate");
    },

    group() {
      const ids = selectedNodeIds().filter((id) => state.nodes[id]!.kind !== "region");
      if (ids.length < 1) return;
      const rects = ids.map((id) => geomOf(state.nodes[id]!));
      const bb = boundingBox(rects);
      if (!bb) return;
      const alloc = new Allocator(state.counters);
      const regionId = alloc.node("region");
      const pad = 24;
      const ops: PatchOp[] = [
        ...alloc.counterOps(),
        {
          op: "add",
          path: `/nodes/${regionId}`,
          value: J({
            id: regionId,
            kind: "region",
            stage: state.nodes[ids[0]!]!.stage,
            title: "Group",
            body: "",
            attrs: { shape: "frame" },
            geometry: { x: ri(bb.x - pad), y: ri(bb.y - pad - 32), w: ri(bb.w + pad * 2), h: ri(bb.h + pad * 2 + 32), z: -1 },
            order: 9000,
            created_rev: state.base,
            updated_rev: state.base,
          }),
        },
      ];
      for (const id of ids) {
        const edgeId = alloc.edge("contains");
        ops.push({ op: "add", path: `/edges/${edgeId}`, value: J({ id: edgeId, kind: "contains", from: regionId, to: id, attrs: {}, created_rev: state.base }) });
      }
      void actions.writeGesture(ops, "group into region", "workspace", "group");
    },

    ungroup() {
      const regionIds = state.selection.filter((s) => state.nodes[s.id]?.kind === "region").map((s) => s.id);
      if (!regionIds.length) return;
      const ops: PatchOp[] = [];
      for (const rid of regionIds) {
        for (const e of Object.values(state.edges)) {
          if (e.kind === "contains" && e.from === rid) ops.push({ op: "remove", path: `/edges/${e.id}` });
        }
        ops.push({ op: "remove", path: `/nodes/${rid}` });
      }
      if (ops.length) void actions.writeGesture(ops, "ungroup", "workspace", "ungroup");
    },

    remove() {
      const ops: PatchOp[] = [];
      for (const s of state.selection) {
        if (s.type === "edge" && state.edges[s.id]) ops.push({ op: "remove", path: `/edges/${s.id}` });
        else if (state.nodes[s.id]) ops.push({ op: "remove", path: `/nodes/${s.id}` });
      }
      if (ops.length) {
        void actions.writeGesture(ops, "remove selection", "workspace", "remove");
        actions.clearSelection();
      }
    },

    zOrder(dir: "front" | "back" | "forward" | "backward") {
      const ids = selectedNodeIds();
      if (!ids.length) return;
      const zs = Object.values(state.nodes)
        .filter((n) => n.geometry)
        .map((n) => n.geometry!.z);
      const maxZ = zs.length ? Math.max(...zs) : 0;
      const minZ = zs.length ? Math.min(...zs) : 0;
      const ops: PatchOp[] = ids.map((id) => {
        const g = state.nodes[id]!.geometry!;
        let z = g.z;
        if (dir === "front") z = maxZ + 1;
        else if (dir === "back") z = minZ - 1;
        else if (dir === "forward") z = g.z + 1;
        else z = g.z - 1;
        return { op: "replace", path: `/nodes/${id}/geometry`, value: J({ ...g, z }) };
      });
      void actions.writeGesture(ops, `z-order ${dir}`, "workspace", `z-order ${dir}`);
    },

    async createAnnotation(
      shape: "frame" | "rectangle" | "ellipse" | "arrow" | "freehand",
      rect: Rect,
      points?: number[][],
    ): Promise<string | null> {
      const alloc = new Allocator(state.counters);
      const regionId = alloc.node("region");
      const attrs: Record<string, unknown> = { shape, anchor_state: "resolved" };
      if (points) attrs.points = points.map((p) => [ri(p[0]! - rect.x), ri(p[1]! - rect.y)]);
      const ops: PatchOp[] = [
        ...alloc.counterOps(),
        {
          op: "add",
          path: `/nodes/${regionId}`,
          value: J({
            id: regionId,
            kind: "region",
            stage: state.stage,
            title: shape === "frame" ? "Frame" : `${shape} annotation`,
            body: "",
            attrs,
            geometry: { x: ri(rect.x), y: ri(rect.y), w: ri(Math.max(rect.w, 8)), h: ri(Math.max(rect.h, 8)), z: 5 },
            order: 9500,
            created_rev: state.base,
            updated_rev: state.base,
          }),
        },
      ];
      const persisted = await actions.writeGesture(
        ops,
        `add ${shape}`,
        "workspace",
        `add ${shape}`,
      );
      return persisted ? regionId : null;
    },

    newNodeFromEdge(fromId: string, kind: NodeKind, at: { x: number; y: number }) {
      const alloc = new Allocator(state.counters);
      const nodeId = alloc.node(kind);
      const edgeId = alloc.edge("depends_on");
      const ops: PatchOp[] = [
        ...alloc.counterOps(),
        {
          op: "add",
          path: `/nodes/${nodeId}`,
          value: J({
            id: nodeId,
            kind,
            stage: state.stage,
            title: `New ${kind}`,
            body: "",
            attrs: {},
            geometry: { x: ri(at.x), y: ri(at.y), w: 240, h: 120, z: 0 },
            order: 8000,
            created_rev: state.base,
            updated_rev: state.base,
          }),
        },
        { op: "add", path: `/edges/${edgeId}`, value: J({ id: edgeId, kind: "depends_on", from: fromId, to: nodeId, attrs: {}, created_rev: state.base }) },
      ];
      void actions.writeGesture(ops, `new ${kind} from ${fromId}`, "workspace", `new ${kind}`);
    },

    async autoLayout(direction: "TB" | "LR" = "TB") {
      try {
        const res = await api.layout({
          scope: "stage",
          stage: state.stage,
          algorithm: "dagre",
          direction,
        });
        if (res.ops.length) {
          const applied = await actions.writeGesture(
            res.ops,
            "auto-layout",
            "layout",
            "auto-layout",
          );
          if (applied) actions.toast("Applied auto-layout.", "success");
        }
      } catch {
        actions.toast("Auto-layout failed.", "danger");
      }
    },
  };
}
