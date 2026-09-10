# Review fixtures

These files are shared inputs for the review workspace tests. They are not a
test themselves.

- `flowchart.mmd` — a small flowchart exercising node shapes, a labelled edge,
  a self-loop and a subgraph.
- `flowchart.golden.svg` — the SVG produced by the pinned Mermaid build
  (11.17.2) rendering `flowchart.mmd` with `deterministicIds` and the strict
  security level. It guards the DOM-id convention the diagram anchors depend on:
  a Mermaid upgrade that changes `flowchart-<id>-<counter>` node ids or the
  `data-id="L_<from>_<to>_<ordinal>"` edge attribute fails a fast test instead
  of silently unanchoring every diagram comment.
- `stage_with_diagram.md` — a stage body covering the supported Markdown subset
  and embedding the fixture flowchart in a fenced ```mermaid block.
