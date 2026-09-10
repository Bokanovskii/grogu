# The `.plan` document graph and its Figma-style editor — implementation plan

## What this is

A plan stops being a pile of Markdown files and becomes a **typed semantic
graph** stored in `.grogu/plans/<id>.plan/`, edited in a local browser
workspace with document, canvas, dependency, revision and control-room modes,
and **compiled** into deterministic Markdown that every existing role, command
and test keeps reading exactly where it reads today.

Compilation is the load-bearing half of that sentence. The graph is what the
editor manipulates; the compiled Markdown is what every *other* consumer — every
agent, every CLI command, every reviewer reading a pull request — actually
reads. So it is not an export step bolted to the end: it runs inside the write
protocol, it gates whether a revision is written at all, and its faithfulness to
the graph is established by three executable identities rather than asserted.
A design in which the graph can advance while the artifact lags is the failure
this plan exists to prevent.

It builds directly on the interactive review workspace finished on
`integration/p-20260904-6095a8` at commit `1933fb3` (plan `p-20260904-6095a8`).
That plan delivered `grogu_markdown.py` (a Markdown subset renderer that stamps
exact source character offsets on every literal run), `grogu_mermaid.py` (a
Mermaid flowchart parser producing semantic node/edge selectors),
`grogu_review.py` (W3C Web Annotation anchors, the
unmoved → unique quote → context → fuzzy → orphaned re-anchoring ladder,
threads, rounds), `grogu_review_server.py` (a loopback `ThreadingHTTPServer`
with a single-use launch token, session cookie and strict CSP) and
`src/review_workspace/` (the vanilla ES-module front end). **Every one of those
is carried forward.** Nothing in that work is discarded; the parts that were
right are lifted into shared modules and the one part that was a deliberate
limitation — "the workspace is a reader and a comment channel, never an
editor" — is what this plan removes.

> **All work in this plan stacks on `integration/p-20260904-6095a8` (commit
> `1933fb3`), not on `main`.** Grogu's primary checkout stays on `main`; every
> workstream worktree must be created from that integration commit. The exact
> command is in "Branching" below. An engineer who starts from `main` will not
> have `grogu_review.py`, `grogu_markdown.py` or `grogu_mermaid.py` and will
> rebuild them badly.

## Invariants — these are load-bearing

**I1. The graph is the only source of truth. Markdown is compiled output.**
`<stage>.md` and `<stage>.sealed` inside a `.plan` package are *artifacts*. No
code path writes them except the compiler. `PlanStore.write_stage` on a `.plan`
plan **imports** the Markdown into the graph and then recompiles; it never
lands the caller's bytes on disk unchanged. Every compiled file carries its
source revision id and a digest, and `plan doc verify` fails when a compiled
file does not match a fresh compile of its stated revision. There is no second
place a fact about the plan can live.

**I2. Every stage read still goes through `PlanStore.read_stage`.** The seal
is enforced by exactly the code that enforces it today. The document store adds
no second read path, no second unsealing path, and no API that returns a node
whose stage the caller may not read. Sealed stages are sealed **in the graph
partition file as well as in the compiled artifact**: `graph/testing.sealed`
and `graph/evaluation.sealed` are written through `grogu_plans.seal` and are
never loaded into a projection for a role that may not read that stage.

**I3. Append-only history; nothing is ever destroyed.** A revision file, once
written, is immutable. Deleting a node writes a `remove` operation; the node is
still reachable at every earlier revision. A comment whose text the architect
rewrote away is orphaned, never deleted — the vocabulary from
`p-20260904-6095a8` is preserved verbatim.

**I4. One writer at a time, and every write states what it was based on.**
Writes take the plan lock, assert `base == HEAD`, apply to an isolated copy,
validate the *whole* result, then write revision → snapshot → `HEAD` with
`os.replace`. A stale base is a `409` the client rebases from, exactly as the
review server already answers a stale `body_digest` with `409`.

**I5. No agent ever parses prose to find out what it must do.** Every
compiled artifact has a byte-exact JSON twin produced by the same compiler in
the same pass, and `grogu plan doc projection --format json` returns it. The
Markdown is for humans and for context windows; the JSON is for machines. Both
are derived; neither is authoritative. A test asserts they carry the same
facts.

**I6. Compiling is part of writing a revision, not an export of one.** Every
accepted revision is compiled before `HEAD` advances, and a revision whose
projections fail their identities is never written. Same graph + same
projection spec + same compiler version ⇒ byte-identical output, on any
machine, in any order of edits. Every meaningful element renders: nodes,
relationships, active directives, diagrams, validation criteria,
geometry-independent structure, provenance and revision identity, in a stable
documented order, dispatched over a closed kind table with no default branch so
a new kind fails the build instead of vanishing. Bounded projections may
**elide**, never silently: everything dropped is listed by id and count in
`Elided`. Faithfulness is established by three executable identities —
completeness, invertibility and graph equivalence — specified under "The
compiler and projections". The compiled artifact is not a view of the plan; for
every consumer other than the editor, it **is** the plan.

**I7. Nothing leaves the machine.** No runtime network call, ever: the server
binds `127.0.0.1`, the CSP forbids every non-`self` origin, all fonts and
assets are vendored, and no bundled library phones home. Node.js is a
**build-time** tool only; `pip install grogu` / `setup.sh` must never need it.

**I8. `<id>/` and `<id>.plan/` are mutually exclusive.** If both exist for
one plan id, every command refuses with the exact repair step. Migration is a
one-way conversion with a verbatim pre-migration copy kept for `plan doc
revert`. A dual layout is a bug, not a state.

**I9. No new required Python dependency.** Python standard library only, as
every other module in `src/` already is (verified: the only third-party import
anywhere in `src/` is `mcp`, in `grogu_mcp.py`). Python is 3.14.7 here and
`.python-version` pins 3.14.

**I10. Role isolation is not weakened to make the editor nicer.** The
workspace runs as `grogu_plans.current_role()` or `reviewer`. It cannot
approve when a role is set (`PlanStore.approve` already refuses), cannot read a
sealed stage it has no right to, and no `--as-user` flag is added to any
command in this plan.

**I11. The control room shows only what Grogu observes, and says what it
cannot.** No raw tool arguments, tool results, assistant bodies, task
descriptions or model reasoning, no secrets, no sealed-stage content, in any
surface or cache. Every element is labelled `observed` or `authored`. Only
sessions explicitly registered to this plan are read, an allowlist normaliser
drops everything else before it leaves the parser, and coverage degrades to a
stated `unavailable` rather than to a guess. No model is polled for status.

**I12. Serialization is chosen per artifact, and every part declares itself.**
`manifest.json` carries an explicit inventory of every part with its media
type, role, digest and typed relationships. Adding a part in a new format is a
manifest entry, not a change to the verifier. XML appears in exactly one part —
the derived SVG rendering of a diagram — and never in the authoritative path.
Unknown **kinds** fail closed; unknown **`ext` keys** are preserved verbatim
through patching, hashing, compilation and migration. Dropping extension data
is a bug, not a default.

## Research and the decisions it settled

Checked **2026-09-05**. Two research passes covered the canvas/editor stack and
the document/patch/persistence stack; both were told to cite primary sources.
Where research did not settle a question it is listed under "Open questions",
not guessed.

### Canvas and editor libraries

* **tldraw — excluded on licence.** `tldraw@5.4.0` (2026-09-02) is not
  OSI-licensed; npm reports `SEE LICENSE IN LICENSE.md` and GitHub reports
  `NOASSERTION`. The default licence permits development only; production use
  requires a licence key, and the licence defines production as providing
  functionality "to end users", which a redistributed local server does.
  Hobby keys additionally require a "made with tldraw" watermark, and the v5.4.0
  tag ships two official documents that **disagree** about whether hobby/trial
  keys transmit telemetry. <https://github.com/tldraw/tldraw/blob/v5.4.0/LICENSE.md>,
  <https://tldraw.dev/releases/v4.0.0> (2025-09-18, the release that made all
  production use key-gated). **Decision: do not use tldraw.** Against I7 and
  against shipping in an MIT repository.

