# The `.plan` document graph and its Figma-style editor — design

The architect fixed the semantics; this stage fixes the surface. It is written
to be implementable with React 19, TypeScript and `@xyflow/react` 12 for the
node graph, plus a single SVG overlay for regions, marquee, connectors in
flight, and annotation strokes. Where a section names a library primitive it is
so the engineer knows which lever to reach for; the *contract* is the behaviour
described here, not the primitive.

## Surfaces

One line each. The five **modes** — Control room, Document, Canvas,
Dependencies, Revision — share one **App shell** and one **workspace chrome**.
Modal surfaces live above. **Control room is a workspace-level mode** (across
plans); the other four are plan-level modes. Both live under the same shell so
the control room never feels like a different app.

* App shell — top bar, mode tabs, workspace body, comment/agents rail, status bar.
* Top bar — home crumb, plan crumb, stage tabs, mode tabs, search, agents
  chip, presence slot, user menu.
* **Control room mode** — agent grid or list, filter chips, drill-down audit
  timeline drawer, feedback composer.
* Document mode — reading column, inline marks, comment rail, inspector drawer.
* Canvas mode — layers panel, canvas viewport, floating contextual toolbar,
  inspector drawer, minimap, zoom control.
* Dependencies mode — canvas viewport with `dagre`-arranged nodes, legend,
  cycle report drawer, minimap.
* Revision mode — revision timeline, side-by-side compiled diff, restore-as-proposal.
* **Agent card** — one card per live agent identity; the primary unit of the
  control room.
* **Audit timeline drawer** — passive-only event stream for one agent or one
  plan.
* **Feedback composer** — scoped feedback to agent, role, plan, or role+plan,
  with a binding switch.
* **Delivery ledger** — durable, listable log of every feedback message the
  user has sent, its target, its delivery state and its acknowledgement.
* **Agents rail** — the right-panel Agents tab inside plan modes; scoped to
  the current plan and stage.
* Comment rail — anchored thread cards for the current stage, filter chips.
* Comment thread modal (or full-panel on narrow widths) — timeline of replies,
  reply composer, resolve, reopen, promote-to-directive, **Ask Grogu to revise**.
* Proposal preview overlay — node diff, compiled Markdown diff, impact set,
  accept and reject actions.
* Impact preview popover — primary and consequential changes for the current
  pending edit or selected proposal.
* Compiled Markdown preview overlay — the projection for the current role and
  stage, source digest visible.
* Search palette — text and id search across nodes, threads and revisions.
* Command palette — every action reachable by name, keyboard-first.
* Ask-Grogu-to-revise composer — instruction composer.
* Migration overlay — one-way migration progress, semantic equivalence result,
  revert affordance.
* Sealed-stage panel — parameterised copy naming stage and role.
* Unwritten-stage panel — parameterised copy naming stage.
* Presence stub — presence pills reserved space, showing only local session.
* Toast rail — autosave, offline, save-failed, conflict-rebased, shutdown.
* Empty / loading / permission-denied / stale-revision / oversized-import /
  ambiguous-anchor / unresolved-blocker / missing-asset / invalid-package /
  crash-recovery panels.

## Hierarchy

For each surface: one primary action, secondary subordinated, destructive kept
away.

* **App shell** — primary is the workspace body (Control room, Document,
  Canvas, Dependencies or Revision); mode tabs and stage tabs are secondary;
  the only destructive affordance in chrome is `End session`, kept in the user
  menu at the far right, never adjacent to save or approve.
* **Control room** — primary is the agent grid and the currently focused
  agent card; the audit timeline drawer and the feedback composer are
  secondary. The only destructive affordance in this mode is `Abandon
  feedback` in the composer, which is a plain link, kept away from `Send`
  and `Send as binding`.
* **Document mode** — primary is the reading column; the comment rail and
  inspector drawer are secondary. Delete-node lives inside the inspector, at
  the bottom, behind a two-step confirmation and never next to `Save`.
* **Canvas mode** — primary is the canvas viewport and the currently selected
  object. The floating contextual toolbar carries the actions for the selection
  and nothing else. Layers panel and inspector drawer are secondary. `Delete`
  is on the contextual toolbar's overflow menu, not on its main row.
* **Dependencies mode** — primary is the arranged graph and the impact
  highlight for the current selection. Legend and cycle report drawer are
  secondary. No destructive action in this mode; edits happen elsewhere.
* **Revision mode** — primary is the compiled diff for the selected revision;
  the timeline is secondary. `Restore as proposal` is the strongest action and
  sits in the diff header, not the timeline; it produces a proposal, never a
  destructive reset.
* **Comment thread** — primary is `Reply`. Secondary: `Resolve`, `Reopen`,
  `Promote to directive`, `Ask Grogu to revise`. `Delete thread` does not
  exist; a thread is only ever resolved or orphaned.
* **Proposal preview** — primary is `Accept proposal`; `Reject with reason` is
  a secondary button that is *not adjacent* to Accept — there is a divider and
  a 24 px gap, and Reject opens a required-reason field before it commits.
* **Migration overlay** — primary is `Migrate`; the secondary action is
  `Cancel`; `Revert to legacy` only appears after a successful migration, in
  the overlay footer, requires a typed plan id to confirm.

## Flow

### Opening the app

The app has two entry points to the same shell:

* **Home** (`/`) is the **Control room**. Nothing plan-scoped is chosen; the
  agent grid fills the workspace body; the plan crumb reads `— no plan
  selected —` and stage/mode tabs are dimmed. Selecting a plan from the home
  crumb or from an agent card switches the shell into plan-scoped mode.
* **Plan** (`grogu plan doc open <id>`) opens the plan and defaults to the
  mode the CLI passed or the last mode the user was in.

Switching between Control room and any plan mode is instantaneous; selection
by agent id or by plan object id is preserved.

### Opening a plan

`grogu plan doc open <id>` prints a `http://127.0.0.1:<port>/?t=<token>` URL
and, unless `--no-open`, opens it. The page exchanges the token for a session
cookie (`303 → /`), then boots. A second visit with the same token returns
`403` and the page shows the `Session token spent` panel (below).

Boot sequence, visible to the user:

1. **Skeleton (0–150 ms).** Top bar, mode tabs and body scaffold at their real
   dimensions with muted neutrals; no spinners. `aria-busy="true"` on `<main>`.
2. **First paint (≤ 400 ms typical).** `/api/doc?stage=<current>&include=normative`
   returns; the document and the layer list populate.
3. **Ready.** SSE `/api/events` connects; the status bar shows
   `Connected · r0031 · autosaved`.

If `/api/doc` returns `403` the whole workspace shows the permission-denied
panel and does not connect SSE. If it returns `409 stale` (rare on open) the
workspace shows the stale-revision panel.

### The four modes: switching

Mode tabs preserve selection by id. Switching to a mode that cannot render the
current selection (a `thread` node has no canvas presence, for example) keeps
the selection in the header but shows an inline notice in that mode's body:
`This item has no canvas representation. It stays selected in the Document.`

### Editing on the canvas

1. Point at empty canvas: cursor is default. Drag from empty canvas: marquee
   select.
2. Point at a node: cursor is `grab`. Click selects; drag moves; `Alt`-drag
   duplicates. `Shift`-click extends the selection. `Cmd`-click toggles.
3. Drag from a handle: connector-in-flight; drop on a target handle to link.
   Drop on empty canvas to open the "new node from this edge" quick menu.
4. On mouse-up after a gesture, one revision is written (`/api/patch` with
   the ops the gesture produced). The status bar shows `Saving…` and then
   `Saved · r0032`. On `409` the status bar shows `Rebasing…`, the pending
   ops are replayed, and if they still apply the status bar returns to `Saved`;
   if not, a conflict card appears in the rail.
5. On cancel (`Esc`) mid-gesture the drag is abandoned with no revision.
   No revision is ever written for an in-flight gesture.

### Editing a text run

1. Double-click a title or a body renders it editable in place.
2. `Esc` cancels, `Cmd/Ctrl-Enter` commits, focus loss commits.
3. Commit sends one revision with a single `set` op on `title` or `body`. If
   the server returns `409 stale` and the local edit still applies against the
   new base, it is silently rebased; if not, the field turns amber, keeps the
   local text, and shows `Content changed since you started. Keep yours or
   discard and re-open.` with `Keep` and `Discard` buttons.

### Adding a contextual comment

1. Text mode: select text, click the anchored `Comment` chip that appears
   6 px above the selection end. Compose, `Post`. A thread appears in the rail
   with a marker connected by a hairline to the selection.
2. Canvas mode: click `Comment` on the contextual toolbar. Cursor becomes a
   crosshair. Drag out a rectangle, ellipse, arrow, or freehand region; the
   `Region` is created and a thread with an `anchors` selector opens.
3. Object mode: with a selection, `C` opens a thread anchored to
   `{type: "object", id, part}`. The composer offers **Discussion**,
   **Question**, **Suggested change**, **Blocking** and (architect only)
   **Directive** as the thread's kind.
4. On cancel (`Esc`), the thread is not created and the marker vanishes.

### Ask Grogu to revise

1. From any thread, `Ask Grogu to revise` opens the instruction composer with
   the thread's quoted context pinned. Enter an instruction, submit.
2. The server returns a proposal (`pr-N`). The `Proposal preview` overlay
   opens.
3. The overlay shows: node-level diff (left), compiled Markdown diff (centre)
   and impact set (right). Primary changes and consequential changes are
   distinguished; see "Impact preview" below.
4. `Accept proposal` applies the ops as one revision with
   `origin: "proposal:pr-N"`. `Reject with reason` requires a reason and keeps
   the proposal in `rejected` state.
