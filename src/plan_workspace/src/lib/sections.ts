import type { NodeKind, PlanNode, Stage } from "../api/types";
export { humanTitle } from "./humanize";

// Document-order grouping. The compiler lists Directives first, then the rest
// in a stable documented order. The reading surface mirrors that order.
export const SECTION_ORDER: NodeKind[] = [
  "directive",
  "goal",
  "constraint",
  "invariant",
  "decision",
  "criterion",
  "task",
  "risk",
  "question",
  "note",
  "evidence",
  "reference",
  "diagram",
  "region",
];

export const SECTION_LABEL: Record<NodeKind, string> = {
  directive: "Must-follow instructions",
  goal: "What this stage delivers",
  constraint: "Scope and guardrails",
  invariant: "Rules that must remain true",
  decision: "Architecture choices",
  criterion: "Definition of done",
  task: "Implementation roadmap",
  risk: "Risks and mitigations",
  question: "Open decisions",
  note: "Plan sections",
  evidence: "Validation evidence",
  reference: "Sources and references",
  diagram: "Architecture diagrams",
  region: "Annotated regions",
  thread: "Threads",
};

export interface Section {
  kind: NodeKind;
  label: string;
  nodes: PlanNode[];
}


/** Group a stage's nodes into ordered sections, threads excluded (they render
 * as marks, not body content). Nodes within a section are ordered by `order`. */
export function sectionsForStage(nodes: Record<string, PlanNode>, stage: Stage): Section[] {
  const sections: Section[] = [];
  for (const kind of SECTION_ORDER) {
    const group = Object.values(nodes)
      .filter((n) => n.kind === kind && (n.stage === stage || (kind === "directive" && n.stage === "")))
      .sort((a, b) => a.order - b.order);
    if (group.length) sections.push({ kind, label: SECTION_LABEL[kind], nodes: group });
  }
  return sections;
}

/** Active directives addressed to a role or everyone, for the Directives lead. */
export function activeDirectives(nodes: Record<string, PlanNode>, role: string): PlanNode[] {
  return Object.values(nodes)
    .filter((n) => n.kind === "directive")
    .filter((n) => (n.attrs?.["status"] ?? "active") === "active")
    .filter((n) => {
      const audience = n.attrs?.["audience"];
      if (!Array.isArray(audience)) return true;
      return audience.includes("everyone") || (role && audience.includes(role));
    })
    .sort((a, b) => a.order - b.order);
}
