import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type { EdgeKind, PlanEdge, PlanNode } from "../../api/types";

// Map the plan graph to xyflow's node/edge model and back. Regions are drawn as
// frames behind their contents by z-order rather than as xyflow subflows, so
// every node keeps ABSOLUTE integer canvas geometry (invariant: integer canvas
// units, no relative-coordinate drift).

export type FlowNodeData = { node: PlanNode } & Record<string, unknown>;

export function nodeType(n: PlanNode): "region" | "card" {
  return n.kind === "region" ? "region" : "card";
}

export function toFlowNodes(nodes: Record<string, PlanNode>, selectedIds: Set<string>): Node<FlowNodeData>[] {
  const out: Node<FlowNodeData>[] = [];
  for (const n of Object.values(nodes)) {
    if (!n.geometry) continue; // only placed nodes appear on the canvas
    if (n.kind === "thread") continue; // threads are marks, not canvas shapes
    out.push({
      id: n.id,
      type: nodeType(n),
      position: { x: n.geometry.x, y: n.geometry.y },
      width: n.geometry.w,
      height: n.geometry.h,
      data: { node: n },
      selected: selectedIds.has(n.id),
      zIndex: n.kind === "region" ? -10 + (n.geometry.z ?? 0) : (n.geometry.z ?? 0),
      draggable: true,
      selectable: true,
    });
  }
  return out;
}

const EDGE_MARKER: Partial<Record<EdgeKind, MarkerType>> = {
  depends_on: MarkerType.ArrowClosed,
  refines: MarkerType.Arrow,
  references: MarkerType.Arrow,
};

export function toFlowEdges(
  edges: Record<string, PlanEdge>,
  nodes: Record<string, PlanNode>,
  selectedIds: Set<string>,
): Edge[] {
  const out: Edge[] = [];
  for (const e of Object.values(edges)) {
    // Only render edges whose endpoints are both placed on the canvas.
    const a = nodes[e.from];
    const b = nodes[e.to];
    if (!a?.geometry || !b?.geometry) continue;
    if (e.kind === "contains" || e.kind === "anchors") continue; // structural, not drawn as connectors
    out.push({
      id: e.id,
      source: e.from,
      target: e.to,
      type: e.kind === "diagram_edge" ? "straight" : "smoothstep",
      selected: selectedIds.has(e.id),
      data: { kind: e.kind },
      className: `edge-kind-${e.kind}`,
      markerEnd: EDGE_MARKER[e.kind] ? { type: EDGE_MARKER[e.kind]! } : undefined,
      label: undefined,
    });
  }
  return out;
}

/** The plausible target kinds when dropping a connector on empty canvas, keyed
 * by the source node's kind. */
export const NEW_NODE_TARGETS: Record<string, PlanNode["kind"][]> = {
  task: ["task", "criterion", "risk"],
  goal: ["task", "criterion"],
  criterion: ["task"],
  directive: ["task"],
  decision: ["task", "risk"],
  region: ["task"],
};