* **Excalidraw — excluded on architecture, not licence.**
  `@excalidraw/excalidraw@0.18.1` is MIT and works offline once its fonts are
  self-hosted (<https://docs.excalidraw.com/docs/@excalidraw/excalidraw/installation#self-hosting-fonts>).
  But its scene is a fixed whiteboard element model with no documented
  custom-shape API comparable to tldraw's `ShapeUtil`, so plan semantics would
  have to live *outside* the scene and geometry *inside* it — precisely the dual
  source of truth I1 forbids. Its last feature release was `0.18.0` on
  2025-03-11; `0.18.1` (2026-04-20) was a Mermaid XSS backport
  (CVE-2025-54881). **Decision: no.**

* **React Flow (`@xyflow/react@12.11.6`, 2026-09-01, MIT) — adopted for both
  the canvas and the dependency modes.** MIT with no key, no account, no
  runtime service. It renders node interiors as real HTML with `tabIndex`,
  ARIA role/label config, tab-through of nodes and edges, Enter/Space select,
  Escape deselect, arrow-key movement and focus auto-panning, and it draws
  edges as SVG paths with a transparent 20px interaction stroke.
  <https://github.com/xyflow/xyflow/blob/%40xyflow/react%4012.11.6/LICENSE>,
  <https://reactflow.dev/learn/layouting/layouting>. Custom node types are
  ordinary React components, which is exactly the shape of "rich plan shapes":
  a goal, a constraint, a decision, a task, a risk, a region and a diagram are
  seven custom node types, not seven vector primitives. Subflows give Figma-like
  frames. **The attribution badge stays visible.** The MIT text imposes no
  attribution-in-UI clause and `hideAttribution` is not enforced, but the
  project asks users to subscribe before removing it
  (<https://reactflow.dev/remove-attribution>) and we are not paying. Do not
  hide it.

* **Canvas/WebGL scene graphs (Konva 10.3.3, Fabric.js 7.4.0, PixiJS 8.20.1 —
  all MIT) — not used.** All three draw to a bitmap with no per-object
  accessibility tree: "The `<canvas>` element on its own is just a bitmap and
  does not provide information about any drawn objects"
  (<https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/canvas>).
  A plan editor whose contents are invisible to a screen reader fails the
  accessibility bar below, and the DOM+SVG route is documented as comfortable
  into the low thousands of elements (JointJS measured acceptable unoptimised
  performance at ~11,000 SVG DOM elements, 2026-05-08,
  <https://www.jointjs.com/blog/jointjs-performance-overview-testing-diagrams-with-100-000-nodes>).
  A plan has tens to low hundreds of nodes. **Decision: HTML interiors + SVG
  connectors, with viewport culling.** Revisit only if profiling proves
  otherwise, and then only with a synchronised semantic DOM.

* **Graph layout: `@dagrejs/dagre@3.1.1` (2026-08-08, MIT), not elkjs.**
  elkjs 0.12.0 is the more capable engine but is licensed
  `EPL-2.0 OR GPL-3.0-or-later` and is 466 KB gzipped; putting copyleft code
  into a vendored bundle inside an MIT repository is a licence question we do
  not need to answer. Dagre is MIT, 16.9 KB gzipped, contains no PRNG, and
  breaks ordering ties by insertion index
  (<https://github.com/dagrejs/dagre/blob/v3.1.1/lib/order/sort.ts>), so it is
  reproducible **given canonical insertion order** — which we control. Neither
  engine promises byte-stable output across upgrades. **Decision:** sort nodes
  and edges by stable id before insertion, pin the exact version, and — this is
  the part that removes the risk entirely — **auto-layout is an explicit user
  action whose result is written back into the graph as a patch.** Layout runs
  on demand, not on render, so a library upgrade can never silently move a
  saved plan.

* **React 19.2.8 (MIT, 2026-07-21) and Vite 8.2.2 (MIT, 2026-08-20).** Vite 8
  requires Node `^20.19.0 || >=22.12.0`; this machine has Node v24.19.0 and npm
  11.17.0. <https://vite.dev/blog/announcing-vite8>. Preact was considered and
  rejected: `@xyflow/react` peers on React 19 and the ~5 KB saving is not worth
  compatibility uncertainty. Svelte was considered and rejected: it would mean
  not using React Flow, which is the single largest piece of leverage here.

### Document, patch and persistence stack

* **No rich-text framework. No ProseMirror, Tiptap, Lexical, Milkdown or
  BlockNote.** All are viable offline and MIT except BlockNote (MPL-2.0 core,
  `GPL-3.0 OR PROPRIETARY` extensions). The disqualifying problem is shared:
  each brings its own document model, so a node body would exist as both
  Markdown text and editor JSON, and conversion between them is lossy in
  documented ways (ProseMirror discards content whose node type is absent from
  the active schema; <https://prosemirror.net/docs/guide/#schema>). That is I1
  violated inside every paragraph. **Decision:** a node body is
  Markdown-subset **text**, edited in a plain textarea with a formatting
  toolbar that inserts syntax and a live preview rendered by the existing
  `grogu_markdown.render_document`. The payoff is not only avoided
  dependencies: `render_document` already stamps exact source character offsets
  on every literal run, which is what the existing text anchors are built on,
  so comment anchoring keeps working unchanged. The cost — no WYSIWYG — is
  accepted and must be stated to the designer.

* **No CRDT. Single-writer lock + base-revision precondition + append-only
  log.** Yjs 13.6.32, Automerge 3.4.1 and Loro 1.15.1 are all MIT and all
  healthy; `y-py` is abandoned in favour of `pycrdt` 0.14.4, and the Automerge
  Python binding is still `0.2.0.dev4`. None of their formats is Git-reviewable,
  and none is canonical bytes. Loro's own "When Not to Use CRDTs"
  (<https://loro.dev/docs/concepts/when_not_crdt>, added 2025-08-08) says
  plainly that CRDTs cannot enforce hard invariants or exclusive ownership and
  recommends a single writer or an event-sourced ledger instead. This system is
  one machine, one user, occasional concurrent agents, and Git auditability is
  binding. **Decision: no CRDT.** Revisit only if independent offline replicas
  become real.

* **RFC 6902 JSON Patch, implemented in-house.** Merge Patch (RFC 7396, which
  obsoletes RFC 7386) cannot express ordered operations, cannot touch array
  elements, has no `test`, and cannot distinguish removing a member from setting
  it to `null` — all four matter here. `jsonpatch` on PyPI is 1.33 from
  2023-06-16 with a dormant mainline; `fast-json-patch` has had no release since
  2022. Against I9 we implement the subset we need (`add`, `remove`, `replace`,
  `test`, `move`, `copy`) in `grogu_planpatch.py`, ~200 lines, and test it
  against the worked examples in **RFC 6902 Appendix A**
  (<https://www.rfc-editor.org/rfc/rfc6902.html#appendix-A>) plus bounds,
  pointer-escaping (RFC 6901 `~0`/`~1`), and failed-`test` atomicity cases.

* **Ids: per-kind monotonic counters, not UUIDv7.** RFC 9562 (May 2024) is the
  standard and `uuid.uuid7()` did land in Python 3.14
  (<https://docs.python.org/3.14/library/uuid.html#uuid.uuid7>), so it is
  available — and it is still the wrong choice. Allocation here is serialised by
  one lock, so the distributed-uniqueness property buys nothing, while a
  36-character opaque id in every compiled heading and every agent's context
  window costs a great deal. The repository already speaks in `c1`, `a1`, `d1`.
  **Decision:** ids are `<kind>-<n>` (`dir-3`, `task-12`, `edge-7`), allocated
  from per-kind counters in the manifest, **never reused and never
  renumbered**, including after deletion.

* **Content addressing: SHA-256 over a restricted canonical profile.**
  RFC 8785 JCS (June 2020, Informational) is the model, and Python's
  `json.dumps(sort_keys=True, separators=(",", ":"))` is *not* JCS — it differs
  on number spellings, negative zero, and non-BMP key ordering. Adding
  `rfc8785` (PyPI 0.1.4) would break I9. **Decision:** define
  `canonical:v1` in `grogu_plandoc_canon.py` as a documented **restriction** of
  JCS that our schema makes safe: no floats anywhere (all geometry is integer
  canvas units; all numbers are integers within ±2^53), no `NaN`/infinity, no
  duplicate keys (rejected at parse with `object_pairs_hook`), strings
  NFC-normalised with `unicodedata.normalize`, keys sorted by UTF-16 code unit
  (`sorted(keys, key=lambda k: k.encode("utf-16-be"))`), UTF-8 output, arrays
  order-preserving with unordered sets sorted by id first. Where our profile
  restricts JCS it must say so in a docstring, and a test must assert our output
  equals JCS on a fixture corpus of values inside the restriction.

* **Schema: hand-written validator, published as JSON Schema 2020-12.**
  2020-12 is still the current released dialect (<https://json-schema.org/specification>);
  the IETF working group's `draft-ietf-jsonschema-json-schema-03` (2026-08-26)
  is not a stable dialect and must not be persisted against. `jsonschema`
  4.26.0 is installed in this checkout's `.venv` but is imported by nothing in
  `src/`. **Decision:** `schemas/plan-document.schema.json` is published as a
  2020-12 document for external tooling and documentation; runtime enforcement
  is `grogu_plandoc_schema.py`, stdlib only. A test validates the same fixture
  corpus through both and asserts they agree, skipping if `jsonschema` is
  absent. That keeps a machine-readable schema honest without a runtime
  dependency.

* **Server: `ThreadingHTTPServer` + `fetch` + SSE, no WebSockets.** The
  stdlib docs say `http.server` is "not recommended for production" and
  implements only basic security checks, which is why the existing review server
  hardens it by hand; that hardening is proven and is reused. SSE over
  `text/event-stream` needs no dependency and covers everything we need
  (server → browser change notifications); WebSockets would mean hand-writing
  RFC 6455 framing, which is not advisable. Starlette 1.6.0 + Uvicorn 0.52.4
  (both BSD-3-Clause) were the alternative and are rejected by I9.

* **Anchoring: keep exactly what `p-20260904-6095a8` built.** W3C Web
  Annotation `TextQuoteSelector` + `TextPositionSelector`
  (<https://www.w3.org/TR/annotation-model/#selectors>), 32 characters of
  context, `difflib.SequenceMatcher` for the fuzzy step, and the orphaned
  vocabulary. Nothing new is needed; the only change is that the anchor's scope
  becomes a **node body** rather than a whole stage body, which makes it
  strictly more stable because editing an unrelated node can no longer move it.

* **Packaging built assets.** Runtime package data belongs inside the import
  package and is reached with `importlib.resources`
  (<https://docs.python.org/3.14/library/importlib.resources.html>). There is no
  ecosystem rule for or against committing generated web assets; committing them
  is the documented right answer when a source checkout must run without Node,
  which is exactly I7/I9 here — Grogu is installed by `setup.sh` from a git
  checkout, not from a wheel. **Decision:** commit `src/plan_workspace/dist/`,
  regenerate with `npm ci && npm run build`, and check for drift in the tester's
  checklist.

## The package layout

A document plan lives in `.grogu/plans/<id>.plan/`. Everything a legacy plan
directory had keeps its name and meaning:

```
.grogu/plans/<id>.plan/
  manifest.json          the existing plan manifest, unchanged in shape        [ignored]
  design.md              COMPILED reviewer projection of the design stage      [tracked]
  implementation.md      COMPILED reviewer projection                          [tracked]
  testing.sealed         COMPILED reviewer projection, sealed                  [ignored]
  evaluation.sealed      COMPILED reviewer projection, sealed                  [ignored]
  record.md              written by `plan finalize`, unchanged                 [tracked]
  attachments/           unchanged                                             [tracked]
  revisions/             `PlanStore._keep_revision` stage drafts, unchanged    [ignored]
  review.json            preserved verbatim at migration for rollback          [ignored]
  HEAD                   one line: the current revision id                     [ignored]
  graph/open.json        nodes/edges for design, implementation and plan-level [ignored]
  graph/testing.sealed   sealed partition                                      [ignored]
  graph/evaluation.sealed sealed partition                                     [ignored]
  log/000001.json …      append-only immutable revision envelopes              [ignored]  snapshots/<rev>.json   periodic full materialisations (every 50 revisions)   [ignored]
  proposals/<pid>.json   pending proposed revisions                            [ignored]
  projections/…          role-bounded compiled cache, content-addressed        [ignored]
  legacy/pre-migration/  verbatim copy of the pre-migration directory          [ignored]
```

The tracked set is therefore **unchanged from today**: the compiled stage
Markdown, `record.md` and `attachments/`. `PlanStore.stage_path` keeps
returning `<plan dir>/<stage>.{md,sealed}`, so `plan show`, `plan finalize`,
`grogu review` and every existing test keep working with a one-line change to
`plan_dir()`.

`PlanStore._LOCAL_ONLY` gains `HEAD`, `graph/`, `log/`, `snapshots/`,
`proposals/`, `projections/` and `legacy/`, appended through the existing
in-place upgrade in `_protect_working_state` that `p-20260904-6095a8` added for
`review.json` (`src/grogu_plans.py:1745-1773` on the integration branch).
**Do not rewrite that marker; extend it the same way.** A `git add -A` that
staged `log/` would publish every superseded draft and every actor string.

`plan_dir()` becomes: return `<plans>/<id>.plan` if it exists, else
`<plans>/<id>`; **if both exist, raise** naming both paths and telling the user
to remove the one they do not want (I8).

## Serialization per artifact

The user asked whether XML belongs anywhere in the package, and asked for the
choice to be made **per artifact** rather than assumed once for the whole thing.
Researched 2026-09-05. The answer is a heterogeneous directory package with one
serializer per kind of artifact: **XML is adopted for exactly one part (derived
SVG), declined for three named jobs with recorded triggers, and absent from the
authoritative path.**

| artifact | format | why |
|---|---|---|
| graph partitions | deterministic pretty JSON, nodes and edges as id-keyed maps | data-centric records; id-keyed maps keep RFC 6902 pointer paths stable under insertion, which array indexes do not |
| revision log | one JSON envelope per file | an append-only *file per revision* cannot tear the way an appended JSONL line can, and each revision reviews on its own |
| snapshots | the same JSON profile | one serializer, one hash contract |
| manifest | JSON, extended with an explicit part inventory | OPC's best idea without OPC's container |
| compiled artifact | deterministic Markdown + its JSON twin | document-centric output; Markdown gives by far the best `git diff` for a one-node change |
| node bodies | Markdown-subset text + standoff W3C selectors | mixed content is XML's real strength, but overlapping ranges defeat proper nesting and the selectors already exist |
| extension data | JSON `ext` map keyed by reverse-DNS | what XML namespaces are wanted for, without a second parser |
| diagram source | Mermaid text | line-diffable, already parsed by `grogu_mermaid` |
| **diagram rendering** | **SVG — XML, adopted, derived** | the one part where XML is the right format on merit; never authoritative, regenerable, drift-checked |

**What XML would genuinely have bought, and why it still loses here.** XML's
real advantage is mixed content — text and inline elements interleaved in one
tree (XML 1.0 5th ed., §3.2.2, W3C Recommendation 2008-11-26,
<https://www.w3.org/TR/2008/REC-xml-20081126/#sec-mixed-content>) — plus a
validating and transforming ecosystem JSON has no equal of: XSD 1.1
(Recommendation 2012-04-05) with `key`/`keyref`, which could declare "an edge's
endpoint must be some node's id" in the schema instead of in our validator;
Schematron (ISO/IEC 19757-3:2025, 4th ed., September 2025) for contextual
rules; and XSLT 3.0 / XPath 3.1 (both Recommendations 2017) as a compiler
language. Three things defeat it:

1. **The standard library has none of it.** `xml.etree.ElementTree` supports a
   small XPath 1.0 subset and no XSD, no RELAX NG, no Schematron, no XSLT
   (<https://docs.python.org/3.14/library/xml.etree.elementtree.html>). Under
   I9 we would hand-write the validator either way, so XSD's `keyref` is a
   capability we could not actually use.
2. **Overlapping annotations.** XML requires proper nesting, so two comment
   ranges that overlap need milestones or standoff annotation anyway. We
   already have standoff selectors — the W3C Web Annotation Data Model
   (Recommendation 2017-02-23) — and they handle overlap natively.
3. **Canonicalisation is a wash.** `ElementTree.canonicalize` (added in Python
   3.8) implements **C14N 2.0, which is a W3C Working Group Note (2013-04-11),
   not a Recommendation**; Canonical XML 1.1 (Recommendation 2008-05-02) is not
   in the standard library. Neither canonical form produces a reviewable diff:
   canonicalisation optimises for signature equivalence, not for reading.

**Reconsider XML if, and only if,** node bodies acquire typed inline semantics
— citations, tracked changes, typed terms — that standoff selectors handle
badly. Then make **just the body** an XML part. That is the whole point of
choosing per artifact: the decision is reversible for one part without
disturbing the graph, the log or the compiler.

**What we steal from OOXML, and what we refuse.** ECMA-376 Part 2, the Open
Packaging Conventions (5th edition, December 2021;
<https://ecma-international.org/publications-and-standards/standards/ecma-376/>)
separates an abstract package of typed parts from its physical mapping, with
`[Content_Types].xml` naming each part's media type and `_rels/*.rels` carrying
typed relationships between parts. **Steal the idea:** `manifest.json` gains a
`parts` inventory — every file in the package with its media type, its role, its
digest and its typed relationships (`compiled-from`, `snapshot-of`, `renders`,
`sealed-partition-of`, `imported-from`). That makes `plan doc verify` a
declarative walk rather than a hard-coded list, and it is how a future part in a
new format joins without touching the verifier.

**Refuse the ZIP container, precisely.** A DOCX **is** an OOXML ZIP package of
XML parts, governed by OPC: `[Content_Types].xml` at the root, one or more
WordprocessingML parts, and `_rels/*.rels` relationship parts, all zipped. The
objection is not that it is XML and not that it is a package. It is that OPC
**does not define a unique physical serialization**, so ZIP entry order,
timestamps, compression choices, XML encoding, prefix and whitespace
variations, and locally chosen relationship ids are all producer choices. A
disciplined producer can emit a deterministic DOCX; the format does not
guarantee one, and nothing else in the toolchain will preserve it. Git then
treats the archive as an opaque binary. A plain directory of parts keeps every
OPC benefit — typed parts, declared media types, explicit relationships — and
adds Git-native diffing, so the container is the one thing we drop.

One more thing worth naming honestly: **WordprocessingML is not the mixed
content that makes XML attractive.** `CT_P` and `CT_R` in the Strict `wml.xsd`
are element-only sequences; text lives inside `w:t`. So DOCX is not even a good
demonstration of XML's real strength, and its run model is where much of its
verbosity and diff noise comes from.

**Figma: what is actually known, and what is not.** The UX is modelled on
Figma, so it is worth being exact rather than convenient. Figma's own help page
states that `.fig` is **proprietary**, may change, and should not be consumed by
third-party readers
(<https://help.figma.com/hc/en-us/articles/8403626871063-Save-a-local-copy-of-files>).
**Its current internals are not published, and this plan asserts nothing about
them.** What Figma has published, and all this plan relies on:

* a 2016-11-07 engineering post describing the format **at that time** as a ZIP
  containing FlatBuffers-encoded data
  (<https://www.figma.com/blog/debugging-data-corruption-with-emscripten/>);
* `figma/kiwi`, a real and official schema-based **binary** encoding for trees
  of data — **its README does not state that current `.fig` files use it**, and
  this plan does not claim they do;
* a 2022-10-20 post describing checkpoints as the in-memory file encoded to a
  binary format and compressed
  (<https://www.figma.com/blog/making-multiplayer-more-reliable/>).

Community reverse-engineering of `.fig` exists; it is not a vendor contract and
is not cited here as one. The defensible statement is narrow: **Figma's
published material points to binary encodings, and no published Figma source
describes an XML persistence format.** That is not the same as "Figma does not
store XML", and the difference matters.

The two representations Figma *does* document are a JSON node tree from the
REST API (<https://developers.figma.com/docs/rest-api/file-endpoints/>) and a
live mutable object model rooted at `DocumentNode` in the plugin API — an API
projection and an editor model, neither of which is a file format. What is
worth copying is none of those formats but the **architecture**: their
2019-10-16 post describes a document as `Map<ObjectID, Map<Property, Value>>`
with server-authoritative, CRDT-*inspired* but not decentralised sync
(<https://www.figma.com/blog/how-figmas-multiplayer-technology-works/>), and
the 2022 post describes full checkpoints plus a journal of incremental changes
carrying sequence numbers, replay-verified against byte-identical checkpoints.
That is the append-only log + periodic snapshot + digest-verified replay
designed above, arrived at independently and confirmed by the system whose UX
we are copying.

### Where XML does belong, and what it is declined for

The question is not "XML or JSON" for the package; it is which artifact, and
for what job. Four candidates, decided:

**1. Derived diagram rendering — XML, adopted.** SVG is XML and is the right
format for it. It is a **derived** part: never authoritative, regenerable by
`plan doc compile`, listed in the inventory with a `renders` relationship to
its Mermaid source. It must be deterministic, which means keeping the pinned
Mermaid render settings the review work already established
(`deterministicIds: true`, `securityLevel: 'strict'`,
`flowchart.htmlLabels: false`) and treating an SVG that differs from a fresh
render as drift, exactly like a stale compiled artifact. This is the one place
XML is in the package today and it is there on merit.

**2. DOCX / OOXML import — declined, with the door left open.** Reading a plan
someone wrote in Word is imaginable and cheap-ish to attempt: `zipfile` and
`xml.etree` are both stdlib. It is declined because the input this system
actually receives is Markdown written by an architect or an agent, and because
WordprocessingML's run model would need a normaliser of its own to produce
anything but shredded text. **Trigger to revisit:** a real user actually
arriving with a DOCX plan. When that happens it is an importer that emits
graph patches, plus one `imported-from` part in the inventory — **no change to
the graph, the log, the compiler or any existing part**, which is the entire
reason serialization was chosen per artifact.

**3. XML export for interoperability — declined, and this one is not close.**
There is no consumer. DocBook and DITA are real, maintained document-graph
vocabularies with strong transform toolchains, and nothing in this system's
life reads them. Exporting to a format nobody imports is speculative
generality that would then have to be kept faithful under the same three
identities as the Markdown, doubling the compiler's obligations for no reader.
**Trigger to revisit:** a named consumer that cannot read the JSON twin.

**4. Namespaced extensibility — the real requirement, solved without XML.**
This is XML's strongest remaining claim: namespaces let a third party add
fields to a document without colliding with the owner's vocabulary or being
dropped. That requirement is genuine here — a plugin, a downstream tool or a
future Grogu feature will want to hang data on a node — and it interacts badly
with "fail closed on unknown kinds", so it needs an explicit answer rather than
an omission.

**Decision:** every node and edge carries an optional `ext` map whose keys are
reverse-DNS strings (`dev.grogu.review`, `com.example.tool`) and whose values
are opaque JSON. The rules are exact, and they differ from the rules for kinds:

* An **unknown kind** fails closed — it raises, because a node nobody can render
  must not be silently dropped.
* An **unknown `ext` key** is preserved verbatim — it round-trips through
  patching, canonicalisation, compilation and migration untouched. Dropping it
  is a bug, not a default.
* `ext` participates in the canonical digest like any other JSON, so an
  extension cannot change a node without changing its hash.
* `ext` renders in a dedicated `## Extensions` section as one sorted, fenced
  JSON block per node that has one, so **Identity 3 holds exactly** and nothing
  is lost. It is rare, so the artifact stays clean.
* A bare key with no dot is refused, so the namespace is real rather than a
  convention nobody follows.

This gives the property XML namespaces are wanted for, in the format the rest
of the package already speaks, with no parser and no schema language added.

**And the standing reconsideration.** If node bodies ever acquire typed inline
semantics — citations, tracked changes, typed terms — that standoff selectors
handle badly, make **just the body** an XML part with its own media type and
its own `imported-from`/`renders` relationships. The part inventory is what
makes that a local change rather than a migration. That is the whole point of
deciding per artifact, and it is why the answer to "should this be XML" is
recorded as a decision with triggers rather than as a preference.

**One consequence to hold on to:** RFC 6902 stays because the standard library
implements neither it nor RFC 5261 / RFC 7351 (the XML patch framework and its
media type), so both would be hand-written — and RFC 5261's XPath-restricted
selectors are strictly harder to implement correctly than JSON Pointer. If the
graph ever outgrows JSON Patch, the next step is a small domain vocabulary
(`add_node`, `set_body`, `add_edge`), not XML Patch.

## The document model

### Nodes

```json
{
  "id": "dir-3",
  "kind": "directive",
  "stage": "implementation",
  "title": "Markdown is compiled, never authored",
  "body": "…Markdown-subset text…",
  "attrs": {"binding": "must", "audience": ["engineer"], "status": "active"},
  "ext": {"dev.grogu.review": {"thread": "thr-2"}},
  "geometry": {"x": 240, "y": -80, "w": 320, "h": 160, "z": 3},
  "order": 3000,
  "created_rev": "r0007",
  "updated_rev": "r0031"
}
```

`stage` is one of the four stages or `""` for plan-level. `order` is an integer
document-order key, spaced by 1000 on insert so a reorder is one `replace`.
`geometry` is integers in canvas units (1 unit = 1 CSS pixel at 100% zoom) and
is **absent** for nodes never placed on the canvas.

The closed kind table, and whether each is normative — this table is the *only*
place the directive/discussion distinction is decided, and the compiler reads
it rather than restating it:

| kind | prefix | normative | meaning |
|---|---|---|---|
| `goal` | `goal` | yes | what the change must achieve |
| `directive` | `dir` | yes | an instruction the audience must carry out |
| `constraint` | `con` | yes | a bound on acceptable solutions |
| `invariant` | `inv` | yes | a property that must hold at all times |
| `decision` | `dec` | yes | a settled choice, with its rejected alternatives |
| `criterion` | `crit` | yes | a validation/acceptance criterion |
| `task` | `task` | yes | a unit of work with an order and dependencies |
| `risk` | `risk` | no | a named failure mode and its mitigation |
| `question` | `qn` | no | an explicitly open question |
| `note` | `note` | no | rationale, background, discussion |
| `evidence` | `ev` | no | a measurement, run, or citation |
| `reference` | `ref` | no | an external source: url, title, date |
| `diagram` | `dia` | no | a Mermaid source block plus its parsed subgraph |
| `region` | `reg` | no | a Figma-style frame grouping nodes |
| `thread` | `thr` | no | a comment thread: selector, comments, status |

`risk` and `question` are non-normative but are **never elided** from a
projection — see the elision ladder. "Non-normative" means "not an
instruction", not "unimportant".

### Edges

```json
{"id": "edge-7", "kind": "depends_on", "from": "task-3", "to": "task-1",
 "attrs": {}, "created_rev": "r0011"}
```

Kinds: `depends_on`, `blocks`, `refines`, `contains`, `validates`,
`supersedes`, `derives_from`, `references`, `answers`, `anchors`,
`diagram_edge`. `contains` is how a `region` holds nodes and how a `diagram`
holds its parsed nodes. `validates` is how a `criterion` binds to what it
proves. `anchors` is how a `thread` binds to what it is about, alongside its
selector. `diagram_edge` is an edge **inside** a diagram: it carries the
Mermaid operator and label in `attrs` and is deliberately unconstrained, for
the reason in the next paragraph.

Edge invariants, enforced by the schema validator on the *whole result* after
every patch: both endpoints exist; no self-edge except on `references` and
`diagram_edge`; `depends_on`, `blocks`, `refines` and `contains` are acyclic;
an edge may not cross a stage boundary into a sealed stage from an unsealed one
except via `references`; `contains` is single-parent.

**`diagram_edge` is exempt from acyclicity and from the self-edge rule, and
that exemption is load-bearing.** (Amendment a1, raised by the engineer and
accepted after verification against the code.) A Mermaid flowchart is a
drawing, not a dependency assertion: `grogu_mermaid.parse` faithfully emits a
self-loop for `Parse -->|retry| Parse` — which the existing fixture
`tests/fixtures/review/stage_with_diagram.md` contains at line 17 — and emits
cycles for any flowchart that draws one. Importing those as `depends_on` would
make migration of a real legacy plan impossible. So diagram edges import as
`diagram_edge`, are shown in the dependency mode visually distinguished as
derived from a diagram, and have their cycles **reported as information rather
than refused**. Promoting a `diagram_edge` to a `depends_on` is an explicit
action that runs the full validation; a promotion that would create a cycle or
a self-edge is refused, naming the cycle. Nothing is lost either way: the
drawing keeps every edge it had.

### Ids

Allocated from `manifest["plandoc"]["counters"]` under the plan lock. Never
reused, never renumbered, including after `remove`. Referenced ids in a
selector, an edge or a compiled artifact are therefore stable for the life of
the plan.

## Selectors

One tagged union, used identically by comment threads, directives that point at
something, and impact queries.

```json
{"type": "node",   "id": "task-3"}
{"type": "text",   "node": "dir-3", "quote": {"exact": "…", "prefix": "…", "suffix": "…"},
                   "position": {"start": 120, "end": 168}, "body_digest": "sha256:…"}
{"type": "object", "id": "task-3", "part": "title|body|attrs.binding"}
{"type": "edge",   "id": "edge-7"}
{"type": "edge",   "from": "task-3", "to": "task-1", "ordinal": 0}
{"type": "region", "id": "reg-2"}
{"type": "composite", "op": "all|any", "members": [ …selectors… ]}
```

* `text` is `grogu_review.text_anchor` unchanged, scoped to a node body, with
  its 32-character context and its `body_digest` precondition. Re-anchor with
  `grogu_review.reanchor_text` unchanged. **Reuse the functions; do not
  reimplement them.**
* `edge` by endpoints carries `ordinal` for parallel edges, matching the
  pair-ordinal convention `grogu_mermaid` already uses.
* `composite` resolves to the union (`any`) or intersection (`all`) of its
  members' resolved node sets. Nesting depth is capped at 4 and a deeper
  selector is refused, not silently truncated.

Resolution returns `{state: "resolved"|"shifted"|"orphaned", nodes: [...],
edges: [...]}`. A selector whose target was deleted becomes `orphaned` and keeps
everything it captured, exactly as an orphaned comment does today.

## Directives versus discussion

The kind table above is the mechanism. What it buys:

* `grogu plan doc projection --role engineer` returns the normative nodes for
  the stages that role may read, in document order, plus every `risk`,
  `question` and unresolved `thread` attached to them, and **omits** `note`,
  `evidence` and `reference` bodies unless `--include all`.
* A directive has `status` (`active`, `satisfied`, `withdrawn`, `superseded`)
  and `audience`. The compiled artifact for a role lists only `active`
  directives addressed to that role or to everyone, under a single
  `## Directives` heading, before anything else. An engineer never has to read
  a paragraph to find out what changed: `plan doc projection --role engineer
  --since r0031 --json` lists directives added, changed or withdrawn since a
  revision.
* Steering notes and accepted amendments **become directives.** When the
  architect accepts an amendment or folds in a steering note on a `.plan` plan,
  the resolution writes a `directive` node with `source: "amendment"` /
  `"steering"` and the originating id in `attrs.origin`. This is the fix for
  harness friction #70 ("a recorded design review does not reach the role that
  has to act on it") inside this feature's own surface: routing is a graph
  edge, not a hope.

## Revisions, patches and snapshots

### The revision envelope

`log/<seq:06d>.json`, immutable once written:

```json
{
  "schema_version": 1,
  "revision": "r0031",
  "seq": 31,
  "parent": "r0030",
  "at": "2026-09-06T10:11:12Z",
  "actor": "charlie",
  "role": "architect",
  "agent": "plan-v2-architect",
  "intent": "add the dependency-impact criterion",
  "origin": "cli|workspace|migration|proposal:pr-4",
  "ops": [ {"op": "add", "path": "/nodes/crit-9", "value": {…}} ],
  "before_digest": "sha256:…",
  "after_digest": "sha256:…"
}
```

`revision` is `r` + zero-padded seq; `before/after_digest` are `canonical:v1`
SHA-256 over the whole partition set (sealed partitions included, computed
before sealing so the digest is stable).

### The write protocol — this is the part to get right

1. Take the plan lock (`PlanStore.locked()`, one `plans.lock` per repository,
   `grogu_platform.exclusive_lock`).
2. Read `HEAD`; refuse if `base != HEAD` with `409` and the current revision.
3. Load the partitions the caller may write; **refuse any op whose path
   targets a stage the caller may not read** (I2 — the seal is a write boundary
   as well as a read one).
4. Apply the ops to a deep copy, all-or-nothing. A failed `test` or a bad
   pointer aborts the whole patch.
5. Validate the **entire** resulting graph: schema, edge invariants, acyclicity,
   id uniqueness, orphan-reference check. Patch validity does not imply graph
   validity.
6. Compile every affected projection **and evaluate the three identities**
   (completeness, invertibility, graph equivalence). If compilation or any
   identity fails, abort — a revision that cannot be compiled into a faithful
   artifact is never written, and the error names the identity, the element and
   the field that diverged. This is the step that makes the compiled Markdown
   core architecture rather than an export: there is no state of the store in
   which the graph has advanced and the artifact has not.
7. Write, in this order, each with `write tmp → fsync → os.replace`:
   `log/<seq>.json`, then the changed `graph/*` partitions, then
   `snapshots/<rev>.json` if `seq % 50 == 0`, then the compiled artifacts,
   then `HEAD`. `HEAD` last means a crash anywhere earlier leaves the previous
   revision intact and the partial files unreferenced.
8. Release the lock; emit an SSE `revision` event.

Recovery, on load: if `HEAD` names a revision with no log file, fall back to the
newest complete log entry and rewrite `HEAD`. If a partition digest does not
match the revision's `after_digest`, rebuild the partition by replaying the log
from the newest snapshot and report what it did. `plan doc verify` runs both
checks without repairing and exits non-zero on any mismatch.

### Snapshots and bounded replay

A full materialisation every 50 revisions bounds a rebuild to 50 patches.
Snapshots are derived and can be deleted at any time; the log is the truth.

## The compiler and projections

### The projection spec

```json
{"role": "engineer", "stages": ["implementation", "design"], "include": "normative",
 "budget_chars": 40000, "since": "", "compiler": 1}
```

`projection_id` is `canonical:v1` SHA-256 of the spec. The cache key is
`(revision, projection_id, compiler_version)`; a cache hit is a file read.

### Ordering — the whole of determinism

1. Sections in a fixed declared order: `Directives`, `Goals`, `Decisions`,
   `Constraints and invariants`, `Tasks`, `Diagrams`, `Validation criteria`,
   `Risks`, `Open questions`, `Discussion`, `Comment threads`, `Extensions`,
   `Elided`, `Provenance`.
2. Within a section, nodes sort by `(stage index, order, id)` — `id` sorts by
   `(prefix, numeric suffix)`, never lexicographically, so `task-9` precedes
   `task-10`.
3. Edges render under their `from` node, sorted by `(kind, to id, id)`.
4. Attribute maps render sorted by key.
5. Line endings are LF; the file ends with exactly one newline; no trailing
   whitespace anywhere.

### The compiled form

```markdown
# <plan title> — implementation plan

> plan p-20260905-aab017 · revision r0031 · projection engineer/implementation ·
> compiler 1 · graph sha256:9f2c… · projection sha256:41ab…

## Directives

- **dir-3** (must · engineer) Markdown is compiled, never authored.
  No code path writes `<stage>.md` except the compiler.
- **dir-7** (should · engineer) Prefer extending `grogu_review.reanchor_text`
  to writing a second re-anchoring ladder.

## Tasks

### task-3 · Implement the patch engine
depends on: task-1
validated by: crit-9

Body prose, verbatim from the node, unchanged byte for byte.

## Provenance

- source revision r0031, written 2026-09-06T10:11:12Z by architect@plan-v2-architect
- graph digest sha256:9f2c…
- projection digest sha256:41ab…
- compiler grogu-plan-compile/1
- elided: 4 note(s), 2 evidence node(s) — note-1, note-4, note-6, note-9, ev-2, ev-3
```

The grammar is closed and documented: headings are
`^(#{2,3}) (?P<id>[a-z]+-\d+) · (?P<title>.*)$`, directive lines are
`^- \*\*(?P<id>[a-z]+-\d+)\*\* \((?P<binding>must|should) · (?P<audience>[^)]*)\) (?P<title>.*)$`,
relationship lines are `^(?P<kind>[a-z ]+): (?P<ids>[a-z]+-\d+(, [a-z]+-\d+)*)$`.
Node bodies are emitted verbatim, indented by nothing, and are the only free
prose.

### Semantic equivalence — three identities, not one

This is the part the steering makes binding, so it is specified as executable
identities rather than as a promise. `grogu_plandoc_compile.py` exports:

* `scope(graph, spec) -> GraphFragment` — the sub-graph a spec selects: the
  nodes, edges and attributes in scope for that role, those stages and that
  `include` setting. Pure, total, and defined by the kind table, not by the
  compiler's opinion.
* `project(fragment, spec) -> ProjectionIR` — the ordered, elided intermediate.
* `render(ir) -> str` — Markdown. `render_json(ir) -> dict` — the JSON twin.
* `parse(markdown) -> ProjectionIR` — the inverse of `render`.
* `lift(ir) -> GraphFragment` — the inverse of `project` for everything
  `project` did not deliberately elide.

**Identity 1 — completeness.** `elements(project(scope(g, s), s)) ∪ elided(…)
== elements(scope(g, s))`. Every in-scope node, edge and attribute either
renders or is named in `Elided`. Nothing is missing and nothing is invented.
This is the identity that answers "faithfully render every meaningful plan
element".

**Identity 2 — invertibility.** `parse(render(ir)) == ir`, and `render_json(ir)`
carries the same ids, kinds, titles, bodies, relationships, statuses,
directive bindings, audiences, criteria, diagram sources, extension maps and
provenance as `render(ir)`.

**Identity 3 — graph equivalence.** `lift(parse(render(project(scope(g, s), s))))
≡ scope(g, s)` minus exactly the elided set, compared **structurally** — node
for node, edge for edge, attribute for attribute, body byte for byte — not by
string similarity.

Identity 2 alone would only prove the two artifacts agree with each other.
Identity 1 and Identity 3 are what make the artifact equivalent to **the source
graph**, which is what the steering requires and what "not a lossy export"
actually means.

**Exhaustiveness, failing closed — for kinds.** `scope`, `project` and
`render` each dispatch over the closed node-kind and edge-kind tables with **no
default branch**. An unknown kind raises; it never falls through to "skip". A
test asserts that adding a kind to the table without adding it to all three
fails the suite. This is why a future node kind cannot become silently
unrendered — the failure mode the steering exists to prevent.

**Preserving, not failing — for extension data.** The opposite rule governs
`ext`: an unrecognised reverse-DNS key is carried through `scope`, `project`,
`render`, `parse` and `lift` untouched, and renders in `Extensions` as a sorted
fenced JSON block, so Identity 3 holds over it exactly. A compiler that dropped
an `ext` key it did not recognise would fail Identity 1 by an element it could
not name, which is precisely the diagnostic wanted.

**Compilation gates the revision.** `--check` is not a separate audit run on
demand: all three identities are evaluated inside the write protocol before
`HEAD` advances, and a revision whose projections do not satisfy them **is
never written**. `GROGU_PLANDOC_STRICT=0` may skip identities 1 and 3 for speed
on a large interactive drag, but identity 2 always runs, and `plan doc compile
--check` and `plan doc verify` always run all three and exit non-zero on the
first divergence, printing the differing field path — not a diff of the whole
file.

`parse` has a second job: it is the **Markdown importer** used by migration and
by `PlanStore.write_stage` on a `.plan` plan. One parser, two uses, so an
importer bug shows up as an equivalence failure in the test suite.

### The compiler is versioned, and its output is a reviewed artifact

`COMPILER_VERSION` is an integer in `grogu_plandoc_compile.py`, is printed in
every provenance block, and is part of every cache key.

* **A golden corpus** lives under `tests/fixtures/plandoc/golden/`: a handful of
  graphs — empty, one node, every node kind, every edge kind, a diagram with a
  self-loop, a deeply nested region, a bounded projection that elides, a
  migrated real plan — each with its expected compiled Markdown and JSON twin
  committed beside it. **Any change to compiler output is a diff in that
  corpus, reviewed like code.** This is the mechanism that makes "excellent"
  maintainable rather than a one-time judgement.
* Bumping `COMPILER_VERSION` requires regenerating the corpus in the same
  commit. A cached projection whose recorded compiler version is not the
  current one is a miss, never a stale hit.

### The cache and consumption contract

The artifact has to be cheap for an agent to consume, so the caching is part of
the design rather than an optimisation left to whoever notices:

* Cache key: `(revision, projection_id, compiler_version)` where
  `projection_id` is `canonical:v1` SHA-256 of the spec. A hit is one file read
  with no parsing and no graph load.
* Every projection carries a `projection digest`. `GET /api/projection` returns
  it as a strong `ETag` and honours `If-None-Match` with `304`, so a running
  agent that re-reads an unchanged plan pays nothing.
* `--since <rev>` returns only what changed — directives added, changed or
  withdrawn, and the nodes whose compiled digest moved — so an agent that has
  already read the plan does not re-read it to find out nothing happened.
* Compiling every projection for a plan is bounded by the write protocol: only
  projections that already exist in the cache are recompiled eagerly; the rest
  are compiled on first request.

### Bounded projections and the elision ladder

When `budget_chars` is set, drop in this fixed order until the budget is met,
recording every drop:

1. `reference` and `evidence` bodies (titles and urls kept).
2. `note` bodies, then `note` nodes entirely.
3. Resolved `thread` nodes.
4. `decision` rationale paragraphs (the decision line itself is normative and
   stays).
5. Bodies of `task` nodes more than `depth` edges from a root goal.

Never dropped: any `directive`, `constraint`, `invariant`, `criterion`, `goal`,
`risk`, `question`, unresolved `thread`, or any relationship. If the budget
cannot be met without dropping a never-dropped node, the compiler **exceeds the
budget and says so** in `Provenance` rather than lying. Elision is deterministic
and reported by id and count.

## Migration

### From a legacy plan directory

`grogu plan doc migrate <id>` (also run automatically, once, with a printed
notice, the first time any `.plan`-aware command opens a legacy plan):

1. Refuse if `<id>.plan` already exists (I8).
2. Copy `<id>/` verbatim to a staging directory, then to
   `<id>.plan/legacy/pre-migration/`.
3. For each written stage, read through `PlanStore.read_stage` as `reviewer`
   (so sealed stages unseal exactly once, through the one legitimate path),
   `parse` it into nodes, and where `parse` cannot recognise structure, keep the
   text as a single `note` node per heading section, titled from the heading.
   **Nothing is dropped**: an unparsed section becomes a node, not a warning.
4. Every Mermaid block in a stage becomes a `diagram` node whose parsed
   `grogu_mermaid` nodes become contained nodes and whose parsed edges become
   `diagram_edge` edges — **not** `depends_on`, because a flowchart may
   legitimately contain a self-loop or a cycle and the existing fixture
   `tests/fixtures/review/stage_with_diagram.md` contains
   `Parse -->|retry| Parse`. Legacy diagrams therefore populate the dependency
   mode without being able to fail migration. Migration never promotes a
   `diagram_edge` to a `depends_on` on its own.
5. Every `review.json` thread becomes a `thread` node with a `text` selector
   re-anchored into the node that now owns its quoted text, keeping its
   comments, status, round membership and orphaned state. `review.json` is left
   in place, untouched, for rollback.
6. Write revision `r0001` with `origin: "migration"`, compile every stage, and
   **assert the compiled reviewer projection is semantically equivalent to the
   original Markdown** — same headings, same node ids, same body text. Report a
   diff and abort the migration if not.
7. `os.replace` the staged package into place, then remove `<id>/` only after
   the package is complete and verified.

`grogu plan doc revert <id>` restores `<id>/` from `legacy/pre-migration/` and
removes `<id>.plan/`, printing what was discarded (the revisions since
migration). `--dry-run` prints the plan of both without touching disk.

### Migration of the Markdown review system

The review system is **not replaced; it is moved into the graph and its surface
is merged.**

* `grogu_review.text_anchor`, `reanchor_text`, `mermaid_anchor`,
  `reanchor_mermaid`, `digest` and the fuzzy helpers move to
  `src/grogu_plandoc_anchor.py` **unchanged in behaviour**, and
  `grogu_review.py` imports them from there. The existing
  `tests/test_grogu_review.py` cases for those functions must pass untouched;
  that is the proof the move was faithful.
* `ReviewStore` threads/rounds become graph `thread` nodes and manifest rounds.
  `grogu review list/comment/reply/resolve/request-changes/status` keep their
  exact CLI shape and output and are re-pointed at the document store for
  `.plan` plans. Their existing tests are re-pointed, not deleted.
* `grogu review <id>` opens the **new** workspace in document mode with the
  comment rail. `src/review_workspace/` and `src/grogu_review_server.py` are
  deleted at the end of workstream `integration`, and only after every
  behaviour in `docs/review.md` is demonstrated in the new surface — the mark
  positioning rule ("each thread card's top is positioned at its mark's vertical
  offset, colliding cards pushed down with at least 8px between them", defect
  d2), the round chip format ("round N · changes requested · 6 open", defect
  d7), the parameterised sealed/unwritten panels (defect d6), document-order
  rail cards (defect d9), and `written` alone deciding whether a body renders
  (defect d5). **These are regressions the tester already found once; finding
  them again is failure.**
* `docs/review.md` is folded into `docs/plan-documents.md`, keeping the pinned
  Mermaid selector conventions section verbatim — it is the only record of how
  the 11.17.2 build names nodes and edges.

## Role isolation and sealed stages

* Partitions: `graph/open.json` holds design, implementation and plan-level
  nodes; `graph/testing.sealed` and `graph/evaluation.sealed` hold theirs and
  are written through `grogu_plans.seal`.
* `PlanDocumentStore.load(role=…)` loads only the partitions
  `ROLE_READABLE_STAGES[role]` allows and **never** decodes the others. An
  engineer's process never holds testing nodes in memory.
* Cross-partition edges: an edge whose other endpoint is in an unreadable
  partition is omitted from the projection, and its existence is reported as a
  count only (`2 relationship(s) to sealed stages`), never as ids or titles.
* The HTTP API applies the same rule at the boundary: `/api/doc` returns the
  role's partitions only, and a patch op whose path names an unreadable
  partition is refused with `403` before it is parsed.
* The workspace's stage tabs still show all four, with the sealed panel text
  parameterised by stage and role (defect d6).

## Concurrency, locking and the browser

* One `plans.lock` per repository, held only for the duration of a write.
* Reads never take the lock; they read `HEAD` then the partitions, and retry
  once if `HEAD` moved underneath them (the same read-snapshot loop
  `ReviewStore._read_stage_snapshot` already uses).
* The browser holds no lock. It sends `base` with every patch, gets `409` with
  the current revision and the ops since its base, replays its own pending
  intent against the new state, and reapplies or shows a conflict card naming
  the nodes involved. Local edits are never discarded silently.
* SSE `/api/events` pushes `revision`, `proposal`, `thread` and `shutdown`
  events so a second window, or a CLI write, updates the open editor.
* A write from the CLI while a workspace is open is normal and must work; the
  test plan covers it.

## The server and the runtime

`src/grogu_plan_server.py`, modelled on `grogu_review_server.py` and reusing its
proven hardening:

* Binds `127.0.0.1` on an ephemeral port; single-use launch token exchanged
  once for a session cookie (defect d10: **the token must be invalidated on
  exchange**; a second use returns `403`); idle timeout; `End session`.
* Strict CSP: `default-src 'none'; script-src 'self'; style-src 'self'
  'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self';
  frame-ancestors 'none'; base-uri 'none'; form-action 'none'`. If a bundled
  library genuinely cannot run under it, relax exactly one directive, keep
  `script-src` free of `'unsafe-inline'` and `'unsafe-eval'`, and record what
  forced it in `docs/plan-documents.md`.
* Every mutating request requires the session cookie **and** an
  `X-Grogu-Token` header echoing the session token, and an `Origin` that
  matches the bound address. No CORS headers are ever emitted.
* Request bodies capped at 256 KB; patch op count capped at 500 per request.
* Static assets served from `src/plan_workspace/dist/` resolved through
  `importlib.resources`, with symlinks refused and no directory listing.

## The web application

`src/plan_workspace/` — sources, config, lockfile and built output all inside
one directory so the workstream owns a single glob.

```
src/plan_workspace/
  package.json  package-lock.json  vite.config.ts  tsconfig.json  index.html
  src/…                     React 19 + TypeScript sources
  dist/                     committed build output, served by the Python server
  dist/LICENSES.txt         aggregated licences of every bundled package
  tests/                    Playwright specs, including the axe scans
```

* Build: `npm ci && npm run build`. Vite `base: "./"`, content hashes disabled
  (`entryFileNames: "app.js"`, `assetFileNames: "[name][extname]"`) so the
  committed output is stable and the drift check is meaningful. No sourcemaps in
  `dist`.
* Runtime dependencies, pinned exactly: `react` 19.2.8, `react-dom` 19.2.8,
  `@xyflow/react` 12.11.6, `@dagrejs/dagre` 3.1.1. **No other runtime
  dependency may be added without an amendment.** Dev: `vite` 8.2.2,
  `typescript`, `@playwright/test` 1.63.0, `@axe-core/playwright` 4.13.0.
* Fonts: system font stack only. No webfont is downloaded or vendored, which
  removes the entire class of offline font failures.
* The app owns **no model**. It holds the graph it fetched, the revision it is
  based on, and pending local intent. Every durable change is a patch to the
  server. There is no client-side store that could disagree with the graph.

### The five modes

Named here so the workstreams and the designer share vocabulary; the designer
owns how they look and behave.

* **Document** — the compiled projection as a reading surface with the comment
  rail, inline marks, and inline editing of node titles and bodies. This is the
  descendant of the review workspace and must not lose anything it did.
* **Canvas** — the Figma-style surface. Nodes as rich shapes by kind, regions
  as frames, drag/resize/marquee/multi-select, snapping, alignment, z-order,
  connect-by-handle, group into region, zoom to fit, and keyboard equivalents
  for all of it. Geometry writes back as patches, debounced, one revision per
  gesture — not one per pointer move.
* **Dependency** — the same graph filtered to `depends_on`/`blocks`/`refines`
  plus `diagram_edge`, with dagre layout, impact highlighting, and cycle
  reporting. A cycle among `diagram_edge`s is information; a cycle among the
  others cannot exist because the validator refuses it.
* **Revision** — the log as a timeline: what changed at each revision, a
  side-by-side compiled diff, and restore-as-proposal (never a destructive
  reset).
* **Control room** — every running agent on every plan, and the one place the
  user answers them. Specified in its own section below.

### Comment threads and "Ask Grogu to revise"

A thread is a `thread` node with a selector. `Ask Grogu to revise` on a thread
creates a **proposal**, not an edit:

```json
{"id": "pr-4", "at": "…", "from_thread": "thr-2", "base": "r0031",
 "why": "…", "ops": [...], "status": "pending|accepted|rejected",
 "decided_at": "", "decided_by": "", "why_not": ""}
```

`GET /api/proposals/pr-4/preview` returns the node-level diff, the compiled
Markdown diff, and the impact set. Accepting applies the ops as an ordinary
revision with `origin: "proposal:pr-4"`. Rejecting records `why_not` and keeps
the proposal forever. A proposal whose `base` is no longer `HEAD` is shown as
`stale` with the conflicting nodes named, and is rebased or abandoned
explicitly — never auto-merged.

### Dependency impact analysis

`impact(selector, depth=∞)` returns, deterministically:

* `direct` — nodes one edge from the selection along
  `depends_on`/`blocks`/`refines`/`contains`/`validates`.
* `transitive` — the closure, with the shortest path to each node.
* `cycles` — any cycle the selection participates in.
* `compiled_delta` — for a proposal or a pending edit, the ids whose compiled
  projection digest would change. This is the honest answer to "what does this
  break": not "what is nearby" but "what actually renders differently".

## The planning control room

The workspace shows one plan. The control room shows **every agent working on
every plan**, and is the one place the user answers them. It is a mode of the
same application and a `grogu plan doc control` CLI view, backed by
`src/grogu_controlroom.py`.

### What Grogu can actually observe — read this before designing anything

This is the single most important constraint and it must be stated in the
interface, not just in this plan. There are **two** passive sources and they
give very different things.

**Source 1 — the Grogu command feed (always available).** `grogu_watch` is
passive: every `grogu` invocation passes through one exit path and records that
it happened: `at`, `command` (**the subcommand name only — never its
arguments**), `role`, `agent`, `plan`, `repository`, `cwd`, `exit`, `pid`,
appended to `$GROGU_HOME/activity.jsonl` and trimmed at 4000 lines
(`src/grogu_watch.py:47-118`). `sessions()` collapses that into one row per
agent keyed by `GROGU_AGENT` with `calls`, `first`, `last`, `last_command`,
`failures`, `roles`, `idle_seconds` and a `working`/`idle`(≥300 s)/`gone`
(≥3600 s) classification (`:120-197`); `board()` joins plan summaries and
`waiting_on_you()` computes what is blocked on the user (`:233-294`).

This source **cannot** see the agent's tool calls, file edits or reasoning. On
it alone, a twenty-minute build and a hung agent are indistinguishable except by
elapsed time.

**Source 2 — the Copilot session event log (present but optional).**
`$COPILOT_HOME/session-state/<session>/events.jsonl` is a JSON-lines file
carrying `subagent.started` / `subagent.completed`,
`tool.execution_start` / `tool.execution_complete` and
`permission.requested` / `permission.completed`, with a top-level `agentId` and
`timestamp` and `data.toolCallId` / `data.toolName`. Its feasibility was proved
before this plan by `.grogu/plans/p-20260826-49fd23/attachments/probe_agent_events.py`,
and the file's existence is verified in this checkout. **This is the only source
that can answer "what tool is it in", "how many tools failed" and "what is it
waiting on", which the steering explicitly asks for.**

It is also a serious privacy surface — the same file carries `reasoningText`,
`reasoningOpaque`, assistant message bodies, task descriptions, tool arguments,
tool results and permission intention text. Therefore:

* **Explicit registration only.** A session is read only when the supervisor
  registered it against this plan and repository at spawn. There is **no scan**
  of `session-state/`, no walk of other sessions, no cloud history, no other
  home directory. An unregistered session is invisible.
* **An allowlist normaliser, not a filter.** Exactly these fields may leave the
  reader: event id, event type, timestamp, `agentId`, `toolCallId`, `toolName`
  (validated against a safe-identifier form), phase, and an allowlisted exit or
  error code. **Everything else is dropped before the record leaves the parse
  function** — `reasoningText`, `reasoningOpaque`, assistant bodies, task
  descriptions, tool arguments, tool results and permission intention text are
  never read into a returned structure, never cached, never logged, never put
  in an exception message, and never written to disk.
* **Bounded incremental reading.** Initial tail ≤ 4 MiB per registered session;
  each refresh ≤ 1 MiB per source; a single line ≤ 1 MiB; at most 32 registered
  sources per server. A torn first line, a malformed line, an oversized event,
  a rotated or truncated file, or a cursor that cannot be resumed produces an
  explicit **coverage gap marker** — never an exception and never a count that
  silently claims to be complete. Cursors advance only over fully processed
  records and carry a source generation so a restart deduplicates.
* **`traces.db`** (`$GROGU_HOME/traces.db`: `run_id`, `created_at`, `kind`,
  `provider`, `model`, `status`, `duration_ms`, `input_tokens`,
  `output_tokens`, `estimated_cost_usd`, `payload_json`) may be read for the
  scalar columns only. **`payload_json` is arbitrary legacy data: it is never
  displayed, never returned by the API and never joined into a row.**
* **No polling of models.** Nothing asks an agent to report its status, and no
  model is spent producing anything on this board.

**Coverage degrades honestly.** When source 2 is absent, unregistered, or its
event vocabulary is a version this build does not recognise, the board keeps
every agent from source 1 and reports
`tools: unavailable`, `lifecycle: partial` — it does not fall back to guessing.
An unknown event type is counted as unsupported without its body being copied.

**Identity is registered, never inferred.** A `grogu` command's PID is not the
agent's PID, and one agent is a sequence of short-lived processes. Correlation
is by the registered mapping `(repository, plan, GROGU_AGENT, run_id,
session_id, agentId, role, workstream)`. **Never correlate by `cwd` prefix, by
display-name similarity, or by proximity in time.** An event with no registered
mapping is an explicitly *uncorrelated* row, shown as such, with no role and no
imported description. Tool counts from source 2 and command counts from source 1
are reported separately and are never added together.


### Live facts versus authored text

Every element in the control room is labelled as exactly one of:

* **observed** — derived from `activity.jsonl`, `traces.db` or plan state.
  Command names, counts, exit codes, timestamps, ages, stage states, gate
  states, open amendments and defects *as records*.
* **authored** — text a model wrote: an amendment claim, a defect report, a
  steering note, a plan body, a commission brief. Rendered in a visually
  distinct treatment with the author's role and agent attached.

The user must never have to guess which they are reading. A summary sentence
that Grogu itself composed from observed facts is **observed**; a summary
sentence a model composed is **authored**. There is no third category, and the
control room never asks a model to produce one.

### The agent row

```json
{"agent_key": "…", "agent": "plan-v2-engineer-core", "run_id": "…",
 "role": "engineer", "roles": ["engineer"], "workstream": "core",
 "plan": "p-20260905-aab017",
 "revision": {"last_read": "r0031", "current": "r0034", "relation": "behind"},
 "lifecycle": "registered|running|finished|failed|cancelled|unknown",
 "activity": "active|quiet|possibly_stuck|blocked|unknown",
 "connection": "live|stale|disconnected|unknown",
 "started_at": …, "last_observed_at": …, "elapsed_ms": …,
 "elapsed_basis": "lifecycle_start|first_observed|unknown",
 "current_action": {"tool_name": "bash", "phase": "started", "at": …} ,
 "tools": {"started": 61, "completed": 59, "failed": 2, "inflight": 1,
           "complete": false},
 "grogu_commands": {"calls": 61, "failures": 2, "last_command": "plan gate"},
 "failures": [...], "blockers": [...],
 "steering": {"unread": 1, "delivered": 3, "acknowledged": 2},
 "coverage": {"lifecycle": "complete", "tools": "partial",
              "outcomes": "unavailable"},
 "basis": "observed"}
```

**Three axes, kept orthogonal.** Conflating them is how a dashboard starts
lying:

* `connection` — how healthy the *observer* is. Sample age > 10 s is `stale`;
  30 s of failed collection is `disconnected`. This says nothing about the
  agent.
* `lifecycle` — what the agent's run is doing. Only explicit evidence moves it
  to `finished`, `failed` or `cancelled`, and **a terminal state stays terminal
  even when the collector later disconnects.** Silence, a vanished CLI process,
  an expired window or a completed tool never proves an agent finished.
* `activity` — what it appears to be doing now.

| activity | definition |
|---|---|
| `active` | a `grogu` command or a tool event in the last 300 s |
| `quiet` | 300 s–3600 s of silence from a **healthy** observer; not a claim that anything is wrong |
| `blocked` | a real blocker exists: an unresolved permission request, a closed gate, an open defect or amendment for its role, or an unread `requires_replan` note |
| `possibly_stuck` | the labelled heuristic below |
| `unknown` | no mapping, or coverage is unavailable |

`possibly_stuck` is a **labelled heuristic shown with its evidence**, never an
automatic stop, replan or notification. It requires a healthy observer, a known
non-terminal run, and one of: (a) a tool exceeds an explicitly observed deadline
by 60 s; (b) no recorded outcome — artifact, revision, stage change or lifecycle
result — for 900 s while ≥ 100 completed tool events were observed; (c) ≥ 3
explicit failures with the same safe code within 120 s. A long-running tool with
no declared deadline is **not** stuck merely for taking time. No prose is
analysed to reach this conclusion, and the thresholds are constants a test can
assert.

`elapsed_ms` is measured from an observed lifecycle start. Without one, show a
labelled first-observed **lower bound** — never a fabricated launch time. The
plan's current revision is not assumed to be the revision the agent read;
`revision.relation` says `behind` when they differ and `unknown` when the last
read is not observable.

`fresh_as_of` on the snapshot is the timestamp of the newest record considered,
and the interface shows the **age of the data** beside the age of the agent. A
board three minutes stale while claiming an agent is idle is worse than no
board.

**Refresh:** one server-side collector samples at most once per 2 s across all
clients; a visible page polls every 2 s and immediately on focus; a hidden tab
backs off to 10 s and resyncs on focus. A locally observable event reaches the
visible board within 5 s at p95. **Reading the dashboard must not create Grogu
activity records** — otherwise the board makes agents look busy by being
watched.

### Drilling in

Selecting an agent shows only what is observable, in three panels:

1. **Activity** — its `grogu` commands and, where source 2 is registered, its
   tool events, in order, with times, exit codes, and the gaps between them
   shown as gaps. Nothing is interpolated, and a gap marker from a truncated or
   rotated source is shown as a gap rather than closed over.
2. **Evidence** — the plan objects it touched: stages written, amendments
   raised, defects filed or resolved, reviews recorded, artifacts attached.
   Each links into the plan document at the node it concerns, resolved at the
   recorded revision, so "this defect" navigates to the node the defect is
   about. A target that is missing, moved or sealed shows an explicit
   unavailable state — it never silently navigates to something similar.
3. **Limits** — a fixed, always-present statement of what Grogu cannot see for
   this agent, naming the categories: model reasoning, command arguments, tool
   arguments and results, file edits, and — when source 2 is unregistered —
   tool activity entirely.

### Feedback that actually arrives

The control room's write path is `PlanStore.steer` — the existing one, not a
new channel.

* Selecting an agent and writing feedback sends `grogu plan steer` scoped to
  that agent's `--plan` and `--role`.
* **Binding** feedback sets `requires_replan`, which already closes the gates
  until the architect folds it in. The composer says which gates will close,
  by name, before the user sends it.
* Delivery is acknowledged from the existing read receipts: the row shows
  `unread` until `steering_acked` advances for that role and agent, then
  `read at <time>`. This is the fix for the failure mode the harness already
  records — an agent shown a note once that discards its output has lost it
  (harness friction #66, #70) — because the user can now *see* that it was not
  read.
* Where a running agent is holding a handle, the control room prints the
  relay command rather than the text, per `.github/AGENTS.md`: it never pastes
  steering into another channel.

### Privacy invariants for the control room

**CR1.** No raw tool arguments, no tool results, no assistant message bodies,
no task descriptions, no permission intention text, no `reasoningText`, no
`reasoningOpaque`, no secrets and no sealed-stage content — ever, in the board,
the drill-in, the API, an exception message, a log line, a trace row or any
cache. The allowlist normaliser drops them **before** the record leaves the
parse function; this is not a display filter.
**CR2.** A sealed stage's existence and state may be shown; its content may
not. An engineer viewing the control room sees the same board as the user
minus anything that would name a testing or evaluation node.
**CR3.** `payload_json` from `traces.db` is never rendered, never returned and
never joined.
**CR4.** The control room writes nothing to disk except through
`PlanStore.steer` and the registration record. It builds no new log file and no
new database. Its in-memory cache is keyed by repository, plan and effective
role, is disposable, and is discarded on a role change or a restart — a broader
view can never be reused for a narrower one.
**CR5.** The activity log stays under `$GROGU_HOME`, never inside a repository,
and is never committed. Only registered sessions are read; there is no scan of
`session-state/`.
**CR6.** No model is asked to narrate progress. If a status summary is ever
displayed it must be an explicitly supplied, classified, ≤240-character string
with its author role and source event attached, labelled `authored`, and it may
never drive lifecycle, failure counts, stuck detection, gates or any compiled
artifact. An ordinary assistant message or a hidden thought is not a summary
source. When provenance cannot prove a summary is permitted, it is `null` and
the interface says summaries are unavailable.

### Multi-agent filtering

Filter by plan, role, workstream, repository and state; sort by age, failures
or blockers. The default view is `waiting_on_you()` first — the existing
function, unchanged — then agents that are `stuck`, then `working`, then the
rest. The question the user asks every time they look is "is anything stuck on
me", and that must be answerable in one glance without scrolling.

## The CLI surface


All under `grogu plan doc`, all accepting `--json`, all refusing on a legacy
plan with the migrate instruction:

```
grogu plan doc open <id> [--mode document|canvas|dependency|revision] [--port N]
                         [--no-open] [--timeout SECS]
grogu plan doc show <id> [--stage S] [--kind K] [--json]
grogu plan doc projection <id> --role R [--stage S] [--include normative|all]
                               [--budget N] [--since REV] [--format md|json]
grogu plan doc node add --kind K --title T [--body -] [--stage S] [--attr k=v]
grogu plan doc node set <node-id> [--title T] [--body -] [--attr k=v] [--order N]
grogu plan doc node rm <node-id>
grogu plan doc link <from> <kind> <to>      /  unlink <edge-id>
grogu plan doc patch <id> --file - [--base REV] [--dry-run] [--intent "…"]
grogu plan doc revisions <id> [--json]      /  diff <id> --from REV --to REV
grogu plan doc propose <id> --file - --why "…"
grogu plan doc proposals <id> [--json]      /  accept <pid> | reject <pid> --why "…"
grogu plan doc impact <id> --select <node-id|json> [--depth N] [--json]
grogu plan doc compile <id> [--check]       /  verify <id>
grogu plan doc migrate <id> [--dry-run]     /  revert <id>
grogu plan doc control [--plan ID] [--role R] [--workstream W] [--state S]
                       [--window MINUTES] [--json]
grogu plan doc control --agent NAME [--json]        # the drill-in
grogu plan doc control --agent NAME --feedback - [--binding]
```

Per harness friction #77, `--why`, `--intent` and `--body` all accept `-` for
stdin. Per friction #74, every subcommand accepts and honours `--role`.

## The HTTP API contract

**This section is the contract between the `core`/`integration` workstreams and
the `workspace` workstream. Both build against it; neither may change it
without an amendment.** All responses are JSON except the static routes. All
mutating routes require the session cookie, the `X-Grogu-Token` header and a
matching `Origin`.

| method | path | request | response |
|---|---|---|---|
| GET | `/` | — | `index.html`; `/?t=<token>` exchanges the single-use token for a cookie, then `303` to `/` |
| GET | `/static/<file>` | — | built asset from `dist/` |
| GET | `/api/health` | — | `{ok, plan, revision, role, modes}` |
| GET | `/api/doc` | `?stage=&include=` | `{plan, title, revision, role, stages:{readable[],sealed[],written{}}, nodes[], edges[], counters{}, sealed_edge_count}` |
| GET | `/api/projection` | `?role=&stage=&include=&budget=&format=` | `{markdown}` or the JSON twin |
| POST | `/api/patch` | `{base, ops[], intent}` | `200 {revision, digest, changed[]}` · `409 {error:"stale", revision, ops_since[]}` · `403` on a sealed path · `422 {error, op_index, why}` |
| POST | `/api/threads` | `{selector, body}` | `{thread}` · `409` on a stale `body_digest` |
| POST | `/api/threads/<tid>/reply` | `{body}` | `{thread}` |
| POST | `/api/threads/<tid>/resolve` | `{note}` | `{thread}` |
| POST | `/api/threads/<tid>/reopen` | `{}` | `{thread}` |
| POST | `/api/threads/<tid>/revise` | `{instruction}` | `{proposal}` |
| GET | `/api/proposals` | — | `{proposals[]}` |
| POST | `/api/proposals` | `{ops[], why, from_thread, base}` | `{proposal}` |
| GET | `/api/proposals/<pid>/preview` | — | `{node_diff, compiled_diff, impact, stale}` |
| POST | `/api/proposals/<pid>/accept` | `{}` | `{revision}` |
| POST | `/api/proposals/<pid>/reject` | `{why_not}` | `{proposal}` |
| POST | `/api/impact` | `{selector, depth}` | `{direct[], transitive[], cycles[], compiled_delta[]}` |
| POST | `/api/layout` | `{scope, algorithm:"dagre"}` | `{ops[]}` — the caller applies them as a patch |
| GET | `/api/events` | — | SSE: `revision`, `proposal`, `thread`, `control`, `shutdown` |
| GET | `/api/control` | `?plan=&role=&workstream=&state=&window=` | `{fresh_as_of, limits[], waiting_on_you[], agents[], plans{}}` |
| GET | `/api/control/<agent>` | — | `{agent, activity[], evidence[], limits[], blockers[]}` |
| POST | `/api/control/<agent>/feedback` | `{text, binding}` | `{seq, delivered_to, gates_closed[]}` |
| POST | `/api/request-changes` | `{note}` | unchanged from the review server |
| POST | `/api/approve` | `{confirm_open, note}` | unchanged; refuses when a role is set |
| POST | `/api/shutdown` | `{}` | `{ok:true}` |

Error bodies are always `{"error": "<machine token>", "message": "<human>"}`.

## Offline, privacy and security

* No runtime network access. A Playwright test records every request the page
  makes and fails on any destination that is not the bound loopback origin.
* The graph carries the user's unfiltered words. `graph/`, `log/`,
  `snapshots/`, `proposals/`, `projections/` and `legacy/` are git-ignored;
  `plan finalize` stages only what it stages today; `grogu guard` runs over the
  compiled artifacts before they are published, unchanged.
* Node bodies are rendered by `grogu_markdown` and are **escaped by
  construction**: the closed tag allowlist stays, no raw HTML passes through,
  and no `<img>` is emitted from plan text. The React app must render node
  bodies through the same server-produced HTML, never with
  `dangerouslySetInnerHTML` on unsanitised text.
* `dist/LICENSES.txt` is generated at build time and lists every bundled
  package with its licence; the build fails if a package's licence is not in the
  allowed set (`MIT`, `ISC`, `BSD-2-Clause`, `BSD-3-Clause`, `Apache-2.0`).

## Backwards compatibility

* A plan that is never migrated keeps working exactly as today. Legacy plans
  are readable and writable through the existing code paths for as long as they
  exist.
* Every existing command keeps its output shape. `plan show`, `plan status`,
  `plan brief`, `plan gate`, `plan finalize`, `plan retro`, `review *` and the
  MCP surface must all pass their existing tests unchanged, against both a
  legacy and a migrated plan.
* `schema_version` is 1 on every new file. A file with a higher version is
  refused with the version it needs, never partially read.
* `PlanStore.write_stage` on a `.plan` plan imports and recompiles, and the
  bytes it reports writing are the compiled bytes. The one visible behaviour
  change is that Markdown written back out may be normalised; the command says
  so when it happens. Harness friction #73 (a `--file` pointing at the stage's
  own artifact silently not registering as a rewrite) must not be reintroduced:
  a `--file` inside the `.plan` package is refused with the reason.

## Failure recovery

| failure | detection | recovery |
|---|---|---|
| crash mid-write | `HEAD` names a missing revision | fall back to the newest complete log entry, rewrite `HEAD`, report it |
| partition corrupted | digest ≠ revision `after_digest` | replay from the newest snapshot; if that fails, from `r0001` |
| compiled artifact edited by hand | `plan doc verify` digest mismatch | `plan doc compile` overwrites it and names what differed |
| both `<id>/` and `<id>.plan/` | on every `plan_dir()` | refuse, naming both paths and the repair |
| migration produces a non-equivalent artifact | step 6 of migration | abort, leave `<id>/` untouched, print the diff |
| a proposal's base is gone | `preview` | shown `stale`; rebase or abandon explicitly |
| lock held by a dead process | `exclusive_lock` timeout | report the holder and the lock path; never break the lock silently |
| browser and CLI write together | `base != HEAD` | `409` + `ops_since`; the browser rebases |

## Branching

Grogu's primary checkout stays on `main`. Every workstream worktree is created
**from the integration commit**, and the workstreams re-merge into a new
integration branch for this plan:

```sh
git worktree add ../grogu-worktrees/p-20260905-aab017-<name> \
    -b workstream/p-20260905-aab017/<name> 1933fb3
```

`grogu plan workstream-worktree` prints a deterministic path and branch; if it
branches from `main` rather than from `1933fb3`, reset the new branch onto
`1933fb3` before the first commit and say so in the workstream report. The
integration branch `integration/p-20260905-aab017` already exists and must be
reset to `1933fb3` before the first merge.

## Workstreams

The `--path` globs passed to `grogu plan workstream` are the **only** statement
of ownership. This section names the workstreams and what each is for; it does
not restate their paths.

* **Wave 1, in parallel:**
  * `core` — **the compiler first.** The three identities, the closed kind
    dispatch, the golden corpus and the versioned output are the highest-value
    thing in this plan and the reason the graph is safe to adopt at all; build
    them before the conveniences. Then the document model, canonicalisation,
    patch engine, revision log, schema, selectors, projections, impact
    analysis, and the Markdown importer. Owns `grogu_markdown.py` and
    `grogu_mermaid.py`. No CLI, no server, no HTTP. Delivered as importable
    modules with unit tests.
  * `workspace` — the React application, its build, and its browser tests.
    Builds against the HTTP API contract above and a fixture server it writes
    itself (`tests/` inside its own directory) — it must not wait for `core`.
    Owns all five modes' user interface, the control room included.
  * `controlroom` — the observability read model: the registered-session
    allowlist reader with its cursors, size caps and gap markers; the agent row
    and its three orthogonal axes; freshness; the observed/authored labelling;
    the drill-in; the filters; and the feedback routing through
    `PlanStore.steer`. Owns `grogu_watch.py`. A pure library with no HTTP and
    no CLI, so the privacy invariants CR1–CR6 are testable without a browser —
    and they must be tested against a **fixture `events.jsonl` that deliberately
    contains reasoning text, tool arguments and assistant bodies**, asserting
    none of it appears in any returned structure.
* **Wave 2, after all three:**
  * `integration` — `PlanStore` wiring (`plan_dir`, `_LOCAL_ONLY`,
    `write_stage`, `read_stage`, `stage_path`), the HTTP server including the
    control routes, the whole `grogu plan doc` CLI, migration including the
    review system, the deletion of `review_workspace/` and
    `grogu_review_server.py`, and the docs.

`core`, `controlroom` and `integration` are the subtle ones and carry a
required review; `workspace` carries one for the offline and determinism
invariants.

## Validation

Run from the repository root of the workstream's worktree.

```sh
# the whole suite; the baseline on main today is 706 tests, OK (skipped=6), ~92s
python3 -m unittest discover -s tests -q

# targeted, during work
python3 -m unittest tests.test_grogu_plandoc -q
python3 -m unittest tests.test_grogu_plandoc_compile -q
python3 -m unittest tests.test_grogu_plandoc_patch -q
python3 -m unittest tests.test_grogu_controlroom tests.test_grogu_watch -q
python3 -m unittest tests.test_grogu_plan_server -q
python3 -m unittest tests.test_grogu_review tests.test_grogu_markdown tests.test_grogu_mermaid -q

# the equivalence and integrity gates
python3 bin/grogu plan doc compile <id> --check
python3 bin/grogu plan doc verify <id>

# the golden compiler corpus: any output change must be a reviewed diff
python3 -m unittest tests.test_grogu_plandoc_golden -q
git status --porcelain tests/fixtures/plandoc/golden

# the control room, headless
python3 bin/grogu plan doc control --json
python3 bin/grogu plan doc control --agent <name> --json

# the web application
cd src/plan_workspace && npm ci && npm run build && npm run typecheck
cd src/plan_workspace && npx playwright test

# committed build artifacts match a fresh build
cd src/plan_workspace && npm ci && npm run build && cd ../.. && \
  git status --porcelain src/plan_workspace/dist
```

A stage is not complete while any of these fails.

## Open questions — raise these, do not guess

1. **Is the Vite 8 / Rolldown build byte-reproducible across machines?** No
   source consulted promises it. If `git status --porcelain src/plan_workspace/dist`
   is non-empty after a clean rebuild on a second machine, do not paper over it:
   raise an amendment. The fallback is `dist/BUILD.json` recording node, npm and
   vite versions plus a SHA-256 per asset, with byte-drift demoted to a warning
   and functional verification promoted to the gate.
2. **Playwright browser binaries need one network download.** If the
   environment cannot install them, use the configured `playwright` MCP
   capability for the browser and accessibility checks instead, per
   `.github/AGENTS.md`, and say in the report which route was used.
3. **How far does auto-migration go?** This plan auto-migrates on first open by
   a `.plan`-aware command. If that proves surprising in practice — for
   instance on a `complete` or `superseded` plan — restrict it to an explicit
   `plan doc migrate` and raise an amendment rather than adding a prompt.
4. **Is `traces.db` joinable to an agent at all?** `activity.jsonl` records a
   `pid` and no `run_id`; `traces.db` records a `run_id` and no agent. If no
   reliable join exists, **say so and show no model or token figures** rather
   than inferring them from timing. Do not add a new correlation identifier to
   the activity record without an amendment — every field added there is a
   privacy decision.
5. **What is a "workstream" for an agent that never declared one?**
   `activity.jsonl` has no workstream field. Take it from the registration
   record, and show it as unknown when there is none. **Do not derive it from
   `cwd` or from the agent name.**
6. **The Copilot session event vocabulary is not a contract.** The event
   names in `events.jsonl` were observed, not published. Version-check them; on
   an unrecognised version report `tools: unavailable` and keep the rest of the
   board. Do not widen the allowlist to recover coverage — raise an amendment.
7. **Who registers a session?** This plan says the supervisor registers at
   spawn. If in practice nothing is in a position to do that reliably, the
   control room still works from source 1 alone and reports limited coverage;
   do not invent a registration by scanning. Raise an amendment.
