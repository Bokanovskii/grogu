// Source-offset mapping over the rendered stage body. Nothing else lives here.
//
// The rendered document wraps every run of literal source text in a
// <span data-s data-e> whose text content is byte-identical to
// source[data-s:data-e]. A character that had to be HTML-escaped is emitted in
// its own span carrying data-atomic="1", because its escaped form differs in
// length from the source. That byte-identity is what makes selection offsets
// exact rather than approximated.

function mappedSpan(node) {
  let el = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
  while (el && !(el.dataset && el.dataset.s !== undefined)) {
    el = el.parentElement;
  }
  return el;
}

// Characters within `span`'s text content that precede the (node, offset)
// boundary. For a single-text-node span this is just `offset`.
function offsetWithinSpan(span, node, offset) {
  if (node.nodeType !== Node.TEXT_NODE) {
    // Boundary is at an element edge; count text length of children before it.
    let count = 0;
    for (const child of span.childNodes) {
      if (child === node) break;
      count += child.textContent.length;
    }
    return count;
  }
  let count = 0;
  const walker = document.createTreeWalker(span, NodeFilter.SHOW_TEXT);
  let current;
  while ((current = walker.nextNode())) {
    if (current === node) return count + offset;
    count += current.textContent.length;
  }
  return count;
}

function boundaryOffset(node, offset, which) {
  const span = mappedSpan(node);
  if (!span) return null;
  const start = Number(span.dataset.s);
  const end = Number(span.dataset.e);
  if (span.dataset.atomic === "1") {
    // Selection inside an atomic span selects the whole span.
    return which === "start" ? start : end;
  }
  return start + offsetWithinSpan(span, node, offset);
}

export function offsetsFromSelection(root, selection) {
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) {
    return null;
  }
  const start = boundaryOffset(range.startContainer, range.startOffset, "start");
  const end = boundaryOffset(range.endContainer, range.endOffset, "end");
  if (start === null || end === null) return null;
  const lo = Math.min(start, end);
  const hi = Math.max(start, end);
  if (hi <= lo) return null;
  return { start: lo, end: hi };
}

// Every mapped span in the document, in document order.
function mappedSpans(root) {
  return Array.from(root.querySelectorAll("[data-s]"));
}

export function rangeForOffsets(root, start, end) {
  const spans = mappedSpans(root);
  const range = document.createRange();
  let startSet = false;
  let endSet = false;
  for (const span of spans) {
    const s = Number(span.dataset.s);
    const e = Number(span.dataset.e);
    const textNode = span.firstChild && span.firstChild.nodeType === Node.TEXT_NODE
      ? span.firstChild
      : span;
    if (!startSet && start >= s && start < e) {
      const within = span.dataset.atomic === "1" ? 0 : start - s;
      if (textNode.nodeType === Node.TEXT_NODE) range.setStart(textNode, within);
      else range.setStartBefore(span);
      startSet = true;
    }
    if (end > s && end <= e) {
      const within = span.dataset.atomic === "1" ? textNode.textContent.length : end - s;
      if (textNode.nodeType === Node.TEXT_NODE) range.setEnd(textNode, within);
      else range.setEndAfter(span);
      endSet = true;
    }
  }
  if (!startSet || !endSet) return null;
  return range;
}

// Wrap every mapped span overlapping [start, end] in its own <mark>. One mark
// per span means a selection across three table cells produces three marks and
// no mark crosses a cell boundary.
export function highlight(root, thread) {
  const anchor = thread.anchor || {};
  if (thread.anchor_state === "orphaned") return [];
  const start = anchor.start;
  const end = anchor.end;
  if (typeof start !== "number" || typeof end !== "number") return [];
  const marks = [];
  for (const span of mappedSpans(root)) {
    const s = Number(span.dataset.s);
    const e = Number(span.dataset.e);
    if (e <= start || s >= end) continue;
    const textNode = span.firstChild;
    if (!textNode || textNode.nodeType !== Node.TEXT_NODE) continue;
    const from = Math.max(start, s) - s;
    const to = Math.min(end, e) - s;
    if (to <= from) continue;
    const sub = document.createRange();
    sub.setStart(textNode, from);
    sub.setEnd(textNode, to);
    const mark = document.createElement("mark");
    mark.dataset.thread = thread.id;
    mark.setAttribute("tabindex", "0");
    mark.setAttribute("role", "button");
    const quote = (anchor.exact || "").slice(0, 40);
    mark.setAttribute("aria-label", `comment on "${quote}"`);
    if (span.closest("pre")) mark.classList.add("mark-code");
    if (thread.anchor_state === "shifted") mark.classList.add("mark-moved");
    try {
      sub.surroundContents(mark);
      marks.push(mark);
    } catch (error) {
      // A range that cannot be surrounded (rare, spanning element edges) is
      // skipped rather than throwing; the card in the rail still carries the
      // comment.
    }
  }
  return marks;
}
