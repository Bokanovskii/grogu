# Interactive plan review workspace — design

The reader came for the plan. The plan is the only thing rendered at full size,
full contrast and full measure. Every comment affordance is either invisible
until the reader acts (the selection button), or lives outside the reading
column in a 280px rail (threads), or is one 2px line under a run of text
(marks). Nothing about the comment machinery moves, reflows or re-colours the
prose.

Two decisions frame everything below.

**Threads live in a right-hand rail, not inline and not in a drawer.** Inline
threads reflow the document, so the prose the comment is about moves as soon as
you comment on it, and a plan with 12 comments stops being a document. A drawer
hides the relationship between a comment and its passage behind a toggle. A
rail keeps both on screen at once: the mark stays in the prose at its original
position, and the card sits at the same vertical offset in the rail, so the
relationship is spatial and needs no line, no arrow and no animation to
explain. The rail is also the only place that can hold an orphaned thread,
which by definition has no passage to sit beside.

**Anchor state is carried by three signals at once, never by colour alone.**
Underline style in the prose (solid / dashed / absent), a word in a fixed
position on the card (nothing / `moved` / `orphaned`), and, for orphaned only,
a section of the rail that sorts above every other thread and cannot be
collapsed. That is the failure mode this feature exists to prevent, so it gets
redundancy the other states do not.

## Surfaces

- **Reading view** (`/`) — the stage body rendered as a document in a 672px
  column, with sticky header, four stage tabs, and the thread rail.
- **Stage tabs** — Design, Implementation, Testing, Evaluation, in that fixed
  order, always all four, each carrying its own state (written, not written,
  not needed, sealed).
- **Anchored mark** — a run of text inside the stage body carrying one or more
  threads.
- **Diagram (rendered)** — a Mermaid flowchart whose nodes, edges and
  subgraphs are individually hoverable, focusable and commentable.
- **Diagram (degraded)** — the same block as source in a `<pre>` plus a chip
  row of the same nodes, edges and subgraphs, each commentable.
- **Comment composer** — a card in the rail, opened by selection, by the
  selection button, by `c`, or by activating a diagram target.
- **Thread rail** — one card per thread, positioned to its mark, plus the
  orphaned section, the empty state, the cross-stage pointer, and the resolved
  disclosure.
- **Review actions** — Request changes and Approve in the header; both open a
  native `<dialog>` for confirmation.
- **Keyboard help** — a native `<dialog>` listing every shortcut, opened with
  `?`.
- **Revision banner** — a strip above the document when the architect rewrote a
  stage while the workspace was open.
- **Page footer** — End session and the privacy line.
- **Session-ended screen** — replaces the whole page when the server is gone.
- **CLI launch and mirror** — `grogu review`, `grogu review list`,
  `grogu review status`, `grogu review assets`, and the one line
  `plan status` gains.

## Hierarchy

**Reading view.** The primary element is the stage body: 17px/28px at
`--color-ink`, 672px wide, centred, on `--color-bg` with no card, no border and
no background of its own. Everything else is 13px or 15px at `--color-ink-2` on
`--color-bg` or `--color-surface`. The header is 100px of chrome above a
document that is otherwise unbounded.

Primary action: **Approve** — the only filled `--color-accent` control on the
page. There is exactly one, in the header, and nothing else on the page uses a
filled accent background.

Secondary: **Request changes** — 1px `--color-rule-strong` border, transparent
background, `--color-ink` label. This is the ordinary path and it is a normal
button: no warning colour, no confirmation weight in its resting state, no
capital letters. It sits 12px to the left of Approve.

Tertiary, in ascending quietness: stage tabs (15px, selected tab 600 weight
with a 2px `--color-accent` underline), rail card actions (`Reply`, `Resolve`,
13px text buttons), the selection button, the footer.

**Destructive actions: there are none.** No comment can be deleted, no thread
can be discarded, and nothing in this workspace removes a user's words (I6).
`Resolve` is reversible by `Reopen`. `End session` stops a local process and
loses nothing; it is 480px away from Approve, in the page footer below the
document, never in the header action group.

**Thread card.** Primary is the comment text (15px/22px, `--color-ink`). The
quote is secondary (13px/20px, `--color-ink-2`, 2px left border in
`--color-rule-strong`). Author and time are tertiary (12px/16px,
`--color-ink-2`). Actions appear only when the card is expanded.

**Composer.** Primary is the textarea. `Comment` is the primary button,
`Cancel` is a text button to its left with 8px between them.

**Dialogs.** One primary button, bottom right; `Cancel` to its left as a text
button. Escape and the backdrop both cancel.

## States

### Reading view

| State | Trigger | What is shown |
| --- | --- | --- |
| loading | first `GET /api/plan` | nothing for the first 300ms; then the skeleton (three `--color-rule` bars, 28px tall, widths 60% / 100% / 92%, 12px apart, repeated 5 times down the measure, no animation) |
| loading, slow | load still pending at 3000ms | the skeleton plus one line above it: `Still reading the plan.` |
| ready | data received | the stage body |
| stage not written | `written == false` | body replaced by the unwritten copy |
| stage not needed | `state == "declined"` | body replaced by the declined copy |
| stage sealed | stage not in `readable_stages` | body replaced by the sealed copy; no request for that stage is ever made |

