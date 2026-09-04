// Diagram interaction: render a Mermaid block, and resolve a stored semantic
// anchor to an element in the produced SVG. The element-id conventions are read
// from the pinned Mermaid build (see docs/review.md) rather than recalled.

let mermaidModule = null;
let mermaidLoadFailed = false;

async function loadMermaid() {
  if (mermaidModule) return mermaidModule;
  if (mermaidLoadFailed) return null;
  try {
    const mod = await import("/vendor/mermaid.min.js");
    mermaidModule = mod.default || window.mermaid || mod;
    return mermaidModule;
  } catch (error) {
    mermaidLoadFailed = true;
    return null;
  }
}

// Remove every script, foreignObject, external use, on* attribute and every
// href whose value is not a same-document fragment. This is why DOMPurify is
// not vendored: sanitisation here is a closed ~30-line allowlist.
function sanitizeSvg(svgText) {
  const doc = new DOMParser().parseFromString(svgText, "image/svg+xml");
  const svg = doc.documentElement;
  if (svg.querySelector("parsererror")) return null;
  const walker = doc.createTreeWalker(svg, NodeFilter.SHOW_ELEMENT);
  const toRemove = [];
  let node = svg;
  do {
    const tag = node.tagName ? node.tagName.toLowerCase() : "";
    if (tag === "script" || tag === "foreignobject") {
      toRemove.push(node);
      continue;
    }
    for (const attr of Array.from(node.attributes || [])) {
      const name = attr.name.toLowerCase();
      const value = attr.value.trim();
      if (name.startsWith("on")) {
        node.removeAttribute(attr.name);
      } else if (name === "href" || name === "xlink:href") {
        if (!value.startsWith("#")) node.removeAttribute(attr.name);
      } else if (tag === "use" && (name === "href" || name === "xlink:href")) {
        if (!value.startsWith("#")) toRemove.push(node);
      }
    }
  } while ((node = walker.nextNode()));
  for (const el of toRemove) el.remove();
  return svg;
}

export async function renderDiagram(container, source, index) {
  const mermaid = await loadMermaid();
  if (!mermaid) return false;
  const seed = container.dataset.blockDigest || `block-${index}`;
  try {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      flowchart: { htmlLabels: false },
      deterministicIds: true,
      deterministicIDSeed: seed,
    });
    const { svg } = await mermaid.render(`grogu-diagram-${index}`, source);
    const sanitized = sanitizeSvg(svg);
    if (!sanitized) return false;
    container.replaceChildren(document.importNode(sanitized, true));
    return true;
  } catch (error) {
    return false;
  }
}

export function elementForNode(svg, nodeId) {
  const pattern = new RegExp(`(?:^|-)flowchart-${escapeId(nodeId)}-\\d+$`);
  const nodes = svg.querySelectorAll("g.node, [id]");
  for (const el of nodes) {
    if (el.id && pattern.test(el.id)) {
      return el.closest("g.node") || el;
    }
  }
  return null;
}

export function elementForEdge(svg, edge) {
  // Prefer [data-id]; fall back to id suffix L_from_to_ordinal; fall back to
  // the n-th flowchart-link by declaration order.
  const from = edge.from;
  const to = edge.to;
  const ordinal = edge.pair_ordinal || 0;
  const byData = svg.querySelector(`path[data-id$="_${from}_${to}_${ordinal}"], [data-id="${from}_${to}"]`);
  if (byData) return byData;
  const bySuffix = svg.querySelector(`[id$="L_${from}_${to}_${ordinal}"], [id$="L-${from}-${to}-${ordinal}"]`);
  if (bySuffix) return bySuffix;
  const links = svg.querySelectorAll("path.flowchart-link, path.relation, .edgePath path");
  const n = edge.edge_index || 0;
  return links[n] || null;
}

function escapeId(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
