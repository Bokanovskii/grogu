import type { AnchorState, Comment, PlanNode, ThreadKind, ThreadStatus } from "../../api/types";

// A thread is a node of kind "thread". The server stores its rich shape under
// ext["dev.grogu.thread"] and mirrors the scannable fields into attrs. This
// helper builds a stable view the rail and marks render from.

export interface ThreadView {
  id: string;
  anchorNode: string | undefined;
  kind: ThreadKind;
  status: ThreadStatus;
  anchorState: AnchorState;
  round: number;
  quote: string;
  comments: Comment[];
  resolvedMembers: number | undefined;
  totalMembers: number | undefined;
  lastExactRev: string | undefined;
  promotedDirective: string | undefined;
  revisionRequest:
    | { id: string; state: string; instruction: string; at: number }
    | undefined;
  order: number;
}

export const THREAD_KIND_META: Record<
  ThreadKind,
  { glyph: string; label: string; tone: string }
> = {
  discussion: { glyph: "·", label: "Discussion", tone: "neutral" },
  question: { glyph: "?", label: "Question", tone: "neutral" },
  suggestion: { glyph: "✎", label: "Suggested change", tone: "neutral" },
  blocking: { glyph: "!", label: "Blocking", tone: "warn" },
  directive: { glyph: "▶", label: "Directive", tone: "accent" },
};

function asComments(v: unknown): Comment[] {
  if (!Array.isArray(v)) return [];
  return v as Comment[];
}

export function threadOf(node: PlanNode): ThreadView {
  const ext = (node.ext?.["dev.grogu.thread"] ?? {}) as Record<string, unknown>;
  const attrs = node.attrs ?? {};
  const kind = (ext.kind ?? attrs["thread_kind"] ?? "discussion") as ThreadKind;
  const status = (ext.status ?? attrs["status"] ?? "open") as ThreadStatus;
  const anchorState = (ext.anchor_state ?? attrs["anchor_state"] ?? "resolved") as AnchorState;
  return {
    id: node.id,
    anchorNode: (attrs["anchor_node"] as string) ?? (ext.anchor_node as string) ?? undefined,
    kind,
    status,
    anchorState,
    round: Number(ext.round ?? attrs["round"] ?? 1),
    quote: String(ext.quote_context ?? node.body ?? ""),
    comments: asComments(ext.comments),
    resolvedMembers: ext.resolved_members != null ? Number(ext.resolved_members) : undefined,
    totalMembers: ext.total_members != null ? Number(ext.total_members) : undefined,
    lastExactRev: (ext.anchor_last_exact as string) ?? undefined,
    promotedDirective: (ext.promoted_directive as string) ?? undefined,
    revisionRequest: (attrs["revision_request"] ??
      ext.revision_request) as ThreadView["revisionRequest"],
    order: node.order,
  };
}

export function deriveThreads(nodes: Record<string, PlanNode>): ThreadView[] {
  return Object.values(nodes)
    .filter((n) => n.kind === "thread")
    .map(threadOf)
    .sort((a, b) => a.order - b.order);
}
