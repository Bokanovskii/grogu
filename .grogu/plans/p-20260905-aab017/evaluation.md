# The `.plan` document graph — evaluation plan

## What evaluation asks that testing does not

Testing asks whether the 96 checks pass. Evaluation asks the two questions no
assertion answers:

* **Is this actually better than the Markdown it replaces?** A plan system that
  is correct, deterministic and unpleasant is a failure, because nobody will
  plan in it and the architect will go back to writing prose.
* **Does the compiled artifact genuinely serve an agent?** The standing user
  steering makes this binding: the Markdown must be something another agent can
  consume *efficiently*, not merely something that round-trips.

Evaluate on the finished, merged integration branch, with the full suite green
and no open defect. Run as the user (no `GROGU_ROLE`) except where a check names
a role.

## The end-to-end threshold

This is the bar. The work does not ship below it. Every figure is measured on
this machine and reported with its number, not with an adjective.

| # | Measure | Threshold |
|---|---|---|
| T1 | The 134 testing checks | 134/134 pass with evidence |
| T2 | Full suite `python3 -m unittest discover -s tests -q` | green, and **≥ 706** tests plus the review suites, i.e. no reduction against the pre-plan baseline |
| T3 | Real-plan migration: migrate `p-20260904-6095a8` and `p-20260826-49fd23` | both migrate, both pass `plan doc compile --check` and `plan doc verify`, and the compiled stage text is semantically equivalent to the originals |
| T4 | Cold workspace load, populated plan, all five modes reachable | first paint **< 2.0 s**, interactive **< 3.0 s** |
| T5 | Compile the 400-node / 900-edge fixture | **< 500 ms** |
| T6 | `plan doc projection --role engineer --budget 40000` on a real migrated plan | **< 200 ms** warm, **< 1 s** cold |
| T7 | axe serious + critical violations, each of five modes, empty and populated | **0** |
| T8 | Non-loopback network requests from the page across a full exercise | **0** |
| T9 | Keyboard-only completion of the twelve operations in testing check 96 | 12/12 reachable |
| T10 | Bundled runtime JavaScript, gzipped | **< 400 KB** total |
| T11 | All three compiler identities — completeness, invertibility, graph equivalence — over every role projection of every stage of every migrated plan in this repository | 100%, with the three set differences of Identity 1 empty |
| T11b | Revisions in the log with a matching compiled artifact at the same revision and digest | 100%; a single exception is a do-not-ship |
| T12 | Agent-consumption task below (E4) | ≥ 8/10 on the rubric |
| T13 | Control-room leak scan: the seven distinctive strings in fixture **E**, searched across every API response, every returned structure, every exception and every byte on disk | **0** occurrences |
| T14 | Time from a locally observable agent event to it appearing on a visible board | **< 5 s** at p95 |
| T15 | Control room with 20 registered agents across 4 plans: snapshot build time | **< 300 ms** |

A threshold missed by a small margin is a finding to report with the number, not
a rounding error to absorb.

## E0 — Is the control room honest?

The control room's failure mode is not being wrong; it is being *plausible*.
Judge it by what it refuses to say.

* Start an agent, let it run a long build, and look at the board. Does it tell
  you the truth — "no observed action for 6 minutes, observer healthy" — or does
  it imply something it does not know?
* Unregister the session source and look again. Does the board degrade to a
  stated `tools: unavailable`, or does it quietly show zeros?
* Take one row and ask, for every figure on it, "how does Grogu know that?"
  Every answer must be an observed record or an explicit unknown. Report any
  figure you could not trace.
* Read the Limits panel. Would a user who read only that panel be surprised by
  anything the board does not show? If yes, the panel is wrong.
* Send binding feedback and watch it land. Did the composer name the gates
  before you sent it? Did the row show `unread`, then `read at`? Did the gate
  actually close?

**Pass:** every figure traceable, every unknown stated, feedback observed to
arrive.

## E1 — Is the compiled artifact excellent?

Take the compiled `implementation.md` of a real migrated plan and read it as if
you were the engineer about to build it.

Score each 0–2 and report the total out of 10; **≥ 8 passes**.

1. **Findability.** Can you locate every directive addressed to you without
   reading the whole document? Time yourself.
2. **Faithfulness.** Identity 1 already proves nothing in scope is missing, so
   do not re-derive it here. Judge instead whether what renders is *legible*:
   pick ten nodes at random from `plan doc show --json` and ask whether their
   relationships are understandable from the artifact alone, without opening
   the graph.
3. **Order.** Is the order obviously stable and obviously meaningful, rather
   than merely deterministic? A deterministic order that scatters related nodes
   scores 0 here even though it passes every test.
4. **Provenance.** Can you tell which revision you are reading, what it was
   compiled from, and what was elided, without running a command?
5. **Prose quality.** Does it read as a document, or as a database dump with
   headings? This is the one that decides whether anyone uses the system.