`written` is the only field that decides whether a body exists. `state` is the
stage's **progress** (`pending`, `in_progress`, `complete`) and must not gate
any of the three panels above, must not gate the document, and must not gate
the rail. A written stage whose progress is `pending` is the ordinary case
during review — a stage the architect has just rewritten is exactly that — and
it renders its body and every one of its threads.
| error | `GET /api/plan` fails once | the error card with `Retry` |
| session ended | two consecutive failures, or `POST /api/shutdown` returned, or `End session` | the session-ended screen replaces the page |
| revision changed | poll sees a changed `stage_revisions` value | the revision banner above the document; the body does **not** change until `Reload the plan` |

### Rail

| State | What is shown |
| --- | --- |
| empty, no threads anywhere | the empty-state block, top of rail, 15px/22px |
| empty on this stage, threads elsewhere | `No comments on this stage.` then one line per stage that has them: `Implementation — 3` as a button that switches stage |
| threads present | cards positioned to their marks, in document order; a card whose anchor has no mark on screen (orphaned, or a diagram target) keeps its place in that order at the position of the block it belongs to |
| orphaned present | the orphaned section pinned above every card, never collapsible |
| resolved present | a native `<details>` at the bottom of the rail, closed by default, summary `Resolved — 2` |
| saving a comment | the composer's `Comment` button is `disabled` and its label becomes `Saving` for as long as the request is in flight |
| save failed | the composer keeps every character the user typed and shows the save-failure copy beneath the buttons |

### Diagram target (node, edge, subgraph)

| State | Rendered mode | Degraded mode |
| --- | --- | --- |
| rest | Mermaid's own stroke and fill, unmodified | chip: 1px `--color-rule-strong` border, `--color-surface` background |
| hover | shape stroke `--color-accent` 2px; edge path stroke `--color-accent` 3px | chip border `--color-accent`, 1px |
| focus (keyboard) | 2px `--color-accent` outline, 2px offset, around the element's bounding box | same outline on the chip |
| carries a thread | a filled `--color-accent` circle, 8px diameter, centred on the node shape's top-right bounding-box corner; an edge with a thread is redrawn stroke `--color-accent` at 2.5px along its whole path | a filled `--color-accent` circle, 6px, 6px before the chip label |
| focused thread | as "carries a thread" plus the 2px `--color-accent` outline | as above plus the outline |
| unresolved | the target is not findable in the SVG; nothing is drawn on the diagram and an `Unresolved anchors` chip row appears directly beneath it | not possible — chips come from the Python parse |

### Anchor states in prose

| State | Mark | Card |
| --- | --- | --- |
| anchored | `box-shadow: inset 0 -2px 0 var(--mark-line)`, solid | no state word |
| moved | the same 2px line, dashed 3px on / 3px off, in `--mark-line` | the word `moved` top-right, plus the revision line |
| orphaned | no mark; the text is gone | the word `orphaned` top-right in `--color-attention`, 2px `--color-attention` left border on the card, the quote block, and the orphaned explanation |
| focused | background `--mark-focus-bg`, underline becomes 2px solid `--color-accent` | 2px `--color-accent` left border, card expanded |
| resolved | no mark by default; shown again with its solid line while its card is expanded from the resolved disclosure | the word `resolved` top-right |

Inside a fenced code block a mark is a background block, `--mark-code-bg`, with
2px horizontal padding and no underline, because a 2px underline collides with
descenders and line boxes in a monospace block. A focused mark in code keeps the
background and adds a 2px solid `--color-accent` bottom border.

Overlaps. Each run of text carries every thread that covers it. Depth 1 draws
one line. Depth 2 or more draws two lines: 2px, a 1px gap, then 2px, which needs
`padding-bottom: 6px` on the paragraph and does not change line height at
17px/28px. Depth is never encoded as a colour or an opacity. A click on an
overlapped mark focuses the most recently created thread covering it; the rail
then places every thread covering that run adjacent, in creation order, each
with the 2px `--color-accent` left border.

A heading, a table cell and a code line are marked exactly like a paragraph: the
line sits under the text run, in the text's own colour context, and never
crosses a table cell boundary — a selection covering three cells draws three
marks, one per cell, and is one thread. A selection covering 90% or more of a
heading's text snaps to the whole heading.

## Flow

1. `grogu review p-20260904-6095a8` starts the loopback server, prints four
   lines, and opens the browser at `/?t=<token>`, which sets the cookie and
   redirects to `/`.
2. The page requests `GET /api/plan`. It opens on the stage named by `--stage`,
   otherwise on the first written and readable stage in the order design,
   implementation, testing, evaluation.
3. The reader reads. Selecting text inside the stage body raises the selection
   button 8px below the selection's last rectangle, horizontally at its left
   edge, clamped to the column. The button is `Comment`. Clicking it, or
   pressing `c`, opens the composer in the rail at the selection's top offset,
   focuses the textarea, and leaves the browser selection in place so the reader
   can see what they are commenting on.
