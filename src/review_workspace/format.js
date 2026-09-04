// Pure, DOM-free formatting and anchor-building helpers shared by app.js.
// These are the parts that carry exact copy and the anchor fields the core
// module validates, so they are factored out here and covered directly by
// tests/fixtures/review/format_check.mjs (run with `node`), independent of a
// browser.

// A text anchor must carry the revision and body_digest of the stage body the
// reader is looking at: `grogu_review.add_thread` refuses a POST whose
// body_digest does not match the current body, which is the concurrency guard
// that catches the architect rewriting the stage mid-comment. Stamping the
// digest the reader already holds is what makes comment creation work at all.
export function textAnchor(markdown, start, end, { stage, revision, bodyDigest }) {
  return {
    kind: "text",
    stage,
    revision,
    start,
    end,
    exact: markdown.slice(start, end),
    prefix: markdown.slice(Math.max(0, start - 32), start),
    suffix: markdown.slice(end, end + 32),
    body_digest: bodyDigest,
  };
}

// A Mermaid anchor is semantic; it also carries the block offsets/digest and the
// same revision + body_digest guard as a text anchor.
export function mermaidAnchor(block, item, { stage, revision, bodyDigest }) {
  const anchor = {
    kind: "mermaid",
    stage,
    revision,
    block_index: block.index,
    block_start: block.start,
    block_end: block.end,
    block_digest: block.digest,
    target: item.target,
    body_digest: bodyDigest,
  };
  if (item.target === "node" || item.target === "subgraph") {
    anchor.node_id = item.node_id;
    anchor.label = item.label || item.node_id;
  }
  if (item.target === "edge" && item.edge) {
    anchor.edge = {
      from: item.edge.from,
      to: item.edge.to,
      pair_ordinal: item.edge.pair_ordinal || 0,
      edge_index: item.edge.edge_index,
      // Endpoint labels are stored so the card reads the nodes' labels, not
      // their Mermaid ids, and reads the same as the chip for the same edge.
      from_label: item.edge.from_label || item.edge.from,
      to_label: item.edge.to_label || item.edge.to,
    };
    anchor.label = item.edge.label || "";
  }
  return anchor;
}

// A node comment's card header reads `node · <label>`; an edge comment's reads
// `edge · <from label> → <to label>`, with ` ("<edge label>")` appended when the
// edge is labelled. Both endpoints are the nodes' labels.
export function diagramHeader(anchor, resolveLabel = null) {
  if (anchor.target === "edge" && anchor.edge) {
    const e = anchor.edge;
    const fl = e.from_label || (resolveLabel && resolveLabel(e.from)) || e.from;
    const tl = e.to_label || (resolveLabel && resolveLabel(e.to)) || e.to;
    let head = `edge · ${fl} → ${tl}`;
    if (anchor.label) head += ` ("${anchor.label}")`;
    return head;
  }
  if (anchor.target === "subgraph") return `subgraph · ${anchor.label || anchor.node_id}`;
  if (anchor.target === "node") return `node · ${anchor.label || anchor.node_id}`;
  return "diagram";
}

// A tab whose stage has open comments appends the count with a space either
// side of the separator; at zero the segment is omitted.
export function tabLabel(label, openCount) {
  return openCount ? `${label} · ${openCount}` : label;
}

// The drawer button (narrow viewports) is labelled `Comments — <open count>`.
export function drawerLabel(openCount) {
  return openCount ? `Comments — ${openCount}` : "Comments";
}

// The orphaned revision line names the revision the thread orphaned **at** —
// the stage's current revision when the re-anchor failed — not the revision the
// anchor was created at.
export function orphanedRevision(thread) {
  const history = thread.anchor_history || [];
  for (let i = history.length - 1; i >= 0; i--) {
    if (history[i].state === "orphaned" && history[i].to_revision != null) {
      return history[i].to_revision;
    }
  }
  if (history.length) {
    const last = history[history.length - 1];
    if (last.to_revision != null) return last.to_revision;
  }
  return thread.anchor_revision;
}

export function orphanedRevisionLine(thread) {
  return `orphaned since revision ${orphanedRevision(thread)}`;
}

export function movedRevisionLine(thread) {
  const history = thread.anchor_history || [];
  const last = history[history.length - 1] || {};
  const from = last.from_revision != null ? last.from_revision : "?";
  const to =
    last.to_revision != null
      ? last.to_revision
      : thread.anchor_revision != null
      ? thread.anchor_revision
      : "?";
  let base = `moved · revision ${from} to ${to}`;
  const confidence = thread.anchor_confidence != null ? thread.anchor_confidence : 1;
  if (confidence < 0.95) base += " · close match";
  return base;
}