5. If the proposal's `base` is not `HEAD` the header shows `Stale — rebase or
   abandon`; there is no auto-merge.

### Directive promotion

A discussion, question, suggested-change or blocking thread with `Promote to
directive` in its menu; architect only. Promotion opens a two-step form:
`Directive text · Audience · Binding (must|should)`; on confirm the promotion
writes a `directive` node with `attrs.origin = "thread:<tid>"` and links it
with `derives_from` from the source thread's anchor, then resolves the thread
with a note pointing at the new directive id. The rail card of the promoted
thread turns to the resolved state with a chip `↗ dir-N`.

### Save, undo, offline

* Every completed gesture, commit or accepted proposal writes one revision.
  The status bar shows `Saved · r<NNNN>` and the toast rail shows
  `Autosaved` for 2 s.
* `Cmd/Ctrl-Z` undoes; `Cmd/Ctrl-Shift-Z` redoes. Undo is implemented as a
  new revision (`origin: "undo"`) that inverts the last user revision;
  history is append-only. The undo depth is unbounded within the session and
  jumps to a labelled marker in the toast rail: `Undid: rename 'task-3'`.
* If the network fails or the server returns 5xx: the status bar turns amber,
  `Offline · edits queued (N)`. Pending intents remain live; new edits are
  queued locally and marked with a hairline amber underline. On reconnect the
  queue is drained one revision at a time, in order, showing `Reconnecting…`
  then `Synced · r<NNNN>`.
* If the tab is closed with an unflushed queue, the next boot reads the
  queue from `localStorage` (per-plan namespace) and shows a
  `Restore unsaved edits?` panel. The queue is a list of *intents* (the ops
  and the base the user last saw), not compiled output; the user can `Apply`,
  `Discard` or `Show what changed` before deciding.

### Semantic-equivalence failure

The compile is deterministic and checked. If a save produces a compiled
artifact that fails `parse(render(ir)) == ir`, the server returns 422 with
`{"error":"nonequivalent", "field":"…"}` and the client shows a full-width
banner in the status bar (amber, not red — this is a bug in the compiler, not
a user mistake): `The compiled Markdown does not round-trip. Your change was
not saved. Copy diagnostic for a bug report.` The banner's primary action
copies `field`, `revision`, `base`, and the plan id to the clipboard.

### Migration

Opening a legacy plan shows the migration overlay before the workspace
mounts. The overlay is modal; nothing else is interactive.

1. **Explain.** `This plan uses the old on-disk layout. Migrating converts
   it to the .plan package in place. Every file is preserved. You can revert
   until you make a new revision.`
2. **Migrate.** Progress bar with the actual step count from the API
   (`copy → parse → import diagrams → import threads → compile → verify`).
3. **Verify.** If semantic equivalence holds, `Ready`. If not, the overlay
   turns red: `Migration produced a non-equivalent artifact. The legacy
   directory is untouched. Show diff.` Only `Show diff` and `Cancel` are
   available.
4. **Revert.** After a successful migration, the overlay footer keeps a
   `Revert to legacy…` link; clicking it opens a typed-confirmation panel
   naming the revisions that will be discarded.

### Watching an agent

1. In Control room, click an agent card. The right drawer opens with the
   audit timeline for that agent, scoped to the last 24 hours by default with
   a range control. The card is highlighted with a 2 px accent outline.
2. Events stream in via SSE `/api/agents/events`. New events insert at the
   top of the timeline; freshness values on cards update every 5 s.
3. Clicking any event that names plan objects (revision, node id, edge id)
   navigates the workspace to the plan and selects the affected objects,
   preserving the Control room drawer so the user can flip back with `Esc`.
4. **What the timeline shows** — only observable, passive events:
   `revision`, `patch`, `tool call (name + duration + success)`, `gate
   passed`, `gate blocked`, `steering delivered`, `feedback acknowledged`,
   `error`, `disconnected`, `connected`, `role bound`, `workstream started`,
   `workstream merged`. Every row has an event id and a timestamp.
5. **What the timeline never shows** — hidden reasoning, chain-of-thought,
   speculative prompts, raw tool arguments that could contain user content or
   credentials, model responses that were not written to disk. Tool rows show
   name and outcome only; if the tool wrote a patch, the patch id is a link.
   Under each such row a fixed note: `Arguments not shown. Grogu never
   records prompts or reasoning.`

### Sending feedback

1. From the composer at the bottom of the drawer, or from `⌘⇧M` anywhere:
   choose a **target scope**: `This agent`, `All <role>`, `This plan`,
   `<role> on this plan`. A single scope is required; the choice is a
   segmented control.
2. Write the message. A **Binding** toggle sits next to `Send`. Binding
   feedback: `Blocks the target's next gate until acknowledged.` The toggle
   label reads `Binding · closes the next gate` when on, `Advisory` when off.
3. On `Send`, the composer commits to `/api/feedback` and shows a
   deterministic tracking chip in the drawer:
   `f-023 · sent · <2s ago>`. The chip advances through `sent → routed →
   delivered → acknowledged` (or `sent → routed → undeliverable`) as
   observable events come back. Each transition is time-stamped; the whole
   chain is durable and visible in the **Delivery ledger**.
4. Routing is visible. A binding feedback message on `<role> on this plan`
   shows in three places:
   * On the target agent's card, a small badge `⚑ 1 unread` when unread and
     `⚑ 1 acknowledged` after acknowledgement.
   * On the plan's status bar, `Feedback · 1 binding pending`.
   * On the affected gate row in Revision mode, `Blocked by f-023
     (binding)`.
5. **Binding closure of gates.** The server records the feedback as a
   `steering` node with `attrs.binding = true` and adds a `blocks_gate` edge
   to the specific gate. `grogu plan gate` reports the plan blocked with
   `awaiting binding feedback acknowledgement: f-023 "…"`. The gate reopens
   the moment the target agent acknowledges the feedback (a passive event
   raised by the agent side, not inferred by the UI); the delivery chip
   flips to `acknowledged` and the plan status bar clears.
6. **Cancel or replace.** The sender may cancel an unacknowledged binding
   feedback from the delivery ledger; the gate reopens with the reason
   `feedback f-023 withdrawn`. Editing after send is not offered; write a
   replacement.

### Nudging a stuck agent

1. A `Nudge` button appears on any agent card in the `Stuck` state (see
   States).
2. It opens the feedback composer pre-filled with the scope `This agent` and
   the message
   `You appear stuck at <last action> for <duration>. What would help?`.
   The user edits and sends as `Advisory` by default.

## Modes — desktop layouts

Everything is expressed on an 8 px spacing scale. Chrome heights are fixed;
side panels can be resized within stated bounds; the workspace body flexes.

### The App shell at 1440 × 900 (base desktop)

```
┌──────────────────────────────────────────────────────────────────────┐  48
│ Top bar                                                              │
├──────────────────────────────────────────────────────────────────────┤  40
│ Ctrl room │ Stage tabs │ Mode tabs │ Search │ Agents │ Presence Menu │
├──────────┬─────────────────────────────────────────────┬─────────────┤
│          │                                             │             │
│ Left     │ Workspace body                              │ Right       │
│ panel    │ (Control room, Document, Canvas,            │ panel       │
│ (Layers, │  Dependencies, Revision)                    │ (Inspector, │
│ Outline  │                                             │ Comments,   │
│ or       │                                             │ Agents)     │
│ Filters) │                                             │             │
│          │                                             │             │
│ 280 px   │ flex                                        │ 360 px      │
├──────────┴─────────────────────────────────────────────┴─────────────┤  32
│ Status bar (plan · revision · autosave · agents chip)                │
└──────────────────────────────────────────────────────────────────────┘
```

Fixed heights: top bar 48 px, stage/mode bar 40 px, status bar 32 px.
Left panel: default 280 px, min 240, max 400; drag-resizable via a 4 px
gutter with a 12 px pointer target and cursor `col-resize`.
Right panel: default 360 px, min 320, max 520; same gutter.
Left and right panels can each be collapsed to a 40 px rail with a chevron.
Collapse state is per-mode and persisted in `localStorage`.

The mode bar carries, left to right: **Control room** tab (always leftmost,
`⌘0`), a divider, stage tabs, a divider, plan mode tabs (`Document`,
`Canvas`, `Dependencies`, `Revision`; `⌘1`–`⌘4`), a flexible spacer, search
(320 px input), the **Agents chip** (`Agents · 3 live · 1 stuck`, click to
open Control room filtered to the current plan), the presence slot, the user
menu.

When no plan is selected, stage tabs and plan mode tabs are dimmed and
disabled with `aria-disabled="true"`; the Agents chip switches to
`Agents · 5 live · 1 stuck` (all agents, unfiltered).

### Control room mode

```
┌──────────┬─────────────────────────────────────────────┬─────────────┐
│          │  ┌── filter chips row (40 px) ────────────┐ │             │
│ Roles /  │  ├── agent grid (or list) ────────────────┤ │  Audit      │
│ Plans /  │  │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐   │ │  timeline   │
│ Filters  │  │  │ card │ │ card │ │ card │ │ card │   │ │  drawer     │
│          │  │  └──────┘ └──────┘ └──────┘ └──────┘   │ │             │
│          │  │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐   │ │             │
│          │  │  │ card │ │ card │ │ card │ │ card │   │ │  ┌────────┐ │
│          │  │  └──────┘ └──────┘ └──────┘ └──────┘   │ │  │ compose│ │
│          │  └────────────────────────────────────────┘ │  │ feedback│ │
└──────────┴─────────────────────────────────────────────┴─────────────┘
```

* Left panel: **Filters**. Role chips (`architect`, `designer`, `engineer`,
  `tester`, `reviewer`, `supervisor`), plan chips (recent plans by title
  with id), workstream chips (populated from observable events), state
  chips (`Live`, `Idle`, `Stuck`, `Finished`, `Disconnected`, `Error`),
  freshness bucket chips (`Live · <60 s`, `Idle · 1–5 m`, `Stale · 5–15 m`,
  `Dead · >15 m`). Filters combine with AND across categories, OR within.
  A `Clear all` link appears when any filter is active. Chip counts render
  as small superscripts (`architect · 2`).
* Filter chips row (40 px above the grid): mirrors the active filter set as
  removable chips so the user always sees the current lens; a
  `Grid · List` segmented control on the far right toggles density.
* Agent grid: cards are 320 × 180 px, arranged on an 8 px gutter grid, four
  per row at 1440. Density is **quiet** by default and never dependent on
  colour alone; state is shown by glyph and by a 2 px left border colour.
* List view: 40 px rows with columns `state · role · workstream · plan ·
  freshness · action · failures · blockers · unread`. All columns sortable.
  The list uses `<table>` semantics; on narrow widths it collapses to two-line
  rows with the same information.
