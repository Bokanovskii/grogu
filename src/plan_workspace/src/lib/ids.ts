import type { Counters, EdgeKind, NodeKind, PatchOp } from "../api/types";

// Per-kind monotonic ids: `<prefix>-<n>`, allocated from the manifest counters
// the client holds. One writer at a time plus a base==HEAD precondition makes
// this safe: on a stale base the client gets 409 and re-allocates during the
// rebase. Each allocation also emits a counter replace op so local state and
// the server advance together and two adds in one gesture never collide.

export const KIND_PREFIX: Record<NodeKind, string> = {
  goal: "goal",
  directive: "dir",
  constraint: "con",
  invariant: "inv",
  decision: "dec",
  criterion: "crit",
  task: "task",
  risk: "risk",
  question: "qn",
  note: "note",
  evidence: "ev",
  reference: "ref",
  diagram: "dia",
  region: "reg",
  thread: "thr",
};

export const EDGE_PREFIX = "edge";

export class Allocator {
  private counters: Record<string, number>;
  private ops: PatchOp[] = [];

  constructor(counters: Counters) {
    this.counters = { ...(counters as Record<string, number>) };
  }

  private bump(prefix: string): number {
    const next = (this.counters[prefix] ?? 0) + 1;
    this.counters[prefix] = next;
    // `add` (not `replace`): RFC 6902 add creates-or-replaces the member, so a
    // kind whose counter was never seeded still allocates cleanly.
    this.ops.push({ op: "add", path: `/counters/${prefix}`, value: next });
    return next;
  }

  node(kind: NodeKind): string {
    const prefix = KIND_PREFIX[kind];
    return `${prefix}-${this.bump(prefix)}`;
  }

  edge(_kind: EdgeKind): string {
    return `${EDGE_PREFIX}-${this.bump(EDGE_PREFIX)}`;
  }

  /** The counter replace ops accumulated so far; prepend/append to the gesture. */
  counterOps(): PatchOp[] {
    return this.ops;
  }
}
