# Reviewing a plan in the workspace

A complex, multi-stage plan is hard to review as terminal prose, and a
review-required plan otherwise has no user-visible way to interact with it.
`grogu review` opens the plan in the same local React workspace used by
`grogu plan doc open`, initially in Document mode. You can comment inline on a
passage or graph object, request changes, and — only from an undeclared user
session — approve.

For a `.plan` package, the semantic graph is canonical and comments are graph
`thread` nodes. The legacy `grogu review list/comment/reply/resolve/...`
commands are adapters to those nodes, so there is no second writable
`review.json`. A migrated package keeps the old file verbatim under
`legacy/pre-migration/` solely for rollback. Unmigrated legacy plans retain the
original reader workspace and local `review.json` behavior.

## Launching

```sh
grogu review p-20260904-6095a8            # same as `grogu review open`
grogu review open p-20260904-6095a8 [--stage implementation] [--port N]
                                     [--no-open] [--timeout SECS] [--json]
```

For a package, `grogu review` starts the secured plan server bound to
`127.0.0.1`, prints a one-time `/?t=<token>` URL, exchanges it for an
`HttpOnly` session cookie, burns the token, and redirects to `/`:

```
p-20260905-aab017  draft  Typed plan package
  reading as reviewer: design, implementation, testing, evaluation
  http://127.0.0.1:53412  opened in your browser
  ctrl-c ends the session
```

With `--no-open` the output carries the one-time launch URL. Reusing it returns
`403` and the app shows **This session link has already been used.** `Ctrl-C`,
the idle timeout (default one hour), or **End session** stops the server.

## What you can read

The role that launched the workspace decides what it may read. The user's own
shell has no `GROGU_ROLE`, so it reads as **reviewer** and sees all four
stages. An agent that launches the workspace reads as its own role — an
engineer sees `implementation` and `design`; the sealed `testing` and
`evaluation` bodies never appear in any HTTP response, exactly as
`PlanStore.read_stage` already enforces. The stage tabs are always all four;
a stage you may not read shows a "Sealed" panel and is never requested.

## Commenting

- Select any run of text in the plan and press `c` or the **Comment** button.
- In a rendered diagram, click a node or an edge; without the Mermaid asset the
  diagram is shown as source with a commentable chip per node, edge and
  subgraph.
- Reply and resolve happen in the thread's card. Nothing is ever deleted:
  resolving is reversible with **Reopen**, and a comment whose passage the
  architect later rewrote away becomes *orphaned*, keeping the quoted text.

Comments are anchored with the W3C Web Annotation model — an exact quote plus
32 characters of prefix and suffix, and the source character offsets — so a
comment survives edits to the plan and is re-anchored (unmoved → unique quote →
context-disambiguated → fuzzy → orphaned) every time the workspace loads.
The shared implementation lives in `grogu_plandoc_anchor.py`; `grogu_review`
exports compatibility names for existing callers.

## Rounds

**Request changes** closes the current review round and sends **one** steering
note to the architect (composed of your covering note and one block per open
comment). That sets the plan to `needs_review`, which the gate already treats as
blocking. The next comment you make opens the next round. **Approve** calls
`PlanStore.approve` unchanged — which refuses any caller whose `GROGU_ROLE` is
set, so an agent-launched workspace can read and comment but never approve.

## The CLI mirror

Every workspace action has a headless equivalent, which is how the architect
reads a round without a browser and how the feature is tested:

```sh
grogu review list p-20260904-6095a8 [--stage S] [--open] [--json]
grogu review comment p-... --stage implementation --quote "…" --body "…"
grogu review comment p-... --stage design --node Parse --body "…" [--diagram 0]
grogu review comment p-... --stage design --edge Parse>Render --body "…"
grogu review reply p-... c1 --body "…"
grogu review resolve p-... c1 [--note "…"]
grogu review request-changes p-... [--note "…"]
grogu review status p-... [--json]
```

`grogu review comment --quote` finds the quoted text in the stage body and
refuses when it is absent or occurs more than once, naming how many times it
was found. `plan status` gains a `review: round 2, 3 open, 1 orphaned` line when
a review exists, and `plan brief --role architect` lists the open comments.

## Local only

Graph partitions, thread revisions, proposals, caches, registrations, recovery
copies, and legacy `review.json` are git-ignored. `plan finalize` stages only
the same compiled Markdown, record, and attachments it stages for a legacy
plan. Review text is never copied into telemetry or the activity log.

The package server binds loopback only, emits no CORS headers, validates Host,
Origin and `Sec-Fetch-Site`, requires a session cookie plus
`X-Grogu-Token` for mutations, caps bodies at 256 KiB, and uses a strict CSP.
Node bodies are rendered through `grogu_markdown`; raw HTML and plan-authored
images are not passed through.

## Diagrams and the legacy Mermaid asset

Semantic diagram anchors (node, edge, subgraph) are derived by parsing Mermaid
source in Python and do **not** depend on rendering. The React package workspace
uses its committed build and needs no separately installed Mermaid runtime.

The commands below apply only to the unmigrated legacy review workspace.
Rendering there is optional: Mermaid's UMD bundle is never fetched implicitly.

```sh
grogu review assets                    # what is installed
grogu review assets --install          # download the pinned 11.17.2 bundle
grogu review assets --install --from ./mermaid.min.js   # from a local file
```

The bundle is pinned to version 11.17.2 and its SHA-256 is hard-coded in
`grogu_review_server.py`; `--install` refuses on any digest mismatch and installs
nothing. It is written to `$GROGU_HOME/assets/review/mermaid-11.17.2/`, with the
upstream `LICENSE` alongside it. `GROGU_REVIEW_MERMAID` may point at an existing
file for an air-gapped install.

### Stable diagram selectors (pinned build 11.17.2)

Confirmed against the pinned build by rendering the fixture flowchart
(`tests/fixtures/review/flowchart.golden.svg`) with `deterministicIds: true`,
`securityLevel: 'strict'` and `flowchart.htmlLabels: false`:

- **Node.** A node `A` produces a `<g class="node …">` whose `id` ends with
  `flowchart-A-<counter>`, e.g. `grogu-diagram-0-flowchart-Parse-0`. Resolve it
  by matching `/(?:^|-)flowchart-A-\d+$/` against element ids, preferring
  `g.node`; fall back to a `g.node` whose text equals the node's parsed label.
- **Edge.** An edge is a `<path class="… flowchart-link">` carrying
  `data-edge="true"`, `data-et="edge"`, and `data-id="L_<from>_<to>_<ordinal>"`,
  with the element `id` being `<diagramId>-L_<from>_<to>_<ordinal>`. The
  **ordinal is the pair ordinal** — the 0-based index among edges sharing the
  same `(from, to)` — so the first `Parse --> Render` edge is
  `data-id="L_Parse_Render_0"` and a self-loop `Parse --> Parse` is
  `L_Parse_Parse_0`. Resolve edges by `[data-id$="_<from>_<to>_<ordinal>"]`
  first, then by the `id` suffix, then by the n-th `path.flowchart-link` using
  the parsed declaration index.

Because these conventions can move on a Mermaid upgrade, the golden SVG fixture
plus a fast test that asserts the node pattern and edge attributes are present
turns such a change into a test failure rather than a silent breakage.

The Content-Security-Policy is built strict (`script-src 'self'`, no
`'unsafe-inline'` and no `'unsafe-eval'` for scripts; `'unsafe-inline'` is
present for `style-src` only, because Mermaid injects a `<style>` element). If a
future Mermaid build genuinely cannot render under it, relax exactly one
directive, keep `script-src` free of `'unsafe-inline'`, and record what forced
it here.
