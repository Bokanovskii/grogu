# Interactive plan review workspace — evaluation plan

Testing asks whether the workspace does what it claims. This asks whether a
person can actually review a plan in it, and whether the thing that made this
feature necessary — a plan too complex to read as terminal prose — is actually
fixed.

Run this only once the testing stage is complete. Evaluation on a broken build
measures the break.

## Setup

Use the Playwright MCP capability, in a **fresh automation-owned browser
context with a temporary profile**, headless where possible. Never attach to
the user's browser, window, tabs, profile or remote-debugging session, and
never rely on active-window or focus state. This is the operating contract, not
a preference.

Build the subject in a scratch git repository under a temporary directory, with
`GROGU_HOME` redirected — never against this repository's own live
`.grogu/plans/`. The subject plan must have all four stages written and must
include:

* an implementation stage of at least 20 KB with nested lists, several fenced
  code blocks, a table and long paragraphs — a real plan, not a paragraph. The
  implementation stage of `p-20260904-6095a8` itself is a legitimate corpus;
  copy it in.
* a design stage containing at least two Mermaid flowcharts, one with a
  subgraph, labelled edges, and two edges between the same pair of nodes.
* one diagram of an unsupported type (a `sequenceDiagram`), so the degraded
  semantic path is exercised.

Launch with `grogu review <plan> --no-open --json`, take the URL from the JSON,
and drive Playwright to it. Capture every screenshot into
`<repo>/.grogu/plans/<id>/artifacts/` and attach the ones named below with
`grogu plan attach`.

## What is being judged

The design spec written in the design stage is the standard for anything
visual. Where this plan and the design spec disagree about spacing, copy or
layout, the design spec wins; where the design spec is silent, judge against
the criteria here. Read it before you start.

## E1. Can a person read the plan?

Open the workspace at 1280×800 and again at 1440×900.

* The plan text is the most prominent thing on the screen. Chrome, sidebars and
  controls are visibly secondary.
* Headings, lists, code blocks and tables are distinguishable at a glance —
  capture a screenshot of the implementation stage and judge it as a document,
  not as a feature demo.
* Body text meets a contrast ratio of at least 4.5:1 against its background,
  and any text below 18px does too. Measure it; do not eyeball it.
* Time from navigation to the implementation stage being fully rendered is
  under 1 second for the 20 KB stage on a warm cache. Report the measured
  figure.
* The browser console has zero errors and zero CSP violation reports across the
  whole session. A CSP violation that the page recovers from still counts.
* **No request leaves the loopback interface.** Intercept every request through
  Playwright and assert every URL's host is `127.0.0.1` or `localhost`. This is
  the privacy proof for invariant I5 and it is a hard pass/fail.

Fail this section if a reviewer would rather read the Markdown in a terminal.
That is the bar the feature exists to clear.

## E2. Anchored text comments

* Select a phrase mid-paragraph, leave a comment, and confirm: the selected
  text is visibly marked, the thread appears in the review surface, and the
  mark covers exactly the selected characters — no drift by a word, no
  swallowing of the surrounding punctuation.
* Verify the stored anchor is correct at the source level: read the thread with
  `grogu review list <plan> --json` and assert
  `stage_body[anchor.start:anchor.end] == anchor.exact` and that `exact` is the
  text you selected.
* Repeat for: a selection spanning inline code and plain text; a selection
  inside a fenced code block; a selection spanning two paragraphs; a heading; a
  table cell; and a selection containing a character that must be
  HTML-escaped (`<`, `&`).
* Reload the page. Every thread reappears in the same place with the same mark.
* Two threads on overlapping ranges both render and remain individually
  selectable.

## E3. Semantic diagram comments

* Click a node in the first flowchart, leave a comment, and confirm the comment
  binds to the node's **source identifier**: `grogu review list --json` shows
  `anchor.kind == "mermaid"`, `anchor.target == "node"`, and `anchor.node_id`
  equal to the id written in the Mermaid source — not a generated DOM id, not
  the label, not a coordinate.
* Click an edge — specifically, one of the two parallel edges between the same
  pair — and confirm the stored anchor names the right `pair_ordinal`. Then
  confirm the highlight lands on the correct one of the two paths on screen.
  Getting this wrong is invisible in a screenshot of a simple diagram, so use
  the diagram with parallel edges deliberately.
* Comment on a subgraph.
* On the `sequenceDiagram`, confirm the only anchor offered is the whole
  diagram, and that this is explained rather than merely absent.
* Capture a screenshot showing a node comment and an edge comment
  simultaneously highlighted. Attach it as `diagram-anchors.png`.

## E4. Degraded mode is genuinely usable

Remove the installed Mermaid asset (or run with `GROGU_REVIEW_MERMAID` pointing
nowhere) and reload.

* The workspace loads. Nothing is broken, nothing hangs, no console error.
* Each Mermaid block shows its source, plus commentable chips for its nodes,
  edges and subgraphs.
