import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type { EdgeKind, PlanEdge, PlanNode, Stage } from "../../api/types";

// Map the plan graph to xyflow's node/edge model and back. Regions are drawn as
// frames behind their contents by z-order rather than as xyflow subflows, so
// every node keeps ABSOLUTE integer canvas geometry (invariant: integer canvas
// units, no relative-coordinate drift).

export type FlowNodeData = { node: PlanNode } & Record<string, unknown>;
export type CanvasGeometry = { x: number; y: number; w: number; h: number; z: number };

export function nodeType(n: PlanNode): "region" | "card" {
  return n.kind === "region" ? "region" : "card";
}

export function toFlowNodes(
  nodes: Record<string, PlanNode>,
  edges: Record<string, PlanEdge>,
  selectedIds: Set<string>,
  stage: Stage,
): Node<FlowNodeData>[] {
  const geometries = geometryForStage(nodes, edges, stage);
  const out: Node<FlowNodeData>[] = [];
  for (const n of Object.values(nodes)) {
    if (n.kind === "thread") continue; // threads are marks, not canvas shapes
    if (n.stage !== stage) continue;
    const geometry = geometries.get(n.id);
    if (!geometry) continue;
    const displayNode = n.geometry ? n : { ...n, geometry };
    out.push({
      id: n.id,
      type: nodeType(n),
      position: { x: geometry.x, y: geometry.y },
      width: geometry.w,
      height: geometry.h,
      data: { node: displayNode, derived: !n.geometry },
      selected: selectedIds.has(n.id),
      zIndex: n.kind === "region" ? -10 + geometry.z : geometry.z,
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
  stage: Stage,
): Edge[] {
  const out: Edge[] = [];
  for (const e of Object.values(edges)) {
    // Render stage-local relationships even before geometry has been saved;
    // the canvas supplies a deterministic preview layout for unplaced nodes.
    const a = nodes[e.from];
    const b = nodes[e.to];
    if (!a || !b) continue;
    if (a.stage !== stage || b.stage !== stage) continue;
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

export function geometryForStage(
  nodes: Record<string, PlanNode>,
  edges: Record<string, PlanEdge>,
  stage: Stage,
): Map<string, CanvasGeometry> {
  const stageNodes = Object.values(nodes)
    .filter((node) => node.stage === stage && node.kind !== "thread")
    .sort((left, right) => left.order - right.order || left.id.localeCompare(right.id));
  const byId = new Map(stageNodes.map((node) => [node.id, node]));
  const dependencies = new Map<string, string[]>();
  for (const edge of Object.values(edges)) {
    if (!byId.has(edge.from) || !byId.has(edge.to)) continue;
    if (edge.kind !== "depends_on" && edge.kind !== "blocks" && edge.kind !== "refines") {
      continue;
    }
    dependencies.set(edge.from, [...(dependencies.get(edge.from) ?? []), edge.to]);
  }
  const rankMemo = new Map<string, number>();
  const rank = (id: string, path = new Set<string>()): number => {
    if (rankMemo.has(id)) return rankMemo.get(id)!;
    if (path.has(id)) return 0;
    const next = new Set(path).add(id);
    const value = Math.min(
      5,
      1 + Math.max(-1, ...(dependencies.get(id) ?? []).map((dep) => rank(dep, next))),
    );
    rankMemo.set(id, value);
    return value;
  };
  const lanes = new Map<number, number>();
  const result = new Map<string, CanvasGeometry>();
  for (const [index, node] of stageNodes.entries()) {
    if (node.geometry) {
      result.set(node.id, node.geometry);
      continue;
    }
    const hasDependencies = dependencies.size > 0;
    const column = hasDependencies ? rank(node.id) : index % 3;
    const row = hasDependencies ? (lanes.get(column) ?? 0) : Math.floor(index / 3);
    if (hasDependencies) lanes.set(column, row + 1);
    result.set(node.id, {
      x: 64 + column * 304,
      y: 64 + row * 168,
      w: 248,
      h: 120,
      z: 0,
    });
  }
  return result;
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
