import type { EdgeKind, NodeKind } from "../api/types";

// One selection primitive, used by every mode. A Selection is an ordered set of
// typed ids; order is the order in which items entered the selection.

export type SelItemType = "node" | "edge" | "region" | "handle" | "annotation" | "mark";

export interface SelItem {
  type: SelItemType;
  id: string;
}

export type Selection = SelItem[];

export function selHas(sel: Selection, id: string): boolean {
  return sel.some((s) => s.id === id);
}

export function selReplace(id: string, type: SelItemType): Selection {
  return [{ id, type }];
}

export function selExtend(sel: Selection, id: string, type: SelItemType): Selection {
  if (selHas(sel, id)) return sel;
  return [...sel, { id, type }];
}

export function selToggle(sel: Selection, id: string, type: SelItemType): Selection {
  if (selHas(sel, id)) return sel.filter((s) => s.id !== id);
  return [...sel, { id, type }];
}

export function selIds(sel: Selection): string[] {
  return sel.map((s) => s.id);
}

// Kind glyphs — shape/label carry meaning; colour never alone.
export const NODE_GLYPH: Record<NodeKind, string> = {
  goal: "◐",
  directive: "◆",
  constraint: "▤",
  invariant: "∎",
  decision: "◇",
  criterion: "▽",
  task: "▷",
  risk: "✱",
  question: "?",
  note: "∘",
  evidence: "⌘",
  reference: "◱",
  diagram: "⧉",
  region: "▢",
  thread: "◍",
};

export const EDGE_GLYPH: Record<EdgeKind, string> = {
  depends_on: "⇒",
  blocks: "⊣",
  refines: "⇢",
  contains: "◈",
  validates: "▦",
  supersedes: "⤳",
  derives_from: "↰",
  references: "⇉",
  answers: "✓",
  anchors: "⚓",
  diagram_edge: "⋯",
};

/** Target end-cap glyph by edge kind, per the design's connector spec. */
export const EDGE_ENDCAP: Partial<Record<EdgeKind, string>> = {
  depends_on: "→",
  blocks: "⊣",
  refines: "▷",
  references: "•",
  contains: "◆",
  validates: "▪",
};

export const NODE_LABEL: Record<NodeKind, string> = {
  goal: "Goal",
  directive: "Directive",
  constraint: "Constraint",
  invariant: "Invariant",
  decision: "Decision",
  criterion: "Criterion",
  task: "Task",
  risk: "Risk",
  question: "Question",
  note: "Note",
  evidence: "Evidence",
  reference: "Reference",
  diagram: "Diagram",
  region: "Region",
  thread: "Thread",
};
