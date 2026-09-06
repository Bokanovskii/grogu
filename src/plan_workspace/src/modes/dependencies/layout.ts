import dagre from "@dagrejs/dagre";
import type { PlanEdge, PlanNode } from "../../api/types";

// Deterministic dagre arrangement for the Dependencies view. Nodes and edges
// are sorted by stable id before insertion so the layout is reproducible given
// the same graph (dagre breaks ties by insertion index). This arrangement is a
// VIEW ONLY — it is never written back. The explicit "Apply layout to canvas"
// action is what persists geometry, via /api/layout and a patch.

export interface Arranged {
  positions: Record<string, { x: number; y: number }>;
  width: number;
  height: number;
}

const NODE_W = 200;
const NODE_H = 72;

export function dagreLayout(
  nodes: PlanNode[],
  edges: PlanEdge[],
  direction: "TB" | "LR",
): Arranged {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: direction, nodesep: 40, ranksep: 64, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));

  const sortedNodes = [...nodes].sort((a, b) => a.id.localeCompare(b.id));
  const ids = new Set(sortedNodes.map((n) => n.id));
  for (const n of sortedNodes) {
    g.setNode(n.id, { width: NODE_W, height: NODE_H });
  }
  const sortedEdges = [...edges]
    .filter((e) => ids.has(e.from) && ids.has(e.to))
    .sort((a, b) => a.id.localeCompare(b.id));
  for (const e of sortedEdges) {
    g.setEdge(e.from, e.to);
  }

  dagre.layout(g);

  const positions: Record<string, { x: number; y: number }> = {};
  for (const n of sortedNodes) {
    const gn = g.node(n.id);
    // dagre returns centre coordinates; convert to top-left for xyflow.
    positions[n.id] = { x: Math.round(gn.x - NODE_W / 2), y: Math.round(gn.y - NODE_H / 2) };
  }
  const graph = g.graph();
  return { positions, width: graph.width ?? 0, height: graph.height ?? 0 };
}

export const DEP_NODE_W = NODE_W;
export const DEP_NODE_H = NODE_H;
