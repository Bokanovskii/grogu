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

const NODE_W = 248;
const NODE_H = 76;
const EXPANDED_W = 360;
const EXPANDED_H = 164;

export function dagreLayout(
  nodes: PlanNode[],
  edges: PlanEdge[],
  direction: "TB" | "LR",
  expandedIds: ReadonlySet<string> = new Set(),
): Arranged {
  const g = new dagre.graphlib.Graph();
  const connectedIds = new Set(
    edges.flatMap((edge) => [edge.from, edge.to]),
  );
  const connectedNodes = nodes.filter((node) => connectedIds.has(node.id));
  const isolatedNodes = nodes.filter((node) => !connectedIds.has(node.id));
  g.setGraph({
    rankdir: direction,
    nodesep: 28,
    ranksep: 76,
    marginx: 24,
    marginy: 24,
  });
  g.setDefaultEdgeLabel(() => ({}));

  const sortedConnected = [...connectedNodes].sort((a, b) =>
    a.id.localeCompare(b.id),
  );
  const ids = new Set(sortedConnected.map((node) => node.id));
  for (const n of sortedConnected) {
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
  for (const n of sortedConnected) {
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
  const connectedWidth = graph.width ?? 0;
  const connectedHeight = graph.height ?? 0;
  const isolatedGap = 28;
  const isolatedTop = connectedNodes.length ? connectedHeight + 64 : 24;
  const columns = Math.max(
    1,
    Math.min(
      5,
      Math.floor(
        Math.max(connectedWidth, NODE_W) / (NODE_W + isolatedGap),
      ),
    ),
  );
  const sortedIsolated = [...isolatedNodes].sort((a, b) =>
    a.id.localeCompare(b.id),
  );
  for (const [index, node] of sortedIsolated.entries()) {
    positions[node.id] = {
      x: 24 + (index % columns) * (NODE_W + isolatedGap),
      y:
        isolatedTop +
        Math.floor(index / columns) * (NODE_H + isolatedGap),
    };
  }
  const isolatedRows = Math.ceil(sortedIsolated.length / columns);
  const isolatedWidth = sortedIsolated.length
    ? Math.min(sortedIsolated.length, columns) * (NODE_W + isolatedGap) -
      isolatedGap +
      48
    : 0;
  const isolatedHeight = sortedIsolated.length
    ? isolatedTop + isolatedRows * (NODE_H + isolatedGap) - isolatedGap + 24
    : 0;
  return {
    positions,
    width: Math.max(connectedWidth, isolatedWidth),
    height: Math.max(connectedHeight, isolatedHeight),
  };
}

export const DEP_NODE_W = NODE_W;
export const DEP_NODE_H = NODE_H;