4. `Comment` posts `POST /api/threads`. On success the composer becomes the new
   card, the mark appears, and the thread is focused. On failure the composer
   stays exactly as it was, with the text, and shows the save-failure copy.
   **Cancel** (button, or Escape) closes the composer, clears the selection and
   returns focus to the document; a composer with typed text asks first with the
   discard dialog.
5. A diagram target is commented the same way: click the node, edge, subgraph or
   chip, or focus it and press Enter or `c`. The composer opens with the target's
   description as its header instead of a quote.
6. Replying and resolving happen in the expanded card. Resolve moves the card
   into the resolved disclosure and removes its mark. Reopen returns it.
7. **Request changes** opens its dialog with an optional covering note. On
   confirm, `POST /api/request-changes` sends one steering note, the round chip
   gains its state segment, the round line appears under the
   header, and the Request changes button becomes disabled with the waiting
   copy. Reading and commenting stay available; the first new thread opens
   round 2 and re-enables the button.
8. The architect rewrites a stage. The next poll sees a new revision and shows
   the revision banner. Nothing re-renders until `Reload the plan`. After the
   reload, the rail shows the re-anchor summary line for that reload only,
   orphaned threads sort to the top of the rail and are expanded, and the rail
   scrolls to the first orphaned card.
9. **Approve** opens its dialog. With no open threads it is the plain approval
   dialog. With open threads it is the open-threads variant, whose primary
   button reads `Approve anyway`. On confirm, `POST /api/approve`; on success
   the header actions are replaced by the line `approved · Sep 4 14:22` and
   both buttons are removed. Reading and existing threads stay; the composer no
   longer opens.
10. On failure the dialog stays open, keeps the note, and shows the server's
    message under the buttons prefixed `Could not approve: `.
11. The session ends when the reader presses `End session`, when the terminal
    process is stopped, or when the idle timeout expires. All three land on the
    same session-ended screen.

Cancel is available at every step and never destroys typed text without the
discard dialog. Every failure keeps the user's words on screen.

## Copy

Voice: lower case except for proper nouns and sentence starts; a statement of
what is true, then the command that changes it. No exclamation marks, no
"Oops", no "Successfully", no emoji, no praise. Sentence case on buttons.
Success is not announced; the state changes and the reader can see it.

Rewritten example — what this product does not say, then what it says:

```
Before:  Oops! We couldn't save your comment. Please try again later.
After:   Could not save. Your text is still here.
```

### Buttons and labels

```
Comment
Cancel
Reply
Resolve
Reopen
Request changes
Approve
Approve anyway
Reload the plan
Retry
End session
Discard
Keep writing
Install command
```

### Empty state — no comments anywhere

```
No comments yet

Select any text in the plan to comment on it. In a diagram, click a node or an
edge. Press ? for the keyboard shortcuts.
```

### Empty state — none on this stage

```
No comments on this stage.
```

### Orphaned thread

Section header in the rail, where N is the count:

```
Orphaned — 1
```

The explanation, shown once directly under that header at 13px/20px:

```
The plan was rewritten and this text is no longer in it. Your comments are kept
with the words you quoted, and nothing was deleted.
```

Per-card revision line, 12px/16px, under the quote. The number is the revision
the thread was orphaned **at**, which is the stage's current revision at the
moment the re-anchor failed, not the revision the anchor was created at:

```
orphaned since revision 4
```

### Moved thread

Card revision line, 12px/16px, under the quote. The second form is used when
`anchor_confidence` is below 0.95:

```
moved · revision 2 to 3
moved · revision 2 to 3 · close match
```

### Mermaid asset absent

Shown once directly above each degraded diagram block, 13px/20px, in a
`--color-surface` block with 12px 16px padding and 6px radius:

```
Mermaid is not installed, so this diagram is shown as source. Every node and
edge below is still commentable.

grogu review assets --install
```

The command is a `<code>` element on its own line with a `Copy` text button to
its right; the reader can also select it.

### Unresolved diagram anchor

Shown above the chip row that appears under a rendered diagram:

```
Unresolved anchors — the diagram no longer draws these, so comment on them here.
```

### Stage not written

`{stage}` is the stage the tab names, in lower case. Never a stage the reader
is not looking at:

```
Not written yet

The architect writes the {stage} plan before this stage can be reviewed.
```

On the design tab: `The architect writes the design plan before this stage can
be reviewed.`

### Stage not needed

```
Not needed

The architect recorded that no {stage} stage is warranted for this plan.
```

### Stage sealed for this role

`{stage}` is the tab's stage and `{role}` is the effective role. Both are
substituted; neither is ever hard-coded, because the same panel is shown for
testing and for evaluation and the reader is told which one they are looking
at:

```
Sealed

The {stage} plan is written for the tester and is not readable by the {role}.
An implementation written against its own tests only proves the tests were
satisfiable.
```

On the evaluation tab for an engineer: `The evaluation plan is written for the
tester and is not readable by the engineer.`

### Request changes — dialog

