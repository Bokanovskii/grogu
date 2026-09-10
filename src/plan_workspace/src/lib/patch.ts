import type { JsonValue, PatchOp } from "../api/types";

// A focused RFC 6902 / RFC 6901 implementation over the id-keyed graph the
// client holds. It is used to (a) apply a gesture's ops optimistically, (b)
// apply the ops the server reports since our base during a rebase, and (c)
// compute the inverse of a gesture so Undo can be a new revision that inverts
// the last one (history stays append-only). It never mutates in place.

export type Graph = {
  nodes: Record<string, JsonValue>;
  edges: Record<string, JsonValue>;
  counters: Record<string, JsonValue>;
};

class PointerError extends Error {}

function unescape(token: string): string {
  return token.replace(/~1/g, "/").replace(/~0/g, "~");
}

function parsePointer(path: string): string[] {
  if (path === "") return [];
  if (path[0] !== "/") throw new PointerError(`bad pointer ${path}`);
  return path.slice(1).split("/").map(unescape);
}

function clone<T>(v: T): T {
  return structuredClone(v);
}

function getParent(root: unknown, parts: string[]): { parent: any; key: string } {
  let node: any = root;
  for (let i = 0; i < parts.length - 1; i++) {
    const t = parts[i]!;
    if (node == null || typeof node !== "object") throw new PointerError(`no path ${t}`);
    node = Array.isArray(node) ? node[Number(t)] : node[t];
  }
  return { parent: node, key: parts[parts.length - 1]! };
}

function getValue(root: unknown, path: string): JsonValue {
  const parts = parsePointer(path);
  let node: any = root;
  for (const t of parts) {
    if (node == null || typeof node !== "object") throw new PointerError(`no path ${t}`);
    node = Array.isArray(node) ? node[Number(t)] : node[t];
    if (node === undefined) throw new PointerError(`missing ${t}`);
  }
  return node as JsonValue;
}

function setValue(root: any, path: string, value: JsonValue): void {
  const parts = parsePointer(path);
  if (parts.length === 0) throw new PointerError("cannot replace root");
  const { parent, key } = getParent(root, parts);
  if (parent == null || typeof parent !== "object") throw new PointerError("bad parent");
  if (Array.isArray(parent)) parent[key === "-" ? parent.length : Number(key)] = value;
  else parent[key] = value;
}

function removeValue(root: any, path: string): JsonValue {
  const parts = parsePointer(path);
  const { parent, key } = getParent(root, parts);
  if (parent == null || typeof parent !== "object") throw new PointerError("bad parent");
  let old: JsonValue;
  if (Array.isArray(parent)) {
    const i = Number(key);
    old = parent[i];
    parent.splice(i, 1);
  } else {
    old = parent[key];
    if (old === undefined) throw new PointerError(`missing ${key}`);
    delete parent[key];
  }
  return old;
}

function has(root: unknown, path: string): boolean {
  try {
    getValue(root, path);
    return true;
  } catch {
    return false;
  }
}

/** Apply ops to a deep copy of the graph, all-or-nothing. Throws on failure. */
export function applyOps(graph: Graph, ops: PatchOp[]): Graph {
  const next = clone(graph) as any;
  for (const op of ops) {
    switch (op.op) {
      case "add":
        setValue(next, op.path, clone(op.value));
        break;
      case "replace":
        if (!has(next, op.path)) throw new PointerError(`replace missing ${op.path}`);
        setValue(next, op.path, clone(op.value));
        break;
      case "remove":
        removeValue(next, op.path);
        break;
      case "test": {
        const actual = getValue(next, op.path);
        if (JSON.stringify(actual) !== JSON.stringify(op.value))
          throw new PointerError(`test failed ${op.path}`);
        break;
      }
      case "move": {
        const v = removeValue(next, op.from);
        setValue(next, op.path, v);
        break;
      }
      case "copy": {
        const v = clone(getValue(next, op.from));
        setValue(next, op.path, v);
        break;
      }
    }
  }
  return next as Graph;
}

/** Can these ops apply cleanly to this graph? Used during a rebase. */
export function opsApply(graph: Graph, ops: PatchOp[]): boolean {
  try {
    applyOps(graph, ops);
    return true;
  } catch {
    return false;
  }
}

/** Compute the inverse of a forward op list against the pre-state, so Undo can
 * post it as a new revision. Supports add/replace/remove — the ops gestures
 * produce. Inverse ops are returned in reverse order. */
export function invertOps(pre: Graph, ops: PatchOp[]): PatchOp[] {
  const inverse: PatchOp[] = [];
  let cursor: Graph = clone(pre);
  for (const op of ops) {
    if (op.op === "add") {
      const existed = has(cursor, op.path);
      if (existed) {
        const old = getValue(cursor, op.path);
        inverse.push({ op: "replace", path: op.path, value: old });
      } else {
        inverse.push({ op: "remove", path: op.path });
      }
    } else if (op.op === "replace") {
      const old = getValue(cursor, op.path);
      inverse.push({ op: "replace", path: op.path, value: old });
    } else if (op.op === "remove") {
      const old = getValue(cursor, op.path);
      inverse.push({ op: "add", path: op.path, value: old });
    } else if (op.op === "move") {
      inverse.push({ op: "move", from: op.path, path: op.from });
    }
    // advance cursor so multi-op inverses see the right intermediate state
    cursor = applyOps(cursor, [op]);
  }
  return inverse.reverse();
}