* Commenting on a node chip produces a **byte-identical anchor** to the one
  produced by clicking that node with the asset installed. Compare the two JSON
  objects field by field, ignoring timestamps and ids.
* The reason the diagrams are not drawn is stated on screen with the exact
  command that fixes it, and it can be dismissed.
* Capture `degraded-mode.png`.

Then run `grogu review assets --install --from <the file>` and reload: diagrams
draw, and the existing chip-created threads highlight the right elements.

## E5. Revision awareness — the thing that makes this worth building

With four threads on the implementation stage, rewrite that stage as the
architect (`grogu plan write … --replace`) so that:

* thread A's quoted text is untouched;
* thread B's quoted text is unchanged but moved by a large insertion above it;
* thread C's quoted text is lightly edited (a word changed);
* thread D's quoted text is deleted entirely.

Reload the workspace and confirm on screen: A and B are anchored in the right
places, C is shown as moved with its comment intact, and D is shown as no
longer present in the plan **with its original quote still readable**. A
reviewer must be able to tell those four states apart without reading JSON.

Then do the same for the design stage: rename a node's label but keep its id;
change a node's id; delete a node. Confirm the corresponding threads read as
anchored, moved and orphaned.

Judge the orphaned presentation specifically. A comment the user wrote that
silently vanishes, or that reappears attached to unrelated text, is the failure
this whole mechanism exists to prevent — and it is a failure the tests can
pass through, because "orphaned" is a correct state that can still be presented
uselessly.

## E6. The review round, end to end

1. Leave three comments across two stages.
2. Request changes, with a covering note.
3. Outside the browser, confirm: `grogu plan steering --role architect --plan
   <id>` shows exactly one new note naming all three thread ids;
   `grogu plan status` reports `needs_review`; `grogu plan gate implement`
   is blocked and exits 3.
4. As the architect, read the round with `grogu review list <plan> --json`,
   rewrite the affected stages, and confirm the round reads as answered on
   reload.
5. Resolve the three threads in the workspace.
6. Approve. Confirm `grogu plan gate implement` is now allowed and
   `grogu plan status` shows approved.
7. Attempt approval again from a shell with `GROGU_ROLE=architect` exported and
   confirm it is refused, with the refusal visible in the workspace rather than
   swallowed.

Also confirm the guard rail: with an open thread, approving requires a second,
deliberate confirmation, and the open threads are listed at that moment.

## E7. Keyboard and accessibility

* The entire review flow is reachable by keyboard: reach a thread, open it,
  read it, resolve it, and move to the next, without a pointer. Report the
  actual key sequence.
* Focus is always visible, and focus order follows reading order.
* `prefers-reduced-motion: reduce` suppresses non-essential animation. Verify
  with Playwright's media emulation rather than by asserting the CSS exists.
* Every non-textual control has an accessible name. Comment marks are
  announced as something other than plain text.
* At 200% browser zoom the layout remains usable and nothing is clipped.

## E8. Robustness a reviewer will actually hit

* A plan stage of 200 KB: the page renders, selection still anchors correctly,
  and interaction stays responsive. Report the render time.
* A plan with no design stage; a plan where a stage is still `_Not written
  yet._`; a plan with zero comments. Each has a deliberate, readable state, not
  an empty box.
* A Mermaid block that fails to render (deliberately malformed syntax): the
  error is shown next to that block, the rest of the page still works, and
  comments on other blocks are unaffected.
* Stop the server (`POST /api/shutdown`) with the page open, then interact: the
  page says the session ended and does not silently discard an unsent comment.
* Two browser tabs open on the same plan: a comment made in one appears in the
  other after its next load, and neither loses a thread.

## Evidence to produce

Attach to the plan with `grogu plan attach`:

* `read.png` — the implementation stage as a reader sees it (E1).
* `text-anchor.png` — a text comment anchored mid-paragraph (E2).
* `diagram-anchors.png` — node and edge comments (E3).
* `degraded-mode.png` — no Mermaid asset (E4).
* `revisions.png` — the four threads after the rewrite, showing anchored,
  moved and orphaned side by side (E5).
* `evaluation.md` — the written verdict: the measured figures (render times,
  contrast ratios, the keyboard sequence), each section marked pass or fail,
  and every failure described as what a reviewer would experience rather than
  as a defect id.

## The verdict

The feature passes evaluation when E1, E2, E3, E5 and E6 all pass outright,
and E4, E7 and E8 have no failure that would stop a reviewer completing a
review.

E1's loopback-only assertion and E6's refusal of a role-bearing approval are
absolute: either failing is a fail for the whole evaluation regardless of how
good the rest is, because they are the privacy and authority invariants the
feature was allowed to exist under.

State plainly, in one sentence at the top of `evaluation.md`, whether you would
rather review a complex plan in this workspace or in the terminal, and why.
That sentence is the actual result; everything above it is the working.