```
Request changes

3 open comments go to the architect as one steering note. The plan moves to
needs_review and work stays blocked until the architect rewrites it.

Covering note (optional)
[ textarea, placeholder: What the architect should understand before reading the comments ]

                                             Cancel   Request changes
```

Round line under the header after sending, 13px/20px on `--color-surface`:

```
Round 1 is with the architect. Add a comment to open round 2.
```

Request changes button when disabled. There are two reasons and they are two
strings, used as `title` and as `aria-describedby` text. The first is for a
review with nothing open; the second is for a round already sent:

```
No open comments to send.
Round 1 is with the architect. Add a comment to open round 2.
```

### Approve — dialog, no open threads

```
Approve this plan

Approval releases the engineer and the tester to build p-20260904-6095a8. It is
recorded against your name and this workspace cannot undo it.

Note (optional)
[ textarea ]

                                                    Cancel   Approve
```

### Approve — dialog, open threads

```
Approve with 3 open comments?

These comments stay open and nobody is required to answer them. Request changes
instead if you want them addressed first.

Approval releases the engineer and the tester to build p-20260904-6095a8.

Note (optional)
[ textarea ]

                                              Cancel   Approve anyway
```

### Approval refused — agent-launched workspace

Shown in place of the Approve button, 13px/20px, `--color-ink-2`, and repeated
verbatim if a `POST /api/approve` returns 403:

```
Approval is the user's. This workspace is reading as the engineer, so it can
read and comment only.
```

### Approval refused by the plan store

```
Could not approve: <the server's message, unchanged>
```

### Save failure

```
Could not save. Your text is still here.
```

with a `Retry` button 8px below.

### Load failure

```
Could not reach the review server

It may have stopped, or the idle timeout expired.

                                                             Retry
```

### Session ended

```
This review session has ended

The local server has stopped. Every comment you wrote was saved as it was
written.

Reopen the workspace with:
grogu review p-20260904-6095a8
```

### Discard dialog

```
Discard this comment?

You have written 42 characters that have not been saved.

                                            Keep writing   Discard
```

### Privacy line — page footer, always present, 12px/16px

```
Local only. Comments are saved to .grogu/plans/p-20260904-6095a8/review.json,
which is git-ignored and is never sent anywhere.
```

### Comment length

Counter appears only at 7,500 characters and above, 12px/16px, right-aligned
under the textarea:

```
7,512 / 8,000
```

At 8,000 the `Comment` button is disabled and this replaces the counter:

```
Comments are limited to 8,000 characters.
```

### Keyboard help dialog

```
Keyboard

j            next comment
k            previous comment
o            next orphaned comment
Enter        open the focused comment, or comment on the focused diagram target
c            comment on the selected text
r            reply to the open comment
e            resolve the open comment
[            previous stage
]            next stage
1 2 3 4      design, implementation, testing, evaluation
?            this list
Esc          close this list, a dialog, or the composer

Approve and Request changes have no shortcut. Reach them with Tab.
```

### Header meta

Plan id, then title. When the effective role is not `reviewer`, one more span:

```
reading as engineer
```

Stage tab labels. Four tabs, fixed order, sentence case. A tab whose stage has
open comments appends the count with a space either side of the separator; at
zero the segment is omitted:

```
Design
Implementation · 3
Testing
Evaluation
```

Stage meta line above the first heading of the body, 13px/20px,
`--color-ink-2`:

```
implementation · revision 3 · 18.4 KB
```

Round chip, header row 2, right. One format, with optional segments in a fixed
order: round, state, open, orphaned:

```
round 1 · 3 open · 1 orphaned
round 1 · changes requested · 3 open · 1 orphaned
```

`· 1 orphaned` is rendered in `--color-attention` and is omitted at zero. `3
open` is omitted at zero. `· changes requested` appears only while the current
round's state is `changes_requested`. With no review at all the chip reads:

```
no comments
```

### Re-anchor summary, rail top, until the next reload

```
1 comment moved and 1 orphaned when the plan changed.
```

### Revision banner

```
The architect rewrote the implementation plan — revision 4.    Reload the plan
```

### Relative time

Under 60 seconds `just now`; under 60 minutes `7m ago`; under 24 hours `3h ago`;
otherwise `Sep 4 14:22`. The `title` attribute always carries the full local
timestamp.

## Tokens

Declared once on `:root`, overridden in a single `@media (prefers-color-scheme:
dark)` block. Nothing else in the stylesheet contains a literal colour.

