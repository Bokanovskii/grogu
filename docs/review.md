# Reviewing a plan in the workspace

A complex, multi-stage plan is hard to review as terminal prose, and a
review-required plan otherwise has no user-visible way to interact with it.
`grogu review` opens the plan as a document in a local browser workspace where
you read it and comment on it — inline on a passage, or on a node or edge of a
diagram — and either request changes or approve.

The workspace is a **reader and a comment channel**, never an editor. The plan
Markdown under `.grogu/plans/<id>/` stays canonical and is written by exactly
one path (`grogu plan write`, architect only). Everything the workspace adds
lives beside it in a git-ignored `review.json`, and every effect it has on the
pipeline goes through the existing `PlanStore` methods (`steer`, `approve`).

## Launching

```sh
grogu review p-20260904-6095a8            # same as `grogu review open`
grogu review open p-20260904-6095a8 [--stage implementation] [--port N]
                                     [--no-open] [--timeout SECS] [--json]
```

`grogu review` starts a loopback server bound to `127.0.0.1`, prints a few
lines, and opens your browser at `/?t=<token>`, which sets a session cookie and
redirects to `/`:

```
p-20260904-6095a8  draft  Interactive plan review workspace
  reading as reviewer: design, implementation, testing, evaluation
  diagrams: mermaid not installed; `grogu review assets --install`
  http://127.0.0.1:53412  opened in your browser
  ctrl-c ends the session
```

With `--no-open` the last line carries the one-time launch URL instead, so you
can open it yourself. `Ctrl-C`, the idle timeout (default one hour), or
`End session` in the page all stop the server.

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

`review.json` carries your unfiltered words about the plan. It is git-ignored
(the `.grogu/plans/.gitignore` marker is upgraded in place to add it), is never
staged by `plan finalize`, and is never copied into a plan stage, a steering
note verbatim without your action, telemetry, or the activity log. The server
binds `127.0.0.1` only, emits no CORS headers, sets a strict Content-Security-
Policy, and never renders an `<img>` from plan Markdown, so nothing leaves the
machine at review time.

## Diagrams and the Mermaid asset

Semantic diagram anchors (node, edge, subgraph) are derived by parsing the
Mermaid source in Python and do **not** depend on the rendered diagram. Rendering
is optional: Mermaid's ~3 MB UMD bundle is never vendored and never fetched
implicitly. Install it on request:

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
