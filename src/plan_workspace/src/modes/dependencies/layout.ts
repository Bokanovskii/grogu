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

const NODE_W = 220;
const NODE_H = 72;
const EXPANDED_W = 340;
const EXPANDED_H = 164;

export function dagreLayout(
  nodes: PlanNode[],
  edges: PlanEdge[],
  direction: "TB" | "LR",
  expandedIds: ReadonlySet<string> = new Set(),
): Arranged {
  const g = new dagre.graphlib.Graph();
  g.setGraph({
    rankdir: direction,
    nodesep: 32,
    ranksep: 56,
    marginx: 20,
    marginy: 20,
  });
  g.setDefaultEdgeLabel(() => ({}));

  const sortedNodes = [...nodes].sort((a, b) => a.id.localeCompare(b.id));
  const ids = new Set(sortedNodes.map((n) => n.id));
  for (const n of sortedNodes) {
    g.setNode(n.id, {
      width: expandedIds.has(n.id) ? EXPANDED_W : NODE_W,
      height: expandedIds.has(n.id) ? EXPANDED_H : NODE_H,
    });
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
    const width = expandedIds.has(n.id) ? EXPANDED_W : NODE_W;
    const height = expandedIds.has(n.id) ? EXPANDED_H : NODE_H;
    // dagre returns centre coordinates; convert to top-left for xyflow.
    positions[n.id] = {
      x: Math.round(gn.x - width / 2),
      y: Math.round(gn.y - height / 2),
    };
  }
  const graph = g.graph();
  return { positions, width: graph.width ?? 0, height: graph.height ?? 0 };
}

export const DEP_NODE_W = NODE_W;
export const DEP_NODE_H = NODE_H;