```css
/* spacing — the only permitted values */
--s-1: 4px;  --s-2: 8px;  --s-3: 12px; --s-4: 16px;
--s-5: 24px; --s-6: 32px; --s-7: 48px; --s-8: 64px;

/* structure */
--measure: 42rem;      /* 672px reading column */
--rail: 17.5rem;       /* 280px thread rail */
--gutter: var(--s-6);  /* 32px between them */
--header-h: 6.25rem;   /* 56px identity row + 44px tab row */

/* type — one sans stack, one mono stack, no web fonts */
--font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
             "Helvetica Neue", Arial, sans-serif;
--font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
             "Liberation Mono", monospace;
--text-xs:   0.75rem;    /* 12px, line-height 16px — meta, counters */
--text-s:    0.8125rem;  /* 13px, line-height 20px — quotes, chrome */
--text-m:    0.9375rem;  /* 15px, line-height 22px — UI, comment bodies */
--text-body: 1.0625rem;  /* 17px, line-height 28px — prose */
--text-code: 0.875rem;   /* 14px, line-height 22px — code, mono */

/* colour — light */
--color-bg:          #FFFFFF;
--color-surface:     #F4F4F2;
--color-ink:         #191919;
--color-ink-2:       #5A5A57;
--color-rule:        #DCDCD8;
--color-rule-strong: #8E8E88;
--color-accent:      #0B57C7;
--color-attention:   #8A4B00;
--mark-line:         #6092DB;
--mark-focus-bg:     #E2EBF8;
--mark-code-bg:      #DDE7F7;

/* colour — dark, prefers-color-scheme: dark */
--color-bg:          #131416;
--color-surface:     #1C1E21;
--color-ink:         #E9E9E7;
--color-ink-2:       #A6A8AB;
--color-rule:        #2E3237;
--color-rule-strong: #6A6F74;
--color-accent:      #86B4FF;
--color-attention:   #E7A94F;
--mark-line:         #4C648A;
--mark-focus-bg:     #212732;
--mark-code-bg:      #232A37;

/* radii */
--r-1: 4px;   /* chips, marks, focus ring rounding */
--r-2: 6px;   /* cards, code blocks, textarea */
--r-3: 8px;   /* dialogs */

/* motion */
--dur-fast: 100ms;   /* mark focus background */
--dur-base: 120ms;   /* card expand and collapse */
--dur-slow: 160ms;   /* dialog and banner */
--ease: cubic-bezier(0.2, 0, 0, 1);
```

**Accent means "you can act here, or this is what you acted on".** It is the
filled Approve button, focus rings, marks, hovered and threaded diagram
targets, and the focused card's left border. Nothing else.

**Attention means one thing only: a comment lost its place in the plan.** It is
the word `orphaned`, the orphaned card's left border, the orphaned section
header, and the orphaned count in the round chip. It is used nowhere else, and
in particular is not used for errors, for `needs_review`, or for approval.

Type ramp by role — weights only 400, 600 and 700:

```
h1   1.625rem / 32px   600    margin-top 0,     margin-bottom var(--s-4)
h2   1.3125rem / 28px  600    margin-top var(--s-6), margin-bottom var(--s-3)
h3   1.0625rem / 24px  700    margin-top var(--s-5), margin-bottom var(--s-2)
h4   0.9375rem / 22px  700    margin-top var(--s-4), margin-bottom var(--s-2)
h5   0.9375rem / 22px  600    --color-ink-2
h6   0.9375rem / 22px  600    --color-ink-2
p                      400    margin-bottom var(--s-4)
ul ol                  400    margin-bottom var(--s-4), padding-left var(--s-5),
                              li margin-bottom var(--s-2), nested list margin-top var(--s-2)
blockquote             400    padding-left var(--s-4), 2px left border --color-rule-strong
pre                           background --color-surface, padding var(--s-3) var(--s-4),
                              radius var(--r-2), overflow-x auto, margin-bottom var(--s-4)
code (inline)                 --text-code, background --color-surface, padding 1px 4px, radius var(--r-1)
table                         --text-m, border-collapse collapse, overflow-x auto,
                              th 600 left-aligned, th/td padding var(--s-2) var(--s-3),
                              1px bottom border --color-rule on every row
hr                            1px --color-rule, margin var(--s-6) 0
a                             --color-accent, underline 1px with 2px offset
```

Section gap between top-level blocks is 16px; between an `h2` and the block
before it, 32px. Rail card padding is 12px 16px, card gap 8px, minimum gap
between de-collided cards 8px.

Motion is used in exactly four places, each explaining a relationship: the card
expanding to its own content height (`--dur-base`), the mark's focus background
appearing when its card is focused (`--dur-fast`), the dialog and its backdrop
appearing (`--dur-slow`), and the revision banner entering with
`translateY(-4px)` to 0 (`--dur-slow`). There is no shimmer on the skeleton, no
transition on hover, no page transition and no scroll animation other than the
browser's own smooth scroll.

## Accessibility

Contrast, measured and stated, light then dark, against `--color-bg`:

```
--color-ink          17.58 : 1     15.16 : 1
--color-ink-2         6.92 : 1      7.73 : 1
--color-accent        6.56 : 1      8.76 : 1
--color-attention     6.80 : 1      8.94 : 1
--color-rule-strong   3.29 : 1      3.63 : 1
--mark-line           3.17 : 1      3.07 : 1
--color-ink on --color-surface       15.97 : 1    13.74 : 1
--color-ink on --mark-focus-bg       14.63 : 1    12.33 : 1
--color-ink on --mark-code-bg        14.11 : 1    11.85 : 1
```

Every text colour clears 4.5:1. Every non-text indicator that carries meaning —
the mark underline, control borders, the focus ring, the diagram stroke — clears
3:1. `--color-rule` at 1.38:1 is decorative separation only and never carries
meaning on its own.

