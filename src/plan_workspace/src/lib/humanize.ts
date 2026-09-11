import type { EdgeKind } from "../api/types";

export function humanTitle(value: string): string {
  return value
    .replace(/^[A-Z]{1,3}\d+[A-Za-z]?(?:\s*[—–:-]\s*|\s+)/u, "")
    .replace(/\s+/g, " ")
    .trim();
}

export function markdownSummary(markdown: string, limit = 220): string {
  const text = markdown
    .replace(/^[A-Z]{1,3}\d+[A-Za-z]?\s*[—–-]\s*/gmu, "")
    .replace(
      /Architect implementation,\s*independent testing and quality evaluation for task\s+t-[A-Za-z0-9-]+\.?/giu,
      "",
    )
    .replace(/\btask\s+t-\d{8}-[a-z0-9]+\b/giu, "this workstream")
    .replace(
      /p-\d{8}-[a-z0-9]+\s*\/\s*t-\d{8}-[a-z0-9]+\s*\(([^)]+)\)/giu,
      (_match, name: string) => `${name[0]?.toUpperCase() ?? ""}${name.slice(1)} plan`,
    )
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/[#[\]>*_`|()~-]/g, " ")
    .replace(/\b(?:backend|frontend|src)\/[A-Za-z0-9_./*{}[\]-]+/g, "implementation files")
    .replace(/\s+/g, " ")
    .trim();
  const sentences = text
    .split(/(?<=[.!?])\s+/)
    .filter(
      (sentence) =>
        !/^Files?(?: coordinated| changed|:)/i.test(sentence) &&
        (sentence.match(/implementation files/g)?.length ?? 0) < 2 &&
        !/^(Exact paths|Modules?|Migrations?):/i.test(sentence),
    );
  const readable = (sentences.length ? sentences.join(" ") : text).trim();
  if (readable.length <= limit) return readable;
  const cut = readable.slice(0, limit);
  const boundary = cut.lastIndexOf(" ");
  return `${cut.slice(0, boundary > limit * 0.6 ? boundary : limit).trim()}…`;
}

export const HUMAN_EDGE_LABEL: Partial<Record<EdgeKind, string>> = {
  depends_on: "needs",
  blocks: "blocks",
  refines: "clarifies",
  contains: "groups",
  validates: "checks",
  supersedes: "replaces",
  derives_from: "comes from",
  references: "uses",
  answers: "answers",
  anchors: "attaches to",
  diagram_edge: "connects",
};
