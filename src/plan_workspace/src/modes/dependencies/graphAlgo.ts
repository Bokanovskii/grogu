import type { EdgeKind, PlanEdge } from "../../api/types";

// Local, deterministic impact + cycle computation over a chosen edge-kind set.
// Used for the Dependencies-mode highlight and cycle report. The server's
// /api/impact is authoritative for proposal previews; this mirrors it for the
// live view so the client never has to wait to shade a selection.

export interface ImpactResult {
  direct: Set<string>;
  transitive: Map<string, number>; // id -> shortest depth
}

export function computeImpact(
  start: string,
  edges: PlanEdge[],
  kinds: Set<EdgeKind>,
): ImpactResult {
  const adj = new Map<string, string[]>();
  for (const e of edges) {
    if (!kinds.has(e.kind)) continue;
    const list = adj.get(e.from) ?? [];
    list.push(e.to);
    adj.set(e.from, list);
  }
  const direct = new Set(adj.get(start) ?? []);
  const transitive = new Map<string, number>();
  const queue: [string, number][] = [[start, 0]];
  const seen = new Set<string>([start]);
  while (queue.length) {
    const [id, depth] = queue.shift()!;
    for (const next of adj.get(id) ?? []) {
      if (!seen.has(next)) {
        seen.add(next);
        transitive.set(next, depth + 1);
        queue.push([next, depth + 1]);
      }
    }
  }
  return { direct, transitive };
}

/** Find simple cycles in the chosen edge set (Tarjan-style DFS collecting back
 * edges). Returns each cycle as an ordered id list. Deterministic given sorted
 * input. */
export function findCycles(edges: PlanEdge[], kinds: Set<EdgeKind>): string[][] {
  const adj = new Map<string, string[]>();
  const nodes = new Set<string>();
  for (const e of [...edges].sort((a, b) => a.id.localeCompare(b.id))) {
    if (!kinds.has(e.kind)) continue;
    nodes.add(e.from);
    nodes.add(e.to);
    const list = adj.get(e.from) ?? [];
    list.push(e.to);
    adj.set(e.from, list);
  }
  const cycles: string[][] = [];
  const seenCycles = new Set<string>();
  const color = new Map<string, 0 | 1 | 2>();
  const stack: string[] = [];

  function dfs(u: string) {
    color.set(u, 1);
    stack.push(u);
    for (const v of (adj.get(u) ?? []).sort()) {
      if (color.get(v) === 1) {
        // back edge: extract the cycle from the stack
        const idx = stack.indexOf(v);
        if (idx >= 0) {
          const cyc = stack.slice(idx);
          const key = [...cyc].sort().join(",");
          if (!seenCycles.has(key)) {
            seenCycles.add(key);
            cycles.push([...cyc, v]);
          }
        }
      } else if (!color.get(v)) {
        dfs(v);
      }
    }
    stack.pop();
    color.set(u, 2);
  }

  for (const n of [...nodes].sort()) {
    if (!color.get(n)) dfs(n);
  }
  return cycles;
}