Focus. Every focusable element takes `outline: 2px solid var(--color-accent);
outline-offset: 2px; border-radius: var(--r-1)`. `:focus-visible` is used, the
outline is never set to `none` anywhere in the stylesheet, and the accent ring
clears 3:1 against both `--color-bg` and `--color-surface`.

Tab order, once per page: skip link (`Skip to the plan`, visible on focus,
moves focus to the document region) → Request changes → Approve → stage tab
list (one stop; Left/Right and Home/End move between tabs, `role="tablist"`
with roving `tabindex`) → revision banner button when present → the document
region → marks and diagram targets in document order → the composer when open
→ rail cards in document order → the resolved disclosure → End session. The
header actions come before the tabs because they are on the row above them; a
disabled action is skipped, which is the platform's behaviour and is correct.
A scrollable `<pre>` taking focus is the browser's own keyboard-scroll
affordance and is left alone.

Roles and names. Each mark is `<mark tabindex="0" role="button">` with
`aria-label` `comment on "the first 40 characters of the quote"`, and for an
overlap `2 comments on "…"`. A diagram node is `role="button"` with
`aria-label` `node Parse the plan, comment`; an edge, `edge Parse the plan to
Render HTML, comment`; a subgraph, `subgraph Server, comment`; each appends `,
1 comment` when it carries threads. The rail is `<aside aria-label="Comments">`;
each card is `<article aria-label="Comment c3, orphaned">`. The round chip and
the re-anchor summary sit in an `aria-live="polite"` region, so a re-anchor
after a reload is announced. Save failure and load failure are
`role="alert"`. Both dialogs are native `<dialog>` opened with `showModal()`,
which gives focus trapping, Escape and inertness without a reimplementation.

Reduced motion. `@media (prefers-reduced-motion: reduce)` sets every
`transition-duration` and `animation-duration` to `1ms`, sets `scroll-behavior:
auto`, drops the banner's `translateY`, and makes card expansion an immediate
height change. No state is conveyed by motion alone, so nothing is lost.

Dynamic type. Every size above is `rem` against the browser's root size; no
`px` font size and no `px` width appears outside the token block. At a 200%
root size the layout crosses its own breakpoint and becomes the single-column
drawer arrangement, which is the intended result, and the reading column never
requires horizontal scrolling.

Colour independence. Anchored, moved, orphaned, resolved and unresolved are each
distinguishable without colour: solid line, dashed line, no line plus a word, a
word, a word. Every state that has a colour also has a word in a fixed position.

Selection. The browser's own selection colour is not overridden, so the reader
sees the platform's selection while the composer is open.

## Layout

1280x800, comments present, one orphaned:

```
+----------------------------------------------------------------------------+
| p-20260904-6095a8  Interactive plan review workspace   [Request changes][Approve] |
| Design  Implementation·3  Testing  Evaluation        round 1 · 3 open · 1 orphaned |
+----------------------------------------------------------------------------+
|            |                                       |                       |
|            |  implementation · revision 3 · 18.4 KB|  Orphaned - 1         |
|            |                                       |  +-------------------+|
|            |  # What this is                       |  | "the engineer     ||
|            |                                       |  |  must not read"   ||
|            |  A local, browser-based surface for   |  | orphaned since 4  ||
|            |  reading a Grogu plan and ==commenting|  | charlie - 2h ago  ||
|            |  on it==, launched from the CLI.      |  | this contradicts  ||
|            |                                       |  +-------------------+|
|            |  ## Invariants                        |                       |
|            |                                       |  +-------------------+|
|            |  - **I1.** Markdown is ==canonical==. |  | "commenting on it"||
|            |  - **I2.** Every stage read goes ...  |  | charlie - 5m ago  ||
|            |                                       |  | which stage?      ||
|            |  +---------------------------------+  |  +-------------------+|
|            |  | fenced code, --color-surface    |  |                       |
|            |  +---------------------------------+  |  +-------------------+|
|            |                                       |  | "canonical"       ||
|            |     [ diagram: nodes and edges ]      |  | charlie - 5m ago  ||
|            |                                       |  +-------------------+|
|            |                                       |                       |
|            |                                       |  > Resolved - 2       |
|            |  ...                                  |                       |
|            |                                       |                       |
|            |  Local only. Comments are saved to    |                       |
|            |  .grogu/.../review.json, git-ignored  |    [End session]      |
|            |  and never sent anywhere.             |                       |
+------------+---------------------------------------+-----------------------+
  side margin        672px measure              32       280px rail
```

Composer open, aligned to the selection's top:

```
   ...the reader selects a phrase and the button appears below it...

   which is ==the phrase being selected==
                 [ Comment ]                        +-------------------+
                                                    | "the phrase being ||
                                                    |  selected"        ||
                                                    | +---------------+ ||
                                                    | | textarea      | ||
                                                    | | 3 rows min    | ||
                                                    | +---------------+ ||
                                                    | Ctrl+Enter saves  ||
                                                    |   Cancel [Comment]||
                                                    +-------------------+
```