Report the artifact itself as evidence, not a summary of it.

## E2 — Is the editor worth opening?

Do a real task in the workspace: take a plan with no dependency structure and
add five tasks, three dependencies, one region grouping them, two directives and
one comment thread with an "Ask Grogu to revise" proposal that you then accept.

Report:

* how long it took, and how long the same edit takes in a text editor against
  the old Markdown;
* every point at which you had to guess what a control did;
* every point at which the canvas fought you — a drag that did not land where
  you released it, a connection that attached to the wrong handle, a selection
  that was lost by a mode switch;
* whether the four modes feel like four views of one document or four
  applications sharing a directory. If it is the second, that is the finding.

**Pass:** the graph edit is not slower than the text edit, and no step required
guessing.

## E3 — Does it hold up on a real plan?

Migrate `p-20260904-6095a8` — 46 KB of design, 37 KB of implementation, 20 KB
of testing, real Mermaid diagrams and a real review history — and then use it.

* Does the canvas at that size stay usable, or does it need a "tidy up" before
  it means anything?
* Is the dependency mode informative or a hairball?
* Does the document mode still read as the plan the architect wrote?
* Would you be willing to run the *next* plan in this system? Answer plainly.

## E3b — Is the compiler an asset or a liability?

The compiler is the piece this plan bets on. Judge whether it will still be
true in a year.

* Read `tests/fixtures/plandoc/golden/`. Would a reviewer seeing a diff there
  understand what changed about the output, or is it noise they would rubber
  stamp? If the second, the corpus is decorative.
* Add a node kind in a scratch copy. Count the places you had to change and
  whether the suite told you about all of them before you found them by hand.
  **Pass:** the suite named every one.
* Look at what `--check` prints on a failure. Is it the field that diverged, or
  a diff of two large files? A failure a human cannot localise will be
  suppressed the first time it is inconvenient.
* Time a warm projection read and a cold one, and report both. If the warm path
  is not obviously cheap, agents will stop using it and go back to reading the
  raw stage.

## E4 — Can another agent consume it efficiently?

This is the direct test of the user's standing steering, and it is worth more
than the rest.

Give a fresh sub-agent, with no context from this work, **only** the output of
`grogu plan doc projection <id> --role engineer --format md` for a migrated
plan. Ask it to answer, without any other tool call:

1. What are the active directives addressed to the engineer, by id?
2. Which tasks depend on which, and what is a valid build order?
3. Which validation criteria prove which task?
4. What was elided from this projection?
5. Which revision is this, and how would you check it is current?

Score 2 per question; **≥ 8/10 passes**. Then repeat with `--format json` and
confirm the answers agree. A disagreement between the two formats is a serious
finding — it means I5 ("no agent ever parses prose") is not held.

Report the sub-agent's raw answers.

## E5 — Did the review system survive its migration?

The user asked for migration rather than replacement, so the judgement is not
"does it still function" but "is anything worse".

Open the same plan in the old review workspace (from `1933fb3`) and in the new
document mode, side by side. Report every behaviour that got worse: a comment
that is harder to place, a thread that is harder to find, a round that is harder
to send, copy that got vaguer. **Pass:** nothing got worse.

## E6 — The things that would make this a bad foundation

Answer each, with evidence:

* Is there anywhere a fact about the plan can live outside the graph? Name it or
  state that you looked and found none.
* Is the serialization choice per artifact defensible on its own terms, or did
  one format get used everywhere by momentum? Check `manifest.json`'s part
  inventory against what is actually on disk, and confirm the one XML part —
  the derived SVG — is listed with a `renders` relationship and is genuinely
  regenerable.
* Could a third party hang data on a node without forking? Add an `ext` key
  under a reverse-DNS namespace you invent, run a migration and a compile, and
  confirm it comes back untouched. If it does not, the namespacing decision
  taken instead of XML has not actually been delivered.
* If someone arrived tomorrow with a DOCX plan, or with a consumer that needs
  an XML export, how much of this would have to change? The answer should be
  "an importer or exporter plus one part in the inventory". If it is more than
  that, the per-artifact claim was decorative.
* Is there anywhere an agent must parse prose to act? Same.
* If Vite, React Flow or dagre were abandoned tomorrow, what would have to be
  rewritten, and could the graph and its history survive it untouched?
* If a future plan needs a node kind nobody thought of, what does adding it
  cost — a schema line, or a compiler rewrite?
* Would a second concurrent editor on another machine break anything we
  promised? (We promised nothing; confirm the promise is actually absent from
  the docs rather than implied by them.)

## Reporting

One report, attached to the plan, containing: the twelve threshold figures; the
E1 and E4 scores with their raw material; the E2 timings and friction list; the
E3 and E5 verdicts in plain words; and the E6 answers. Recommend ship or
do-not-ship in the first line, and put the single most important reservation in
the second.
