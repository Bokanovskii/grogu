// Durable regression check for the review workspace's pure formatting and
// anchor-building helpers. Run with: `node tests/fixtures/review/format_check.mjs`
// (exit 0 = pass). It has no dependencies and needs no browser; it guards the
// defects fixed after the designer review:
//
//   d1 — comment anchors must carry body_digest and revision, or every POST
//        /api/threads is refused and no comment is ever stored.
//   d4 — an edge card header reads the node labels, not Mermaid ids; a tab
//        appends its open count with a space either side; the orphaned line
//        names the revision the thread orphaned AT, not the one it was created
//        at.

import {
  textAnchor,
  mermaidAnchor,
  diagramHeader,
  tabLabel,
  drawerLabel,
  orphanedRevision,
  orphanedRevisionLine,
  movedRevisionLine,
} from "../../../src/review_workspace/format.js";

let failures = 0;
function check(name, cond) {
  if (cond) {
    console.log("PASS", name);
  } else {
    failures += 1;
    console.log("FAIL", name);
  }
}
function eq(name, got, want) {
  check(`${name} (got ${JSON.stringify(got)})`, got === want);
}

// d1: text anchor carries body_digest + revision and exact/prefix/suffix.
const md = "0123456789 the engineer must not read the testing plan 0123456789";
const ta = textAnchor(md, 11, 54, { stage: "implementation", revision: 3, bodyDigest: "sha256:deadbeef" });
eq("text anchor body_digest", ta.body_digest, "sha256:deadbeef");
eq("text anchor revision", ta.revision, 3);
eq("text anchor exact", ta.exact, md.slice(11, 54));
check("text anchor prefix <= 32", ta.prefix.length <= 32);
check("text anchor suffix <= 32", ta.suffix.length <= 32);
eq("text anchor kind", ta.kind, "text");

// d1: mermaid anchors (node + edge) carry body_digest, revision, block fields.
const block = { index: 0, start: 902, end: 1411, digest: "sha256:blockhash" };
const nodeAnchor = mermaidAnchor(
  block,
  { target: "node", node_id: "Parse", label: "Parse the plan" },
  { stage: "design", revision: 1, bodyDigest: "sha256:bodyhash" }
);
eq("node anchor body_digest", nodeAnchor.body_digest, "sha256:bodyhash");
eq("node anchor revision", nodeAnchor.revision, 1);
eq("node anchor block_digest", nodeAnchor.block_digest, "sha256:blockhash");
eq("node anchor block_start", nodeAnchor.block_start, 902);
eq("node anchor label", nodeAnchor.label, "Parse the plan");

const edgeItem = {
  target: "edge",
  edge: {
    from: "Anchor",
    to: "Review",
    pair_ordinal: 0,
    edge_index: 2,
    from_label: "Anchor comments",
    to_label: "Review round",
    label: "stored",
  },
};
const edgeAnchor = mermaidAnchor(block, edgeItem, {
  stage: "design",
  revision: 1,
  bodyDigest: "sha256:bodyhash",
});
eq("edge anchor body_digest", edgeAnchor.body_digest, "sha256:bodyhash");
eq("edge anchor stores from_label", edgeAnchor.edge.from_label, "Anchor comments");
eq("edge anchor stores to_label", edgeAnchor.edge.to_label, "Review round");

// d4(2): edge card header reads node labels, not Mermaid ids, with the edge
// label in parentheses.
eq(
  "edge header uses labels + edge label",
  diagramHeader(edgeAnchor),
  'edge · Anchor comments → Review round ("stored")'
);
// unlabelled edge omits the parentheses
const plainEdge = mermaidAnchor(
  block,
  { target: "edge", edge: { from: "Parse", to: "Render", from_label: "Parse the plan", to_label: "Render HTML", label: "" } },
  { stage: "design", revision: 1, bodyDigest: "x" }
);
eq("edge header no label omits parens", diagramHeader(plainEdge), "edge · Parse the plan → Render HTML");
// resolveLabel fallback when endpoint labels are missing (e.g. after re-anchor)
const bareEdge = { target: "edge", kind: "mermaid", edge: { from: "A", to: "B", label: "" } };
eq(
  "edge header falls back to resolveLabel",
  diagramHeader(bareEdge, (id) => ({ A: "Alpha", B: "Beta" }[id])),
  "edge · Alpha → Beta"
);
eq("node header format", diagramHeader(nodeAnchor), "node · Parse the plan");

// d4(3): tab label spaces around the separator; drawer label carries the count.
eq("tab with count", tabLabel("Implementation", 3), "Implementation · 3");
eq("tab without count", tabLabel("Design", 0), "Design");
eq("drawer with count", drawerLabel(3), "Comments — 3");
eq("drawer without count", drawerLabel(0), "Comments");

// d4(4): orphaned line names the revision the thread orphaned AT.
const orphanedThread = {
  anchor_revision: 1,
  anchor_state: "orphaned",
  anchor_history: [
    { from_revision: 1, to_revision: 2, state: "shifted" },
    { from_revision: 3, to_revision: 4, state: "orphaned" },
  ],
};
eq("orphaned revision is the orphaning to_revision", orphanedRevision(orphanedThread), 4);
eq("orphaned line copy", orphanedRevisionLine(orphanedThread), "orphaned since revision 4");
// falls back to anchor_revision when there is no history
eq("orphaned revision fallback", orphanedRevision({ anchor_revision: 7, anchor_history: [] }), 7);

// moved line copy and the close-match suffix
const moved = { anchor_state: "shifted", anchor_confidence: 0.86, anchor_history: [{ from_revision: 2, to_revision: 3, state: "shifted" }] };
eq("moved line with close match", movedRevisionLine(moved), "moved · revision 2 to 3 · close match");
const movedExact = { anchor_state: "shifted", anchor_confidence: 1.0, anchor_history: [{ from_revision: 2, to_revision: 3, state: "shifted" }] };
eq("moved line exact", movedRevisionLine(movedExact), "moved · revision 2 to 3");

if (failures) {
  console.log(`\n${failures} FAILED`);
  process.exit(1);
}
console.log("\nALL PASS");