Degraded diagram block:

```
   +----------------------------------------------------------+
   | Mermaid is not installed, so this diagram is shown as     |
   | source. Every node and edge below is still commentable.   |
   |   grogu review assets --install               [ Copy ]    |
   +----------------------------------------------------------+
   +----------------------------------------------------------+
   | flowchart LR                                             |
   |   Parse[Parse the plan] --> Render[Render HTML]           |
   |   Render --> Anchor[Anchor comments]                      |
   +----------------------------------------------------------+
   Nodes      ( Parse the plan ) ( * Render HTML ) ( Anchor comments )
   Edges      ( Parse the plan -> Render HTML ) ( Render HTML -> Anchor comments )
   Subgraphs  ( Server )

   ( * ) = the accent dot shown on a chip that carries a thread
```

Rendered diagram, node and edge affordances:

```
      +----------------+          +----------------+
      | Parse the plan |--------->| Render HTML  * |   * accent dot: has a thread
      +----------------+   ^      +----------------+
             ^             |
      2px accent stroke    2.5px accent stroke along the whole edge
      on hover or focus    when the edge carries a thread
```

Approve dialog, 480px wide, centred, `--r-3` radius, backdrop
`rgba(0,0,0,0.32)`:

```
   +--------------------------------------------------+
   | Approve with 3 open comments?                    |
   |                                                  |
   | These comments stay open and nobody is required  |
   | to answer them. Request changes instead if you   |
   | want them addressed first.                       |
   |                                                  |
   | Approval releases the engineer and the tester to |
   | build p-20260904-6095a8.                         |
   |                                                  |
   | Note (optional)                                  |
   | +----------------------------------------------+ |
   | |                                              | |
   | +----------------------------------------------+ |
   |                                                  |
   |                    Cancel   [ Approve anyway ]   |
   +--------------------------------------------------+
```

CLI launch, the exact intended output:

```
$ grogu review p-20260904-6095a8
p-20260904-6095a8  draft  Interactive plan review workspace
  reading as reviewer: design, implementation, testing, evaluation
  diagrams: mermaid not installed; `grogu review assets --install`
  http://127.0.0.1:53412  opened in your browser
  ctrl-c ends the session
```

With `--no-open`, the fourth line carries the one-time token instead:

```
  open http://127.0.0.1:53412/?t=Ux1nQ7_o0m3s4b9KpVzR2tYw6hLdG8fA1cE5jN0iM4s
```

`grogu review list`, two lines per thread, columns aligned and field order
fixed:

```
$ grogu review list p-20260904-6095a8
c1  open      implementation  orphaned  "the engineer must not read the testing plan"
                                        charlie: this contradicts the seal
c2  open      implementation  anchored  "commenting on it"
                                        charlie: which stage does this mean
c3  open      design          moved     node:Parse "Parse the plan"
                                        charlie: this node does two things
c4  resolved  design          anchored  "the anchor model"
                                        charlie: covered by I6
```

`grogu review status`:

```
$ grogu review status p-20260904-6095a8
p-20260904-6095a8  draft  Interactive plan review workspace
  review: round 1, 3 open, 1 orphaned
  rounds: 1 changes_requested at 2026-09-04T22:41:00+00:00
  threads: design 2, implementation 2
```

`plan status` gains exactly one line, unchanged from the implementation plan:

```
  review: round 2, 3 open, 1 orphaned
```

## Acceptance criteria

- The stage body renders at 17px/28px in a column whose `max-width` is `42rem`,
  centred, with no border, card or background behind it.
- The stylesheet contains no colour literal outside the `:root` and
  `prefers-color-scheme: dark` token blocks, except two neutral alpha-black
  values that are not part of the palette: `rgba(0,0,0,0.32)` for the dialog
  backdrop and `rgba(0,0,0,0.12)` for the selection button's drop shadow.
- Every anchor the front end sends to `POST /api/threads` carries `body_digest`
  and `revision` for the stage it was taken from, and creating a thread from a
  text selection, a diagram node, a diagram edge and a degraded-mode chip each
  return 200 and produce a card in the rail.
- A stage whose progress state is `pending` but whose `written` flag is true
  renders its body, its marks and its threads; only `written == false` shows
  the `Not written yet` panel.
- The sealed panel names the stage of the tab it is shown on and the effective
  role, so the evaluation tab does not describe the testing plan.
- The stage tab for a stage with open comments reads `Implementation · 3`, with
  a space either side of the separator.
- The only filled `--color-accent` background on the page is the Approve
  button.
- `--color-attention` appears only on the word `orphaned`, the orphaned card's
  left border, the orphaned section header, and the orphaned count in the round
  chip.
- An anchored mark renders a 2px solid `--mark-line` line under its text; a
  moved mark renders the same line dashed 3px on, 3px off; an orphaned thread
  renders no mark.
- A card for a moved thread shows the word `moved`, and a card for an orphaned
  thread shows the word `orphaned`, in the card's top-right corner.
- Orphaned threads appear in a rail section above every other card, that section
  cannot be collapsed, and its explanation text matches the Copy section
  verbatim.