* Right drawer: **Audit timeline** for the focused agent (or, if none is
  focused, for the plan filter's aggregate). See "Audit timeline drawer"
  below. Below the timeline is the **Feedback composer**.
* Empty state (no agents at all): a centred panel with the copy under
  "States — Control room". No skeleton is drawn once the API confirms zero.

#### Agent card

```
┌────────────────────────────────────────────────────────────────┐  180
│ ▷ engineer · workstream-web · p-20260905-aab017                │
│ r0031  •  Live · 12s ago                                       │
│ Action: applying patch to canvas layout                        │
│ Tool: xyflow.layout • 340 ms • ok                              │
│ Failures 0  Blockers 1  Unread steering 0             ⚑ 1 sent │
│                                                                │
│           [ Watch ]   [ Send feedback ]   [ Nudge ]            │
└────────────────────────────────────────────────────────────────┘
```

Fixed rows, always in this order (blanks kept as `—` rather than removed so
scanning stays vertical):

* **Row 1** — Kind glyph, role, workstream name, plan crumb (id at
  minimum, title on hover; click navigates to plan).
* **Row 2** — Revision the agent last touched, then a bullet, then the
  freshness label (`Live · 12s ago`, `Idle · 3m ago`, `Stale · 8m ago`,
  `Dead · 22m ago`).
* **Row 3** — Current **Action** in one line, from the last observable
  event. `Idle` if nothing running. Never chain-of-thought.
* **Row 4** — Last **Tool** call: name + duration + outcome (`ok`, `err`,
  `timeout`). Empty if the agent has not called a tool this session.
* **Row 5** — Counters: `Failures N · Blockers N · Unread steering N`;
  right-aligned delivery badge for feedback the user sent to this agent.
* **Row 6** — Actions: `Watch` (open the audit drawer), `Send feedback`
  (open composer scoped to this agent), `Nudge` (only when `Stuck`).

The whole card is a single focusable region with `role="group"` and
`aria-labelledby` to Row 1. Roving tabindex moves between cards; `Enter`
opens the drawer; `F` opens the composer.

#### Audit timeline drawer

Right-side drawer, 480 px wide at 1440, resizable 400–640 px.

* Header: agent identity or plan identity, range control (`Last 1h · 24h ·
  7d · All`), and a `Auto-scroll to newest` toggle.
* Body: reverse-chronological list of events. Each row:
  * icon by event type (revision, patch, tool, gate, steering, feedback,
    error, disconnect, connect, workstream);
  * timestamp (`14:31:07 · 2s ago`);
  * one-line summary;
  * a right-aligned link to the affected plan objects, if any
    (`→ dir-2, task-3`).
* Filter row above the body: chips for event type; a `Hide connect/disconnect`
  toggle; a search box for revision id or node id.
* A **provenance footer** persists at the bottom: `Timeline shows observable
  events. Grogu never records prompts, chain-of-thought, or raw tool
  arguments. What you see here is the same view the tester and reviewer
  see.` This copy is fixed and cannot be dismissed.
* Below the timeline: the **Feedback composer**, described under Flow.

#### Feedback composer

Fixed layout inside the drawer footer (or as a floating panel via `⌘⇧M`):

* Segmented control **Target**: `This agent · All <role> · This plan ·
  <role> on this plan`. The last two only appear when a plan is in scope.
* Textarea, 3 lines default, expandable to 12; placeholder `What would you
  like this <target> to know?`.
* **Binding** switch. Off = `Advisory`. On = `Binding · closes the next
  gate`. When on, an inline explainer appears under the switch:
  `The target's next gate stays blocked until they acknowledge this. You
  can cancel or replace it any time.`
* Footer: `Cancel` (link, left), `Save as draft` (link), then a divider,
  then `Send` primary (or `Send as binding` if the switch is on). The
  destructive `Abandon feedback` is a link far from `Send`, never a button.
* On send, the composer collapses into the tracking chip described under
  Flow, and a fresh empty composer is available.

## Delivery ledger

A modal opened from `⌘⇧L`, from the composer's `View all feedback` link,
and from the user menu.

* Table of every feedback message the user has sent, newest first:
  `f-<id> · target · scope · binding · sent · delivered · acknowledged ·
  actions`.
* Each row expandable to show the message body, the target agent's
  observable acknowledgement event id (link to the audit timeline), and, for
  binding feedback, the gate that was affected.
* Actions: `Copy id`, `Withdraw` (binding only, before acknowledgement),
  `Replace with new`, `Open in Control room`.
* Empty state: `No feedback sent yet.` Body: `Feedback you send to agents
  and roles appears here as a durable record.`
* Every feedback message the user ever sent is retained until the user
  explicitly deletes it; there is no silent expiry.

## Control room integration into plan modes

Inside the four plan-scoped modes (Document, Canvas, Dependencies,
Revision), the control room is not hidden; it stays present in three
places, none of which crowds the primary surface.

* **Status bar chip** — bottom right, immediately left of the connection
  indicator: `Agents · N live · N stuck`. Click to open Control room
  filtered to the current plan. Colour is neutral unless a stuck agent
  exists, in which case the chip gains the warn left border.
* **Right panel Agents tab** — the segmented control at the top of the
  right panel offers `Comments · Inspector · Agents`. The Agents pane
  shows a compact list of agent cards scoped to the current plan; each
  row is a 56 px condensed card with Action, Tool, and Freshness only,
  and a `Watch` button that opens the full drawer.
* **Inspector "Agent activity" section** — appears on any selected node
  or edge whose id has been touched by observable events in the last 24 h.
  Shows up to five recent events with jump-to-timeline links. Empty state:
  `No agent activity on this item in the last 24 hours.`

None of these surfaces exposes hidden reasoning or raw arguments; they use
the same event vocabulary as the drawer.

### Document mode

```
┌──────────┬─────────────────────────────────────────────┬─────────────┐
│          │  ┌─── reading column, 720 px wide ───┐      │  Comment    │
│ Outline  │  │                                    │      │  rail       │
│ tree     │  │  # Directives                      │      │             │
│          │  │  ## Tasks                          │      │  [thread]   │
│          │  │  paragraph, list, code, mermaid,   │      │  [thread]   │
│          │  │  criterion cards…                  │      │             │
│          │  └────────────────────────────────────┘      │             │
│          │  centred, 32 px side margins                 │             │
└──────────┴─────────────────────────────────────────────┴─────────────┘
```

* Left panel: **Outline** — a hierarchical list of stages ▸ sections ▸ node
  titles. Current selection is highlighted; click scrolls the reading column
  and moves focus to that node's heading. Drag re-orders siblings within a
  stage; drop targets are drawn as 2 px accent bars between rows.
* Reading column: 720 px content column, centred in the available body width.
  Content is the compiled Markdown for the current stage and role, rendered by
  the same routine that produces static previews (server-produced HTML,
  vendored fonts, no `dangerouslySetInnerHTML` on unsanitised text).
* Inline marks: a text selection anchored by a thread renders under the run
  as a 2 px underline in the thread's state colour, and a small chip in the
  right margin (`◐` for question, `!` for blocking, `✎` for suggested change,
  `▶` for directive, `·` for discussion). Hovering the chip highlights the
  matching thread card in the rail; clicking scrolls the rail and opens the
  thread.
* Right panel: **Comment rail** — cards in document order, top of each card
  aligned with the vertical offset of its mark, colliding cards pushed down
  with at least 8 px between them. This is the `p-20260904-6095a8` rule,
  preserved verbatim (defect d2). The rail scrolls independently of the
  reading column; a scroll-linked hairline connects the top of the active
  card to its mark.
* Inspector drawer: the right panel switches from `Comment rail` to
  `Inspector` when a node is selected in the reading column. A segmented
  control at the top of the right panel toggles: `Comments · Inspector`.

### Canvas mode

```
┌──────────┬─────────────────────────────────────────────┬─────────────┐
│          │  ┌── canvas viewport ────────────────────┐  │             │
│ Layers   │  │                                        │  │  Inspector  │
│ panel    │  │   [contextual toolbar floats]          │  │             │
│          │  │                                        │  │             │
│          │  │   grid, frames, nodes, connectors      │  │             │
│          │  │                                        │  │             │
│          │  │                    ┌──── minimap ───┐  │  │             │
│          │  │                    │                │  │  │             │
│          │  │                    └────────────────┘  │  │             │
│          │  │  [zoom control]              [fit]     │  │             │
│          │  └────────────────────────────────────────┘  │             │
└──────────┴─────────────────────────────────────────────┴─────────────┘
```

* Left panel: **Layers** — a tree of frames (regions) and their contained
  nodes. Icons are: `▢` region, `◇` decision, `◆` directive, `▷` task, `▽`
  criterion, `∘` note, `✱` risk, `?` question, `⧉` diagram, `◱` reference,
  `⌘` evidence, `◐` goal, `∎` invariant, `⇢` reference edge, `⇒` depends_on
  edge (in the Dependencies mode outline). Kind is redundantly encoded by
  shape and label; colour is *never* the sole cue.
* Canvas viewport: a scrollable, zoomable surface. Default zoom 100 %, range
  10 % – 400 %. Grid: 8 px minor, 32 px major, rendered as 1 px lines in the
  neutral-100 token; visible from 50 % zoom up.
* Contextual toolbar: floats 12 px above the topmost selected object; if the
  object is within 60 px of the top of the viewport it flips below. Contains
  the actions for the current selection only, in a fixed order; overflow is
  a `More…` button opening a menu.
* Minimap: bottom-right corner, 200 × 140 px with 8 px inset; shows the entire
  populated area, a rectangle for the viewport, hover-drag pans, click jumps.
  Collapses to a 32 × 32 button labelled `Minimap` when off. Off by default
  under 1024 px width.
* Zoom control: bottom-left corner, `− 100 % +  Fit`, all keyboard-focusable.
* Right panel: **Inspector** by default; segmented control toggles `Inspector · Comments`.

### Dependencies mode

```
┌──────────┬─────────────────────────────────────────────┬─────────────┐
│ Outline  │  ┌── arranged graph ─────────────────────┐  │  Legend     │
│ (roles   │  │                                        │  │             │
│  &       │  │   goals ▸ tasks ▸ criteria             │  │  Cycles     │
│  filters │  │                                        │  │  drawer     │
│ )        │  │   selection highlights direct and      │  │             │
│          │  │   transitive impact                    │  │             │
│          │  │                                        │  │             │
│          │  │                    [minimap]           │  │             │
│          │  └────────────────────────────────────────┘  │             │
└──────────┴─────────────────────────────────────────────┴─────────────┘
```

* Left panel: **Filter**. Stages, node kinds (multi-select chips), edge kinds
  (`depends_on`, `blocks`, `refines`, `contains`, `validates`), and a
  role-context selector (`As engineer`, `As tester`, `As architect`). The
  filter is a projection of what the server is willing to serve; a role the
  user does not have simply shows fewer partitions, with the sealed-stage
  panel inline.
* Arranged graph: `dagre` layout on the filtered subgraph, top-to-bottom by
  default; a segmented control switches to left-to-right. Layout is computed
  server-side via `/api/layout` and applied as a patch; the client never
  invents geometry silently.
* Impact highlight: selecting a node shades every direct dependent solid, every
  transitive dependent at 50 % opacity, and highlights cycles with a red
  hairline outline plus a `Cycle: N nodes` chip in the drawer.
* Cycles drawer: right side; empty state reads `No cycles.` Populated state
  lists each cycle as `Cycle 1 · 3 nodes: task-3 → task-4 → task-3` with a
  `Reveal` button that pans and zooms to fit.

### Revision mode

```
┌──────────┬─────────────────────────────────────────────┬─────────────┐
│ Timeline │  ┌── diff header (revision · author · reason) ───┐        │
│ (list of │  │  Restore as proposal            [primary]     │        │
│ revs)    │  └───────────────────────────────────────────────┘        │
│          │  ┌── side-by-side compiled diff ──┐                       │
│          │  │  Left: base revision            │                      │
│          │  │  Right: this revision           │                      │
│          │  └─────────────────────────────────┘                      │
└──────────┴─────────────────────────────────────────────┴─────────────┘
```

* Timeline: newest first. Each row: `r0031 · 2h ago · charlie · rename task-3`.
  Grouped by day. `origin` (`user`, `proposal:pr-N`, `undo`, `migration`,
  `layout`) is a small chip on the row.
* Diff: uses the compiler's `render(ir)` on both sides. Word-level highlights
  within changed lines; changed lines drawn on a 2 px accent stripe in the
  gutter. Diagrams render on both sides; changed edges show a subtle stripe.
* `Restore as proposal`: builds a proposal whose ops would carry the chosen
  revision's state forward, opens the proposal preview overlay. The active
  revision is never rewound.
* Right panel: hidden in this mode by default; a segmented control at the top
  offers `Compiled Markdown source · Source digest · Provenance` when expanded.

## Responsive layouts

Breakpoints are content-driven, not device-driven. All four are supported.

| width | shell change |
|---|---|
| **1440** | base as drawn above; Control room grid: 4 cards/row |
| **1024** | right panel default 320 px; minimap default off; Dependencies filter panel collapses to a chevron rail; Control room grid: 3 cards/row |
| **768** | left panel collapses to a rail; comment/agents rail becomes a bottom sheet (`Comments (N)` / `Agents (N)` pill) that slides to 60 % height; inspector becomes a right sheet; canvas contextual toolbar wraps to two rows if needed; Control room grid: 2 cards/row; audit drawer becomes a bottom sheet |
| **390** (touch) | mode tabs move under the stage tabs on their own row; canvas has a full-width top toolbar with a hamburger for advanced tools; Document mode single-column reading; Inspector, Comments and Agents become full-screen sheets reached by a floating action button `⋯`; Control room defaults to List view with two-line rows; audit drawer becomes a full sheet; feedback composer becomes a full sheet reached from the sheet toolbar |

At 390 px the annotation tools (rectangle, ellipse, arrow, freehand) are
present but selectable only from an explicit `Add region` sheet; the default
canvas gesture is pan, one-finger. Two-finger pinch zooms. A long-press on
empty canvas opens the object-creation sheet. See "Touch considerations".

## Selection model and canvas interaction

The selection model is one primitive, used by every mode.

* A **Selection** is an ordered set of ids of `nodes | edges | regions |
  handles | annotations | comment marks`. Order is the order in which items
  entered the selection; keyboard arrows use it to define the "next" item.
* Selection is *the* driver of context: the contextual toolbar, the
  Inspector contents, and the Impact preview all read from it. Nothing else
  in the app changes what appears in those panels.
* Selecting an item in one mode selects it in every other mode where it can
  be selected. A `thread` node, which has no canvas presence, remains
  selected in the header breadcrumb across mode switches.

### Pointer

* Left click: replace selection.
* Shift + click: extend selection.
* Cmd/Ctrl + click: toggle in selection.
* Left drag on empty canvas: **marquee** (rectangle from anchor to cursor,
  dashed hairline, filled at 8 % accent; on release, replaces selection with
  any node or region whose bounding box intersects the marquee; hold Shift to
  extend).
* Middle drag, or Space + left drag: **pan**.
* Two-finger scroll: pan; Cmd/Ctrl + scroll: zoom around the cursor.
* Left drag on a node: **move**. Snapping is engaged by default; hold Cmd/Ctrl
  to suspend snapping.
* Left drag on a resize handle: **resize**. Aspect ratio is free; hold Shift
  to constrain to the object's current aspect.
* Left drag from a connector handle: **connect**. A live SVG edge follows the
  cursor; drop on a compatible handle to link; drop on empty canvas to open a
  small "New node from this edge" chip menu with the plausible target kinds
  for the source kind (`task → task | criterion | risk`, and so on); `Esc`
  cancels.
* Left drag from an annotation tool: draws the shape (rectangle, ellipse,
  arrow, freehand). On release, opens the anchoring thread composer.
* Double-click a title or body: enter inline text editing (Document surfaces
  and the Canvas card body).
* Right click: open the context menu. Every item here is also on the
  contextual toolbar or in the command palette; this is a convenience, not
  the only path.

### Snapping and alignment

* Object edges snap to sibling edges and centres within an 8 px tolerance;
  frames snap to the 8 px grid. Snap threshold halves under 50 % zoom so the
  guides do not become visual noise. Guides are 1 px accent lines that
  appear on drag and vanish on release; they are labelled with the pixel
  distance at their midpoint (`128 px`).
* Multi-selection drag preserves relative geometry; snapping engages on the
  outer bounding box of the selection.
* `Alt`-hovering another object during a drag draws distance measurements to
  it in every direction (Figma-style measure), rendered on the SVG overlay.

### Frames (regions), cards (nodes) and connectors (edges)

* **Frame** — a `region` node rendered as a rounded rectangle with a 32 px
  title strip. Frames clip their children only in Layers; on the canvas they
  draw children on top and use a 1 px accent-tinted stroke. A frame accepts
  children by containment: dropping a node inside registers a `contains`
  edge; dragging it out removes the edge.
* **Card** — a node rendered as a rounded rectangle sized to its content.
  The card top strip is 24 px, showing the kind glyph, id (small monospace),
  and status chip. Body content is truncated at three lines with a `⋯` toggle
  that expands the card in place. Selection draws a 2 px accent outline
  inset by 2 px; hover draws a 1 px neutral-400 outline.
* **Connector** — an edge rendered as a stepped or curved path, 1.5 px,
  accent-neutral colour. Kind is redundantly shown by an end-cap glyph at the
  target: arrow for `depends_on`, T-bar for `blocks`, hollow arrow for
  `refines`, dot for `references`, diamond for `contains`, square for
  `validates`. A tiny label chip appears mid-path on hover, showing the kind
  text.

### Keyboard interaction

Table below is the complete keyboard surface; every action here is also in the
command palette. `⌘` on macOS = `Ctrl` elsewhere.

| chord | action |
|---|---|
| `Tab` / `Shift+Tab` | move focus through the current mode's focus order (below) |
| `Enter` | activate the focused control; on a selected node, open inline edit |
| `Esc` | close overlay; cancel gesture; clear selection if no overlay is open |
| `Space` | hold to enable pan (drag) with any pointer; press again to release |
| `⌘K` | command palette |
| `⌘P` | search palette |
| `⌘0` | Control room |
| `⌘Z` / `⌘⇧Z` | undo / redo |
| `⌘S` | force flush of the autosave queue (no user-visible effect if queue is empty) |
| `⌘F` | search-in-document |
| `⌘⇧F` | search across the plan |
| `⌘/` | show keyboard shortcuts overlay |
| `⌘1`…`⌘4` | switch mode: Document, Canvas, Dependencies, Revision |
| `⌘⇧D` `⌘⇧I` `⌘⇧T` `⌘⇧E` | switch stage: design, implementation, testing, evaluation |
| `⌘⇧M` | open feedback composer at the current scope |
| `⌘⇧L` | open Delivery ledger |
| `⌘⇧G` | jump from an audit event to its affected plan objects (also `Enter` on the row) |
| `F` (Control room, card focused) | open feedback composer scoped to that agent |
| `N` (Control room, stuck card focused) | Nudge (opens composer with the nudge template) |
| `W` (Control room, card focused) | Watch (open the audit timeline drawer) |
| `V` | select tool (canvas) |
| `H` | hand tool (canvas) |
| `R` | rectangle region tool |
| `O` | ellipse region tool |
| `A` | arrow region tool |
| `P` | pencil (freehand) region tool |
| `T` | text-highlight tool (Document) |
| `F` | frame tool (canvas region) |
| `L` | new connector tool |
| `C` | new comment at selection or cursor |
| `⌘⇧A` | Ask Grogu to revise on the current thread |
| `←↑→↓` | 1 px nudge on selection; `⇧+` arrows = 8 px; `⌘+` arrows = 32 px |
| `[` / `]` | send back / bring forward |
| `⌘[` / `⌘]` | send to back / bring to front |
| `⌘G` / `⌘⇧G` | group into region / ungroup |
| `⌘D` | duplicate |
| `⌫` / `Delete` | remove selection (writes a `remove` op; not destructive across time) |
| `+` / `−` | zoom in / out at viewport centre |
| `⌘0` | zoom to 100 % |
| `⇧1` | zoom to fit (canvas) |
| `.` | toggle grid |
| `,` | toggle snapping |
| `⌘E` | export compiled Markdown for the current role and stage (opens preview) |

Every canvas action reachable by pointer has a chord. Nothing is exclusive to
pointer.

### Focus order

* **App shell**: skip links → Control room tab → stage tabs → mode tabs →
  search → agents chip → user menu → left panel header → left panel body →
  workspace body → right panel header → right panel body → status bar.
  Skip-links `Skip to workspace`, `Skip to comments`, `Skip to agents` are
  the first focusable elements.
* **Control room**: filters panel → grid/list toggle → grid (roving
  tabindex across cards in visual order) → audit drawer header → drawer body
  (row-level) → composer target → composer textarea → binding switch →
  `Send`.
* **Document**: outline tree → reading column heading → next node → …
  Inline marks are focusable in document order; when a mark is focused, the
  matching comment card is announced.
* **Canvas**: Layers root → each layer top-to-bottom → contextual toolbar (if
  visible) → canvas viewport as one grouped focus with roving tabindex across
  the selection → minimap → zoom control.
* **Dependencies**: filter panel → arranged graph (roving tabindex over
  nodes in `dagre` order) → cycles drawer.
* **Revision**: timeline → diff header → left pane → right pane.

## Layers panel and outline

The left panel switches between **Layers** (Canvas, Dependencies) and
**Outline** (Document, Revision).

* Row height 28 px, indent 16 px per level, drag-handle at the right edge.
* Icon column 20 px, then title, then a right-aligned status chip.
* Selection mirrors and drives the canvas selection.
* `Cmd/Ctrl + click` a row to add it to the selection; range with `Shift`.
* Right-click a row for kind-appropriate actions.
* An orphaned annotation shows a broken-link glyph before the title with a
  tooltip `Orphaned — anchor no longer resolves. Re-anchor or resolve.`

## Inspector

The Inspector is the properties surface for the current selection. Fields are
grouped and progressively disclosed. Group headers can be collapsed and the
state is per-kind.

Sections (order fixed):

1. **Identity** — id (monospace, click to copy), kind (read-only), stage.
2. **Title and body** — inline single-line and multi-line editors.
3. **Attributes** — key/value pairs typed by kind (`binding`, `audience`,
   `status` for a directive, and so on). Unknown keys are surfaced but tagged
   `custom`.
4. **Relationships** — a compact list of edges to and from this node.
   `Add relationship` opens a picker.
5. **Geometry** — only when the node has geometry: X, Y, width, height, Z,
   as numeric inputs.
6. **Provenance** — created and updated revision (link to the Revision mode),
   source (`user`, `proposal`, `migration`, `steering`, `amendment`).
7. **Comments** — count of open, resolved, orphaned threads on this node.

All fields commit on blur or `Enter`. Errors show inline under the field with
literal copy; see "Copy".

## Comment rail

* Card layout: 12 px padding, 8 px between cards baseline; 8 px minimum gap
  after collision, top of card at the mark's vertical offset (defect d2).
* Card header: author (system tag for `Grogu`), kind chip, timestamp
  (`2h ago`), status.
* Card body: quoted anchor context, then the composer or the reply thread.
* Card footer actions: `Reply`, `Resolve`, `Reopen`, `Ask Grogu to revise`,
  `Promote to directive` (architect), overflow menu.
* Round chip in the rail header: `Round N · changes requested · 6 open`
  (defect d7), verbatim; empty state `Round N · no changes requested`.
* Filter chips at the top of the rail: `Open`, `Resolved`, `Orphaned`, `Mine`,
  and kind chips (`Discussion`, `Question`, `Suggested change`, `Blocking`,
  `Directive`). Filters combine with AND across categories, OR within.
* An **anchored** thread shows the anchor context in its card header. If the
  anchor resolves via the fuzzy step of `grogu_review.reanchor_text`, the
  card shows a `Moved` chip and a `View at r<NNNN>` link to the revision
  where the anchor last matched exactly. If it fails, the card shows the
  `Orphaned` chip and the composer surface is disabled until the user
  re-anchors or resolves. **A thread is never silently moved.**
* Partial resolution: a composite selector may resolve to a subset of its
  original members. The card shows `Anchor changed: 2 of 3 objects resolved`
  with a `Show which` link that highlights the resolved and orphaned members
  on the canvas.
* Kind is signalled by chip *and* by a left border glyph column (2 px wide):
  discussion neutral, question `?`, suggested-change `✎`, blocking `!` in
  warn, directive `▶` in accent. Never colour alone.

## Minimap, breadcrumb and zoom control

* **Breadcrumb** in the top bar: `plan · <plan title> · <stage> · <mode> ·
  <selection or '—'>`. Each segment is a menu:
  * `plan` — recent plans list.
  * `<plan title>` — plan actions (open in Finder, copy path, verify,
    compile).
  * `<stage>` — stage tabs.
  * `<mode>` — mode tabs.
  * `<selection>` — id and title, click to zoom-to-fit in canvas or scroll
    into view in document.
* **Zoom control**: `−  100 %  +`  and `Fit`. `Fit` frames the current
  selection, or the whole populated area if selection is empty. Zoom
  percentage is an editable numeric input.
* **Minimap**: renders every node as its bounding box in a neutral fill;
  viewport as a stroked rectangle in the accent colour; drag the rectangle to
  pan; click to jump.

## Presence-ready affordances

Collaboration is not implemented; the design leaves space for it.

* A **Presence slot** in the top bar, 96 px wide, holds up to four 24 px
  pills; overflow becomes a `+N` pill. In this release only the local session
  pill appears, drawn as an outlined circle with the user's single initial;
  it does not colour a cursor or a selection.
* Each canvas node reserves a 4 px right-side gutter for future presence
  indicators. In this release the gutter is transparent.
* The comment rail reserves a `In progress · <name>` chip in the card
  footer; unused in this release.
* No follow-cursor, live-selection or shared-viewport UI is drawn. Presence
  data model is out of scope.

## Annotations: rectangle, ellipse, arrow, freehand and text highlight

Annotations are stored as `region` nodes with an `attrs.shape` of
`rectangle | ellipse | arrow | freehand`. Freehand stores a poly-line as a
list of integer coordinates. The `anchors` edge points at intersected objects
captured at creation time; those object ids are frozen. The annotation
*displays* on the canvas at its stored geometry.

Text highlight (Document): a `thread` with a `text` selector. Rendered as a
2 px underline plus the margin chip. Selection is character-precise; drag to
extend, `Shift+Arrow` to extend from keyboard.

### Behaviour when objects move

* The annotation's geometry is anchored to the canvas surface, **not** to the
  objects. If an intersected object moves outside the annotation, the
  annotation stays where it was drawn; the anchored thread's status becomes
  **Moved**, its card shows `1 of 3 anchored objects moved out of the
  region` with a `Show at draw time` link that pans to the original bounds
  and draws the objects' ghost outlines at their positions at the anchor
  revision.
* If the last intersected object is removed, the annotation is **Orphaned**;
  the geometry is preserved on the canvas as a dashed outline in neutral-300.
  Composer disabled; `Resolve`, `Re-anchor`, `Delete annotation` available.
* If a text anchor's body changed within the fuzzy tolerance, `Moved` chip
  and `View at r<NNNN>` link; if outside tolerance, `Orphaned`.
* Annotations never silently retarget. Re-anchoring is always a user action
  and shows the exact diff of what will change.

## Directive promotion — the modal

Two-step form.

* Step 1 — **Text**: single-line title (required, 120-char soft limit),
  multi-line body (Markdown, subset), audience checkbox group
  (`Architect`, `Designer`, `Engineer`, `Tester`, `Reviewer`, `Everyone`),
  binding radio (`Must` default, `Should`). Cancel returns to the thread.
* Step 2 — **Preview**: shows the compiled `## Directives` section for the
  chosen audience with the new item, and lists the source thread with a
  `derives_from` link. Primary `Promote`, secondary `Back`.

Promotion writes: `add directive`, `add edge derives_from thread→directive`,
`resolve thread with note ↗ dir-N`. Undo reverses all three; provenance is
preserved.

## Ask Grogu to revise — the composer

* Modal, 720 px wide.
* Top: pinned context — the thread's selector, the current anchor context,
  and the current node's title and stage.
* Middle: an autosized textarea for the instruction, placeholder
  `What would you like Grogu to change?`.
* Bottom: `Base: r<NNNN>` (read-only chip), `Cancel`, `Propose`.
* On submit: the modal turns into a status view: `Grogu is proposing…` with
  a determinate progress bar if the server streams progress, otherwise a
  linear indeterminate bar bounded by a 45 s timeout. On success the modal
  transitions into the Proposal preview overlay.

## Proposal preview and impact preview

The **Proposal preview overlay** is the accept/reject surface for one
proposal.

* Left column (40 %): **Node diff** — grouped by node id, showing
  before/after title, body, attrs. Removed lines struck through, added lines
  underlined; both colours neutral so no colour cue is the only cue.
* Centre column (40 %): **Compiled Markdown diff** for the projections that
  would change, per role. Roles are tabs at the column top.
* Right column (20 %): **Impact**.

Impact is structured explicitly into **Primary** and **Consequential**:

* **Primary changes** — the nodes and edges the proposal directly writes.
  Each listed with kind, id, title and the operation (`add`, `set`,
  `remove`, `link`, `unlink`).
* **Consequential changes** — the transitive impact set: nodes whose
  compiled projection digest changes, edges introduced or removed by
  cascading rules, and any cycles introduced. Each item states the reason
  (`downstream of task-3`, `covered by criterion-2`, `cycle with task-1`).
* If a change is destructive (removes a node with `contains` children,
  breaks a `depends_on` for a downstream `task`), the item is drawn with a
  left border in the warn token and its row expands by default. The
  overlay refuses `Accept` while an unresolved warn row is collapsed
  unread; the header explains: `Review each highlighted consequence
  before accepting.`

`Accept proposal` (primary) commits. `Reject with reason` opens a required
`Reason` field; the button label changes to `Reject` once a reason is typed.
`Cancel` closes the overlay without deciding. A `Save as draft` link keeps
the proposal in `pending` state as-is (no state change on the server).

The **Impact preview popover** is the same content in miniature, shown
inline for a pending edit that has not yet become a proposal. It hangs
from the contextual toolbar with `View impact ▸`, sized 480 × 320, with the
same Primary/Consequential split.

## Compiled Markdown preview

* Full-width overlay (`⌘E`).
* Header: `Compiled projection · Role: <role> · Stage: <stage> · Include:
  <normative|all> · Budget: <N chars or —> · Digest: <sha256:short>`.
* Body: two panes — Markdown source (monospace) and rendered HTML — with
  a synced scroll toggle.
* Footer: `Copy Markdown`, `Save to file…` (uses the local file picker via
  the browser), `Close`.
* Elided items panel at the bottom, always visible when non-empty:
  `Elided: 3 note bodies · 1 reference` with a disclosure that lists each
  elided id.
* If a semantic-equivalence error is present for this projection, the top of
  the overlay shows the amber banner described above, with `Copy
  diagnostic`.

## Search palette and command palette

* **⌘P — Search palette**: a text input with three tabs: `Nodes`, `Threads`,
  `Revisions`. Results are 40 px rows: icon, title, and a small breadcrumb
  (`design · Directives`). Enter navigates to the item in the current mode,
  or asks which mode to open if the item is not renderable there.
* **⌘K — Command palette**: fuzzy match over every action in the command
  registry. Rows show the action name and its chord, right-aligned. Actions
  whose preconditions are not met are dimmed and give a reason on hover.

## States — literal copy

Where a state has no visual, the copy is still fixed here. Every literal
string on this page is what the engineer ships.

### Empty states

* **No plan opened yet**
  * Title: `No plan open.`
  * Body: `Run` `grogu plan doc open <id>` `in your terminal, or head to
    Control room to watch running agents.`
* **No threads yet**
  * Title: `No comments yet.`
  * Body: `Select text, an object, or a region and press C to start a
    conversation.`
* **No revisions yet** (fresh plan)
  * Title: `Nothing to compare.`
  * Body: `Make an edit and a revision will appear here.`
* **No cycles**
  * `No cycles.`
* **No results**
  * `Nothing matches "<query>".`

### Control room states — literal copy

Every card carries one badge from the following set. The badge is redundantly
encoded by glyph, label, and a 2 px left border in the named token.

* **Live** — glyph `●`, border `success`, label `Live · <t> ago`.
  Freshness < 60 s **and** an event of any kind in the last 60 s.
* **Idle** — glyph `◐`, border `neutral-500`, label `Idle · <t> ago`.
  Connected but no observable action for 1 – 5 minutes.
* **Stuck** — glyph `!`, border `warn`, label `Stuck at <last action> ·
  <t> without new event`. Set when the agent claims a task or a role but has
  produced no observable event for `stuck_threshold` (defaults to 5 minutes;
  the value is surfaced under the badge as `threshold 5m`).
* **Finished** — glyph `✓`, border `success`, label `Finished · r<NNNN> ·
  <workstream> merged` (or `Finished · gate <name> passed`).
* **Disconnected** — glyph `⌀`, border `neutral-300`, label
  `Disconnected · last seen <t> ago`. The connect/disconnect events came
  from the harness, not inferred by the UI.
* **Error** — glyph `×`, border `danger`, label `Error · <machine
  token>`. Row 3 replaced by the last observable error message summary; a
  `Show last event` link opens the audit drawer.
* **Waiting on you** — glyph `⚑`, border `accent`, label `Waiting · <N>
  unread steering`. Set when the target has one or more unread pieces of
  feedback the harness has confirmed as delivered.

Freshness label rules, applied on top of the state badge:

* `Live · <60 s`, e.g. `Live · 12s ago`.
* `Idle · 1–5 m`, e.g. `Idle · 3m ago`.
* `Stale · 5–15 m`, e.g. `Stale · 8m ago`.
* `Dead · >15 m`, e.g. `Dead · 22m ago`.

Freshness updates every 5 s. When the tab is hidden, updates pause and the
first foregrounded update carries the true elapsed time.

### Control room empty states

* **No agents at all**
  * Title: `No agents are running.`
  * Body: `Start one from your terminal, for example` `grogu engineer --plan
    p-20260905-aab017` `or open a plan and use ‘Ask Grogu to revise’.
    Anything you send from here is durable and appears in the Delivery
    ledger.`
* **Filtered to zero**
  * `Nothing matches this filter.`
  * `Clear filters` primary link.
* **Audit timeline empty (no events yet)**
  * `No events yet.`
  * Body: `Timeline shows observable events. Grogu never records prompts,
    chain-of-thought, or raw tool arguments.`
* **Audit timeline quiet**
  * `Quiet since <t>. No new events.` (Appears at the top of the timeline
    when the last event is older than 5 minutes; not modal.)
* **Delivery ledger empty**
  * `No feedback sent yet.`
  * `Feedback you send to agents and roles appears here as a durable
    record.`

### Loading

* Skeleton for the shell as described (`aria-busy`).
* Panel-level `Loading…` with an indeterminate 2 px bar only when the wait
  exceeds 400 ms. Never a full-screen spinner.
* Text under a slow operation (>2 s): `Still working…`. Under 5 s:
  `This is taking longer than usual. Check` `grogu doctor` `.`
* Migration progress uses determinate percentages fed from the server.

### Errors

* **Offline / network gone**
  * Status bar: `Offline · edits queued (N)`
  * Toast: `Network unavailable. Your edits are saved locally and will
    upload when the server is reachable.`
* **Save failed (5xx)**
  * Toast: `Could not save. Your edits are kept.` `[Retry]`
  * Status bar: `Save failed · retry queued`
* **Conflict (409, rebased silently)**
  * Toast: `Rebased on r<NNNN>`. `[View changes]`
* **Conflict (409, could not rebase)**
  * Rail card: `Someone else changed <title>. Keep your edit or open theirs.`
    `[Keep mine]  [Open theirs]  [View both]`
* **Semantic-equivalence failure**
  * Banner: `The compiled Markdown does not round-trip. Your change was not
    saved. Copy diagnostic for a bug report.` `[Copy diagnostic]`
* **Permission denied (403)**
  * Panel: `This stage is out of your role's reach.` Body: `You are opening
    the plan as <role>. This stage is only readable by <required role>.`
* **Sealed stage** (parameterised, defect d6)
  * Panel: `The <stage> stage is sealed.` Body: `Only <required role> can
    unseal it. Ask for a review round to re-open.`
* **Unwritten stage** (defect d6)
  * Panel: `The <stage> stage has not been written yet.` Body: `<owner role>
    is responsible for writing it. When they publish, it will appear here.`
* **Invalid package**
  * Panel: `This plan is not in a state we recognise.` Body: `Both an old
    directory and a new package exist at <paths>. Delete the one you do not
    want, then reopen.`
* **Oversized import**
  * Panel: `That import is too big to load safely.` Body: `Limit is 256 KB
    per request; the file was <n> KB. Split it or use` `grogu plan doc
    patch --file -` `.`
* **Missing asset**
  * Inline: `Attachment missing (<name>). It was tracked in attachments/ but
    is not on disk. Restore or remove the reference.`
* **Ambiguous anchor**
  * Inline card: `This comment could match more than one place.` Body:
    `Choose where it belongs.` `[Show 3 candidates]`
* **Unresolved blocker**
  * Rail card ribbon: `Blocking · unresolved`. Overlay when trying to
    `Approve`: `A blocking thread is open. Resolve it before approving.`
* **Session token spent**
  * Panel: `This session link has already been used.` Body: `Run` `grogu
    plan doc open` `again to get a fresh link. The previous session is safe.`
* **Migration produced a non-equivalent artifact**
  * Overlay (red): `Migration would change the meaning of this plan.` Body:
    `The legacy directory is untouched. Diff below shows what would differ.`
    `[Show diff]  [Cancel]`
* **Crash recovery (unflushed queue)**
  * Overlay: `Restore unsaved edits?` Body: `<N> edit(s) were queued locally
    when the last session ended. They still apply to r<NNNN>.` `[Show what
    changed]  [Apply]  [Discard]`
* **Proposal stale**
  * Banner in Proposal preview: `Stale — the plan moved to r<NNNN> since
    this proposal was made.` `[Rebase]  [Abandon]`
* **Feedback undeliverable**
  * Delivery chip: `f-023 · undeliverable · <reason>`. Reasons are
    verbatim from the harness, e.g. `agent has ended`, `plan no longer
    exists`, `role no longer bound`. Body: `Nothing was delivered. Cancel or
    replace this feedback.` The gate does **not** close on undeliverable
    binding feedback; instead the ledger shows the plan status as
    `Feedback f-023 could not be delivered. Gate is open again.`
* **Binding feedback awaiting acknowledgement (gate view)**
  * On the affected gate row in Revision mode: `Blocked by f-023
    (binding) · sent 12m ago · not yet acknowledged.` Actions: `Open in
    Control room`, `Withdraw`.
* **Withdrawn feedback**
  * Delivery chip: `f-023 · withdrawn`. Ledger row shows a strikethrough
    on the message; audit timeline gets a `feedback withdrawn` event.

### Success

* Autosave: toast `Autosaved`, dismisses after 2 s.
* Proposal accepted: toast `Applied as r<NNNN>. [Open revision]`.
* Directive promoted: toast `Promoted to dir-N. [Open directive]`.
* Feedback sent: composer collapses; toast `Feedback sent to <target>. [View
  ledger]`.
* Feedback delivered: delivery chip advances to `delivered`; toast (once per
  message) `Delivered to <target>.`.
* Feedback acknowledged: delivery chip advances to `acknowledged`; toast
  `Acknowledged by <target>. Gate reopened.` (last clause only for binding
  feedback that had closed a gate).

## Copy — voice

Quiet, exact, humane. Never `Oops!`. Never `Something went wrong.` A
rewritten example:

* Draft: `Error: Save failed. Please try again later.`
* Ship: `Could not save. Your edits are kept.` `[Retry]`

Buttons say what will happen: `Promote`, `Propose`, `Restore as proposal`,
`Migrate`, not `OK` or `Confirm`. Confirmation is destructive-only and asks
for a typed noun (`type the plan id`).

Copy for placeholders is instructional, not decorative:
`What would you like Grogu to change?` beats `Type here…`.

## Tokens

Everything is expressed on a small scale. The scale is stated here once and
referenced by name below.

### Spacing scale (4 px base, 8 px rhythm)

`space-0: 0`, `space-1: 4px`, `space-2: 8px`, `space-3: 12px`, `space-4: 16px`,
`space-5: 24px`, `space-6: 32px`, `space-7: 48px`, `space-8: 64px`.

Use `space-2` between siblings, `space-4` between groups, `space-5` around
containers, `space-6` between major regions. Never invent an in-between value.

### Type ramp (system font stack; no webfont)

Base family: `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica
Neue", Arial, "Liberation Sans", sans-serif`. Monospace: `ui-monospace,
SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace`.

| token | size | line | weight | use |
|---|---|---|---|---|
| `text-xs` | 11 px | 16 px | 500 | chips, ids, status |
| `text-sm` | 13 px | 20 px | 400 | table cells, secondary text |
| `text-md` | 14 px | 22 px | 400 | body |
| `text-lg` | 16 px | 24 px | 500 | card titles, section labels |
| `text-xl` | 20 px | 28 px | 600 | mode headings |
| `text-2xl` | 24 px | 32 px | 700 | overlay titles |
| `text-mono-sm` | 12 px | 18 px | 400 | ids, hashes |

No other sizes exist. Emphasis is weight change (500 → 600), not colour.

### Colour tokens

The palette is neutral plus one accent. Colour carries meaning where the token
name says so; everywhere else, colour is neutral by name.

Light theme values:

| token | hex | contrast on `bg` | meaning |
|---|---|---|---|
| `bg` | `#FFFFFF` | — | app background |
| `bg-elev-1` | `#F7F7F8` | — | panel background |
| `bg-elev-2` | `#EFEFF1` | — | popover, contextual toolbar |
| `bg-elev-3` | `#E7E7EA` | — | overlays |
| `neutral-100` | `#EEEEF1` | AA-large | grid lines, minor separators |
| `neutral-300` | `#C9C9CE` | AA | borders, disabled |
| `neutral-500` | `#8A8A93` | AA | secondary text |
| `neutral-700` | `#4B4B54` | AAA | body text |
| `neutral-900` | `#101014` | AAA | headings |
| `accent` | `#3255FF` | AAA (>7:1 on `bg`) | interaction, focus, selection |
| `accent-soft` | `#E4EAFF` | — | selection fill, hover chips |
| `warn` | `#B4560A` | AAA (7.3:1) | warnings, consequential changes |
| `warn-soft` | `#FBEAD0` | — | banner backgrounds |
| `danger` | `#B0202E` | AAA (7.4:1) | destructive confirmation, cycle stroke |
| `success` | `#0E7A46` | AAA (7.2:1) | resolved, applied |

Dark theme values (swap; ratios verified against `bg`):

| token | hex |
|---|---|
| `bg` | `#0B0B0F` |
| `bg-elev-1` | `#141419` |
| `bg-elev-2` | `#1B1B22` |
| `bg-elev-3` | `#22222A` |
| `neutral-100` | `#22222A` |
| `neutral-300` | `#39394A` |
| `neutral-500` | `#7C7C86` |
| `neutral-700` | `#B4B4BE` |
| `neutral-900` | `#F0F0F5` |
| `accent` | `#7C97FF` |
| `accent-soft` | `#1E2440` |
| `warn` | `#F3B270` |
| `warn-soft` | `#3B2A12` |
| `danger` | `#F17A85` |
| `success` | `#5FCB98` |

Meaning of `accent`: **interaction** — a link, a focus ring, the selected
outline, the primary button, the active tab. Meaning of `warn`: **consequential
change or attention required**. Meaning of `danger`: **destructive
confirmation**; never used as a mere alert. Never used to indicate a *kind* of
node — kinds are shown by glyph and label, not colour.

Contrast targets: body text ≥ 7:1 (AAA), non-text UI ≥ 3:1. All named
combinations above meet the target. High-contrast mode maps `neutral-500` to
`neutral-700`, doubles focus-ring thickness, and replaces elevation shadows
with 1 px `neutral-300` borders.

### Radius

`radius-1: 2px`, `radius-2: 4px`, `radius-3: 6px`, `radius-4: 8px`,
`radius-5: 12px`. Cards use `radius-4`; overlays `radius-5`; chips
`radius-2`; buttons `radius-3`.

### Elevation

Shadows are single-layer, cheap. `elev-1: 0 1px 2px rgba(0,0,0,.06)`, popover.
`elev-2: 0 6px 24px rgba(0,0,0,.10)`, overlay. Dark theme scales alpha to
`.24` and `.40`.

### Motion

Purposeful only. `motion-fast: 120 ms`, `motion-base: 180 ms`,
`motion-slow: 260 ms`, easing `cubic-bezier(0.2, 0.7, 0.2, 1)`. Uses:

* Panel collapse: `motion-base`.
* Mode switch cross-fade: `motion-fast`.
* Contextual toolbar reveal: `motion-fast`.
* Zoom-to-fit: `motion-slow`, with a 60 fps translate.
* Toast: `motion-fast` in, `motion-base` out.

`prefers-reduced-motion: reduce` → every duration drops to 1 ms; zoom-to-fit
snaps; panel collapse is instantaneous; the contextual toolbar appears without
translate. Nothing that only decorated is drawn.

### Focus ring

2 px `accent` outline offset 2 px from the target. `outline-style: solid`.
Always visible on focus-visible; never suppressed except when a container
provides an equivalent 2 px inset outline (a card selection ring), and even
then a 1 px accent inner ring is drawn to reinforce.

## Accessibility

* Contrast ratios: see the colour table above. Every meaningful pair is AA
  or AAA on both themes.
* Full keyboard path: see the table under "Keyboard interaction". There is no
  action reachable only by pointer.
* Roles: canvas viewport is `role="application"` with
  `aria-roledescription="Canvas"` and a keyboard-navigable roving tabindex
  over items in Layers order. Selection is announced (`3 items selected:
  task-3, task-4, region-1`) via a polite live region.
* Node cards have `role="group"`, `aria-labelledby` to the title, and
  `aria-describedby` to a hidden summary (kind, id, stage). Edges have
  `role="link"` and an accessible label built as `<kind> from <source title>
  to <target title>`.
* Contextual toolbar is a `role="toolbar"`. The floating position is announced
  as "selection toolbar" when focused via `⌘.`.
* Screen-reader alternatives to the canvas:
  * `View as list` (Layers panel) is *the* accessible surface. Everything the
    canvas does is possible from Layers plus the Inspector.
  * `Describe selection` command reads a structured summary aloud (title,
    kind, stage, relationships, direct impact) via the live region.
* Dynamic type: honours the browser's root font size; the type ramp scales
  from `text-md: 14 px` at 16 px root to `text-md: 18 px` at 20 px root. Line
  heights scale with size. Chrome heights become `min-height` at that point
  so labels never truncate.
* Reduced motion: as above.
* Focus order and skip links: see "Focus order". `Skip to workspace` and
  `Skip to comments` links are provided.
* Colour is never the only carrier of meaning. Every state that has a colour
  also has a glyph and a text label.
* All non-textual controls have an `aria-label`. Every icon-only button
  additionally exposes its label in a hover tooltip and a `title`.
* Live region announcements: autosave (`Saved r0032`), offline
  (`Offline. Edits kept locally.`), rebase (`Rebased on r0033`), conflict
  (`Someone else changed <title>`), semantic-equivalence failure (`Compiler
  bug: not saved`), acceptance (`Proposal pr-4 applied as r0034`).

## Touch considerations

* Minimum hit target 40 × 40 CSS px. Contextual toolbar buttons are 40 px
  tall on touch, 32 px on pointer.
* Pointer events unified via the Pointer Events API. Long-press (`≥ 500 ms`)
  = right-click semantically; opens the context menu.
* One finger pans the canvas; two-finger pinch zooms; three-finger swipe
  switches mode (customisable in `⌘,` — out of scope for this release).
* Marquee is not available with a single finger on touch; a `Select area`
  button on the canvas toolbar starts a two-anchor selection: tap corner,
  tap opposite corner.
* Drag-connect requires a source handle: source handles enlarge to 24 px
  hit target on touch.

## Performance feedback

* Every action shows response feedback within 100 ms: the selection outline,
  the marquee, the contextual toolbar move, or the `Saving…` chip.
* Saves are debounced 150 ms after the last op of a gesture; a single
  gesture writes exactly one revision (invariant of the plan).
* Under load, the toolbar shows `Saving… (N queued)`; when N ≥ 3 the app
  reveals `Slow save · <last error or 'server responding slowly'>` and
  offers `Show queue`.
* Canvas rendering budget: 60 fps at 500 nodes on a mid-tier laptop; below
  60 fps, offscreen nodes are drawn as bounding boxes only (no titles) at
  zooms under 33 %. Users are notified once, non-modally: `Simplified
  rendering at low zoom.`
* SSE reconnect uses exponential backoff visible as `Reconnecting in Ns`
  in the status bar; the app does not spam retries.

## Layout — one representative complex plan

The design must work on a plan that pushes every affordance. Below is the
fixture used to check every mode. All four modes must render this plan.

```
Plan p-fixture-heavy — "Migrate storage backend"
Stages: design, implementation, testing (sealed), evaluation (sealed)

Design stage (32 nodes):
  1 goal (goal-1)
  6 directives (dir-1..dir-6), audiences mixed
  3 constraints, 2 invariants, 3 decisions
  4 criteria bound to tasks by `validates`
  8 tasks, forming a small DAG with one intentional refactor cycle
    (task-3 depends_on task-4, task-4 depends_on task-3) — cycle report exercised
  2 diagrams (Mermaid), each with 6 parsed nodes
  1 region grouping 6 tasks, resized so it clips 2 tasks that must
    show the "moved" annotation state
  3 threads across nodes:
    - text-anchored discussion on dir-2 body
    - object-anchored blocking question on task-3.title, orphaned
      by a title rewrite (exercised: reanchor prompt)
    - composite region annotation over 4 tasks; 1 task removed after
      creation (exercised: partial resolution)
  1 proposal derived from the composite thread, `pending`

Control room agents (7, one per state badge and one plain):
  a-1 engineer · core-package-compiler · Live · action "applying patch"
  a-2 tester · plan-workspace · Idle · action "waiting on integration"
  a-3 engineer · plan-workspace · Stuck · action "compiling markdown"
      · 8m without event · threshold 5m
  a-4 architect · — · Finished · gate implement passed at r0031
  a-5 reviewer · integration · Disconnected · last seen 22m ago
  a-6 designer · — · Error · "budget exceeded"
  a-7 engineer · integration · Waiting on you · 1 binding feedback unread

Feedback fixture (5):
  f-01 sent to a-1 (advisory) — delivered, unacknowledged
  f-02 sent to `engineer on p-fixture-heavy` (binding) — acknowledged,
       gate reopened
  f-03 sent to a-3 (binding) — pending, currently closing the implement gate
  f-04 sent to `all reviewer` (advisory) — undeliverable, no reviewer bound
  f-05 sent to a-7 (binding) — withdrawn
```

Every mode must render without crash and reach all acceptance criteria on
this fixture. The tester and the workstream both use it.

## Screenshot / evidence checklist

The engineer captures the following against the running app in a fresh,
automation-owned Playwright context. Each shot at both light and dark themes
and at the four widths (1440, 1024, 768, 390). Reduced-motion and high-contrast
runs are captured at 1440 only.

* Boot skeleton (`aria-busy`), then Ready.
* Document mode: reading column with anchored marks, rail with three
  threads including one Moved and one Orphaned.
* Document mode: inline edit in progress; conflict card after a simulated
  409.
* Canvas mode: default view of the fixture; contextual toolbar visible on a
  multi-select of two tasks and one region.
* Canvas mode: drag-connect in flight with the "new node from this edge"
  chip menu open.
* Canvas mode: rectangle, ellipse, arrow and freehand annotations, one
  each, with their anchoring threads open in the rail.
* Canvas mode: annotation with a member object removed (`2 of 3` state) and
  with an object moved outside the region (`Moved` state).
* Dependencies mode: full graph; cycle highlighted; cycles drawer expanded.
* Dependencies mode: filtered to `As engineer`; sealed-stage panel visible
  where testing would be.
* Revision mode: timeline with proposal-origin, undo-origin and
  migration-origin rows; diff for one revision.
* Comment thread modal: composer for a suggested-change; `Ask Grogu to
  revise` composer; proposal preview overlay with Primary and Consequential
  changes; accept and reject paths.
* Directive promotion: step 1, step 2, and the resulting `↗ dir-N` chip on
  the source thread.
* Compiled Markdown preview: source + rendered, elision panel visible.
* Migration overlay: progress; success; non-equivalence failure with diff.
* Offline banner; retry toast; conflict rebased toast; conflict
  unresolvable card.
* Semantic-equivalence banner with `Copy diagnostic`.
* Command palette; search palette with cross-mode navigation.
* Keyboard-only run: focus rings visible on every reachable control across
  every mode.
* Reduced-motion run: no easing, no fade; zoom-to-fit snaps.
* High-contrast run: 4 px focus rings; borders on all elevated surfaces.
* Control room: default grid at 1440 populated by the fixture (below);
  filters populated; agents chip in status bar.
* Control room: cards for each of the seven state badges — `Live`, `Idle`,
  `Stuck`, `Finished`, `Disconnected`, `Error`, `Waiting on you`.
* Audit timeline drawer: mixed event types with jump-to-plan link;
  provenance footer visible; a `Quiet since <t>` marker at the top of a
  quiet timeline.
* Feedback composer: `Advisory` and `Binding · closes the next gate`
  states; delivery chip progressing through `sent → routed → delivered →
  acknowledged`.
* Plan status bar: `Feedback · 1 binding pending`; Revision mode gate row:
  `Blocked by f-023 (binding)`; `Withdraw` and reopen path.
* Delivery ledger: populated with feedback of every scope; withdrawn and
  undeliverable rows visible.
* Plan-mode integration: right-panel `Agents` tab; Inspector `Agent
  activity` section on a selected node with recent events.

## Acceptance criteria

One statement per line, checkable without asking anyone what was meant.

* The workspace opens with the layout dimensions and panel defaults defined
  under "The App shell at 1440 × 900" for widths ≥ 1440 px, and the stated
  responsive layouts at 1024, 768 and 390 px widths.
* Every action listed in the keyboard table performs exactly the described
  action in the mode where it applies, and does nothing in a mode where it
  does not apply.
* Nothing on the canvas or in the rail can be reached only by pointer:
  every canvas action and every rail action has an equivalent action in the
  Layers panel plus Inspector plus keyboard chord.
* The contextual toolbar reflects the current selection and only the current
  selection; changing the selection updates the toolbar within one animation
  frame.
* Selecting on the canvas selects on the Document and Dependencies modes
  and vice versa where the item is renderable; the header breadcrumb shows
  the selection even in modes where the item cannot be rendered.
* A drag gesture, an inline edit, or an accepted proposal writes exactly one
  revision; a cancelled gesture writes no revision; an in-flight gesture
  never sends a patch.
* Undo writes a new revision with `origin: "undo"` and does not rewind
  history.
* Autosave, offline, save-failed, conflict-rebased, and conflict-unresolvable
  states show the literal copy given under "Errors", in the surface
  specified.
* Semantic-equivalence failure shows the amber banner with the exact literal
  copy and a `Copy diagnostic` button that puts `field`, `revision`, `base`
  and the plan id on the clipboard as JSON.
* The compiled Markdown preview is reachable at `⌘E`, shows role, stage,
  include, budget and source digest in its header, and lists elided items by
  id when non-empty.
* The Proposal preview overlay distinguishes Primary and Consequential
  changes with the labels above, requires review of any consequential row
  drawn in the warn colour, and its `Reject` action requires a typed reason.
* Annotations, whether text, rectangle, ellipse, arrow or freehand, never
  silently retarget. Moved and Orphaned states show the specified chips and
  copy; re-anchoring is a user action with a diff preview.
* Comment threads never silently retarget. A fuzzy re-anchor shows the
  `Moved` chip and a `View at r<NNNN>` link; a failed re-anchor becomes
  `Orphaned`.
* The four modes render the representative fixture without crash, hit
  60 fps at ≥ 500 nodes on a mid-tier laptop, and drop titles below 33 % zoom
  under load with a one-time non-modal notice using the specified copy.
* All named colour combinations meet contrast target 7:1 (body) and 3:1
  (non-text) in both light and dark themes; automated axe scans pass on
  every mode at every width.
* Focus order matches the "Focus order" section on every mode; skip links
  are the first focusable element.
* `prefers-reduced-motion: reduce` reduces every duration to 1 ms and
  suppresses translate on incidental animations.
* No colour is the only carrier of any meaning; every stateful glyph has a
  text label and a shape.
* Migration overlay follows the four steps and shows the non-equivalent
  outcome copy verbatim when a migration would change meaning; the legacy
  directory is not touched in that case.
* Directive promotion produces a `directive` node with `attrs.origin =
  "thread:<tid>"` and a `derives_from` edge to the source thread, and shows
  the `↗ dir-N` chip on the resolved source thread.
* The Presence slot renders the local session pill only, reserves 96 px in
  the top bar, and no follow-cursor or shared-selection UI is drawn.
* Sealed and unwritten stage panels use the parameterised copy under
  "Errors" and name the correct role and stage.
* A composite annotation whose members partially resolve shows the
  `Anchor changed: N of M objects resolved` copy and highlights which
  resolved and which orphaned on `Show which`.
* A proposal whose `base != HEAD` shows the `Stale — the plan moved to
  r<NNNN> since this proposal was made.` banner and offers `Rebase` and
  `Abandon`; there is no auto-merge path.

### Control room

* The Control room mode is reachable at `⌘0` and from the leftmost tab of
  the mode bar, and is the default surface when the app opens without a
  plan.
* The Control room lives inside the same App shell as the plan modes: same
  top bar, same left and right panels, same status bar; nothing about the
  chrome makes it feel like a separate app.
* Every agent card shows role, workstream, plan, last-touched revision,
  state, freshness, current action, last tool call (name + duration +
  outcome), failure count, blocker count, unread steering count, and the
  delivery badge for feedback the current user has sent to that agent.
* Freshness labels use the four buckets `Live · <60 s`, `Idle · 1–5 m`,
  `Stale · 5–15 m`, `Dead · >15 m`, refresh every 5 s while the tab is
  foregrounded, and show the true elapsed time on the first update after a
  hidden→visible transition.
* The state badges `Live`, `Idle`, `Stuck`, `Finished`, `Disconnected`,
  `Error`, `Waiting on you` each use the literal copy under "Control room
  states — literal copy" and are redundantly encoded by glyph, label and a
  2 px border colour token; colour is never the only cue.
* The `Stuck` badge exposes its threshold verbatim (`threshold 5m`) so the
  user understands why the card is flagged.
* The audit timeline shows only observable events from the enumerated set
  (`revision`, `patch`, `tool call`, `gate passed`, `gate blocked`,
  `steering delivered`, `feedback acknowledged`, `error`, `disconnected`,
  `connected`, `role bound`, `workstream started`, `workstream merged`)
  and its provenance footer reads verbatim `Timeline shows observable
  events. Grogu never records prompts, chain-of-thought, or raw tool
  arguments. What you see here is the same view the tester and reviewer
  see.`
* Tool-call rows show only name, duration and outcome, plus the fixed note
  `Arguments not shown. Grogu never records prompts or reasoning.` No raw
  argument body is ever rendered by the client for any tool event.
* Clicking any timeline event that names plan objects (revision id, node
  id, edge id) navigates the workspace to that plan and selects the
  affected objects; pressing `Esc` returns to the Control room drawer with
  the previous focus.
* The feedback composer accepts exactly one target scope out of `This
  agent`, `All <role>`, `This plan`, `<role> on this plan`; it is not
  possible to send with no scope.
* Sent feedback surfaces a durable tracking chip that advances through
  `sent → routed → delivered → acknowledged` (or → `undeliverable` /
  `withdrawn`), each transition time-stamped, and every message is
  retrievable from the Delivery ledger, newest first, until explicitly
  deleted.
* Binding feedback closes exactly the target's next gate: `grogu plan gate
  <id> --stage <target-stage>` reports the plan blocked with the feedback
  id and message summary; the gate reopens the instant the acknowledgement
  event arrives from the target agent, and the delivery chip flips to
  `acknowledged`.
* Binding feedback that becomes undeliverable does **not** silently hold
  the gate closed: the ledger reports `undeliverable`, the plan's gate row
  copy switches to `Feedback f-<id> could not be delivered. Gate is open
  again.`, and the gate reopens.
* The Delivery ledger `Withdraw` action, available only on binding
  feedback before acknowledgement, reopens the gate with reason
  `feedback f-<id> withdrawn` and writes a `feedback withdrawn` event to
  the audit timeline.
* When a plan is in scope, the status bar shows `Agents · N live · N
  stuck`, the right-panel segmented control offers `Comments · Inspector
  · Agents`, and any selected node or edge with observable activity in the
  last 24 h shows an `Agent activity` section in the Inspector.
* When no plan is in scope, stage tabs and plan mode tabs are dimmed with
  `aria-disabled="true"`; only the Control room tab, search, agents chip,
  presence slot and user menu are active in the mode bar.
* Control room filters can be combined across role, plan, workstream,
  state and freshness bucket; the active filter set is mirrored as
  removable chips at the top of the grid; a `Clear all` link appears when
  any filter is active.
* Density toggles between Grid (320 × 180 cards) and List (40 px rows,
  proper `<table>` semantics with sortable columns); the choice persists
  in `localStorage`.
* Focus order in Control room is filters → density → grid (roving
  tabindex) → drawer header → drawer body → composer target → textarea →
  binding switch → `Send`, and every action is reachable by keyboard.
* Empty, quiet, stuck, finished, disconnected, error, filtered-to-zero and
  ledger-empty states use the literal copy under "Control room states" and
  "Control room empty states"; no state is left to the framework's
  defaults.
* Live-region announcements accompany every state transition on a focused
  card (`engineer on p-20260905-aab017: Stuck at applying patch. 6 minutes
  without new event.`) and every delivery transition on a focused feedback
  chip.

## Left to the engineer

Deliberately not specified:

* The exact React component decomposition. The design fixes behaviour and
  literal copy; how components are named or split is the engineer's call, as
  long as the accessibility and interaction contracts hold.
* The precise node and edge component types beyond the visual result. The
  design describes appearance and behaviour; picking between smooth-step,
  bezier or a custom edge is the engineer's call.
* SVG overlay coordinate mapping strategy — whether the overlay lives inside
  the transformed viewport or above it and is projected — provided marquee,
  annotations and connectors-in-flight all remain crisp and hit-testable.
* Whether the freehand tool simplifies its stroke with an added dependency
  (out of scope under the pinned runtime deps) or a hand-rolled
  Douglas–Peucker step.
* The choice of `IntersectionObserver` versus scroll-linked math for the
  comment rail collision-push layout, as long as the visible result meets
  the 8-px-minimum rule at every scroll position.
* The persistence key layout for panel widths, mode collapse states, and
  the crash-recovery queue in `localStorage`, provided the keys are
  per-plan-namespaced.
* The exact wording of tool-tips (hover labels) for icon-only buttons,
  provided they exactly match the action name from the command palette.
* The specific `dagre` layout options for Dependencies mode, provided the
  produced layout is deterministic given the same graph.
* Whether the Compiled Markdown preview renders HTML via the same
  server-produced HTML as Document mode (preferred) or by re-running
  `grogu_markdown` in the browser bundle. Either is acceptable if the
  invariant against `dangerouslySetInnerHTML` on unsanitised text is
  preserved.
* Any performance instrumentation. The design specifies user-visible
  feedback thresholds; the mechanism is the engineer's call.
* The exact HTTP shape of `/api/agents`, `/api/agents/events`,
  `/api/feedback` and the acknowledgement event stream, provided the wire
  contract is observable-only (no reasoning, no raw tool arguments) and
  the durability, delivery-state and gate-blocking semantics of feedback
  are honoured end to end.
* The `stuck_threshold` default is 5 minutes; whether it is user-tunable
  in this release is the engineer's call, provided the current value is
  always visible on the card.
* The exact vocabulary of "outcome" tokens on tool rows beyond the fixed
  three (`ok`, `err`, `timeout`) is the engineer's call, provided each is
  a bounded machine token and none carries user content.
* Whether the agent grid uses CSS Grid `auto-fit` or a fixed column count
  per breakpoint, provided it lands on the stated cards-per-row at each
  breakpoint.
* How the Control room correlates observable events to plan object ids
  (via the graph's revision index, a server-side join, or per-event
  metadata), provided the client never has to parse prose to know what
  changed.