- After a reload following a plan rewrite, orphaned cards are expanded and the
  rail is scrolled to the first of them.
- A mark inside a fenced code block renders as a `--mark-code-bg` background,
  not as an underline.
- A selection spanning three table cells produces one thread and three marks,
  and no mark crosses a cell boundary.
- A selection covering 90% or more of a heading's text anchors the whole
  heading.
- Text covered by two threads renders two stacked 2px lines separated by a 1px
  gap, and paragraph line height stays 28px.
- Clicking an overlapped mark focuses the most recently created thread covering
  it, and every thread covering that run is shown adjacent in the rail.
- Each thread card's top is positioned at its mark's vertical offset, and
  colliding cards are pushed down with at least 8px between them.
- Hovering a diagram node strokes its shape 2px in `--color-accent`; hovering an
  edge strokes its path 3px in `--color-accent`.
- A diagram node carrying a thread shows an 8px `--color-accent` dot at its
  top-right; an edge carrying a thread is stroked 2.5px in `--color-accent`
  along its whole path.
- With `/vendor/mermaid.min.js` absent, each Mermaid block renders the notice,
  the source in a `<pre>`, and chip rows headed `Nodes`, `Edges` and
  `Subgraphs`; every chip opens the composer and produces a semantic anchor.
- The notice contains the literal string `grogu review assets --install`.
- A node comment's card header reads `node · <label>`; an edge comment's reads
  `edge · <from label> → <to label>`, with ` ("<edge label>")` appended when the
  edge is labelled. Both endpoints are the nodes' **labels**, not their
  Mermaid ids, and read the same in a card and on a chip.
- Every literal string in the Copy section appears in the built interface
  character for character.
- No `alert`, `confirm` or `prompt` is used; both confirmations are native
  `<dialog>` elements opened with `showModal()`.
- The whole review — switch stage, open a thread, reply, resolve, request
  changes, approve — is completable with the keyboard alone, and the shortcut
  list matches the keyboard help dialog verbatim.
- Approve and Request changes have no single-key shortcut.
- Every focusable element shows a 2px `--color-accent` outline at 2px offset,
  and `outline: none` appears nowhere in the stylesheet.
- Measured contrast of each token pair matches the table in the Accessibility
  section within 0.05.
- With `prefers-reduced-motion: reduce`, no transition or animation exceeds 1ms
  and no scroll is animated.
- With the root font size at 200%, the reading column requires no horizontal
  scrolling and the layout is single-column with the drawer.
- At 1280x800 and 1440x900 the reading column, gutter and rail are 672px, 32px
  and 280px, and the page has no horizontal scrollbar.
- Below 1180px the rail becomes a 320px right drawer opened by a header button
  labelled `Comments — 3`; below 880px the drawer is full width and the stage
  tabs become a native `<select>` labelled `Stage`.
- The privacy line is present in the footer on every load and names
  `review.json` and the word `git-ignored`.
- A `POST` failure on any composer leaves every typed character in place and
  shows `Could not save. Your text is still here.`
- Two consecutive `/api/plan` failures replace the page with the session-ended
  screen, which contains `grogu review p-20260904-6095a8`.
- When the effective role is not `reviewer`, the header shows `reading as
  <role>`, the Approve button is replaced by the refusal copy, and no sealed
  stage body is present anywhere in the DOM or in any response body.
- A stage the role cannot read shows the sealed copy and triggers no request for
  that stage.
- A revision change never re-renders the document until `Reload the plan` is
  activated, and while no revision change is pending the banner occupies no
  vertical space at all — no empty strip appears under the header.
- A list item whose source text wraps onto a following indented line renders as
  one list item, and an indented list beneath it renders as a nested list
  inside that item rather than as a new top-level list.
- The page loads and every state above is reachable with the CSP from the
  implementation plan and with no inline script and no inline event handler.
- `grogu review`, `grogu review list` and `grogu review status` produce output
  matching the fenced blocks in Layout, with the same column positions.
- No page in the workspace issues a network request to any origin other than its
  own, and no `<img>` element is generated from plan Markdown.

## Left to the engineer

- Class names, DOM structure, module internals, and how card positions are
  computed and de-collided, so long as the resulting positions satisfy the
  criteria above.
- The de-collision algorithm's behaviour when the rail is shorter than the sum
  of its cards: any policy that keeps every card reachable by scrolling is
  acceptable.
- Poll interval and back-off for `/api/plan` beyond the requirement that polling
  pauses while the document is hidden; 5000ms is a starting value, not a
  specification.
- The exact truncation point of a quote in a card (two lines is the target; the
  method is yours), and the `text-overflow` mechanism used.
- Skeleton bar count and widths past the first screenful.
- Whether the selection button is a `<button>` in a positioned container or a
  popover, provided it is keyboard-reachable and does not move the prose.
- The `Copy` button's clipboard mechanism and its transient label after copying.
- Print styles: not specified, and not required.
- The chip row's wrapping behaviour and chip max-width.
- Any additional `aria-describedby` text beyond the strings named in Copy.
- Whether resolved threads are removed from or retained in the DOM while the
  disclosure is closed.
