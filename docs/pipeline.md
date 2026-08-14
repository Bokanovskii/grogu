# The architect / designer / engineer / tester pipeline

The question this answers: how does a request become a change that somebody can
trust, without three agents talking each other into a confident mistake or
burning a budget on conversation?

## Plans are artifacts

A plan is a file. Agents are handed a plan id and a role, and read what that
role may read. Nothing pastes a plan into another agent's prompt.

This is not a style preference. Passing a plan by value means every agent that
touches it pays for it, every time, and the copies drift. Passing it by
reference means one authoritative version, and re-reading it costs a file read.

```
<repo>/.grogu/plans/<id>/manifest.json     status, stages, loops, steering, audit
<repo>/.grogu/plans/<id>/design.md         optional, what the result should be
<repo>/.grogu/plans/<id>/implementation.md the engineer's input
<repo>/.grogu/plans/<id>/testing.sealed    the tester's input
<repo>/.grogu/plans/<id>/evaluation.sealed optional, end-to-end quality
<repo>/.grogu/plans/steering.json          repository-wide, outlives any plan
<repo>/.grogu/plans/friction.json          recorded friction awaiting review
```

The task store already lives at `<repo>/.grogu/tasks/`; plans sit beside it for
the same reason — one file per unit, trivial to merge, reviewable in a pull
request.

## Most requests are not this at all

Grogu is a general assistant. Most of what it is asked to do — look something
up, read a thread, draft a note, think through a decision — is not work on the
repository, and none of it wants an architect.

`grogu plan triage` answers this first and separately from how substantial the
request is, because the two questions are independent and the second is
meaningless when the first is no. It reports `software: false` and stops there.

The test is deliberately asymmetric. Listing what software work looks like fails
badly — "a plan for the new indexer" matches no vocabulary and is real work — so
the check runs the other way: name the domains that are plainly not this
repository, and route direct only when no software signal is present either.
Being wrong toward "personal" would silently disable the pipeline on real work,
which costs far more than an occasional needless planning cycle.

## Not everything needs a plan

`grogu plan triage "<request>"` decides deterministically. Spending a model call
to decide whether to spend model calls is precisely the waste this is meant to
avoid, so it is patterns and arithmetic.

It is deliberately biased toward planning: an unnecessary plan costs some
tokens, while skipping a plan on real work costs a rewrite. But questions,
steering, retrieval and obvious small edits route `direct`, and an explicit
"just do it" always wins. The failure mode being avoided is a harness that
solemnly plans its way through "what does this function do".

## Architecture decisions are researched, not recalled

A model's knowledge of libraries, services, APIs, limits and pricing has a
cutoff, and architecture is where that hurts most: the deprecated dependency,
the quota that changed, the thing the platform now does for free. The failure is
quiet, because a stale recommendation is written with exactly the same
confidence as a current one.

So the architect looks things up before choosing them — `web_search` for the
current landscape, `web_fetch` for the vendor's own documentation and
changelog, the GitHub tools for whether a project is still maintained — and
records the source and the date in the plan. The engineer inherits those
decisions and cannot otherwise distinguish a researched choice from a
remembered one.

This is not fully enforceable; nothing on disk can prove a search happened.
What is checkable is the trace it leaves, so writing an implementation plan
that adopts something external and cites no source prints a warning. A warning
rather than a refusal, because the detection is heuristic and a false positive
that blocks a plan is worse than one that is merely read and dismissed.

## The design stage

Work with a user-visible surface gets a fourth role. `grogu plan triage` flags
it, `grogu plan new --design` adds the stage, and the designer writes it.

The reason it is a separate role rather than a section of the implementation
plan is empirical: models are markedly worse at design than at code, and the
gap is hard to see, because bad design output is fluent and looks finished.
So the designer runs on the strongest model available, works from a store of
the user's own taste, and produces a spec that is judged on its own.

### The design spec is not sealed

The testing plan is hidden from the engineer; the design spec is handed to it.
The two are not inconsistent, because the artifacts are different in kind.

A test is a *proxy* for correctness. Show the proxy and it gets optimised
against, and the signal is gone. A design spec *is* the requirement. Withholding
it does not preserve any signal; it just means the engineer invents an interface
and then gets marked against one it never saw.

What stays hidden is how the design will be *judged*: the architect folds design
acceptance criteria into the testing plan.

### The format

The spec is structured English carrying concrete values. Not HTML — an HTML
mockup is an implementation, so it makes the designer a front-end engineer,
encodes a hundred incidental decisions the engineer cannot distinguish from
deliberate ones, and does not survive a move to a native view or a terminal. Not
free prose either: "clean, modern, Apple-like" cannot be built, so the engineer
decides, which is the failure the role exists to prevent.

`grogu design template` prints the required skeleton, and `grogu plan write <id>
design` refuses a spec that skips a required section or leans on adjectives.
That refusal is the point. Consistent completeness — every state, including the
empty and error ones nobody enjoys writing — is most of what weaker design
output gets wrong, and it is the part a machine can check.

For a terminal or API surface the highest-fidelity artifact is a fenced block of
the exact intended output; it is unambiguous and directly testable. For a visual
surface it is a deliberately rough ASCII sketch, kept low fidelity so nobody
mistakes it for source. An HTML prototype is allowed only when interaction
itself is the risk, and then it is labelled behaviour reference, beside the
spec, never the spec.

### Design taste is learned, and user-scoped

Design preference belongs to a person, not a repository. It lives in
`$GROGU_HOME/design/` and applies everywhere.

`grogu design remember` records something the user actually said.
`grogu design suggest` queues something the designer *inferred* — from a
rejected layout, a rewritten sentence — and it does not apply until the user
confirms it. That split is the same consent boundary personal memory uses, for
the same reason: an agent that silently learns a taste the user never expressed
produces confident work nobody wanted, and afterwards nothing can tell an
inferred preference from a stated one.

### Signing off on what was built

A spec survives contact with an implementation about as well as any other plan.
So the designer is spawned a second time, after implementation, to look at the
interface running — screenshots of every state for a visual surface, captured
output for a terminal one.

`grogu plan gate <id> --stage test` stays shut until `grogu plan design-review`
records a pass, and a pass requires evidence. Reviewing the diff instead only
establishes that the code matches the words in the spec, which was never the
question.

## Why the engineer cannot read the test plan

An implementation written with the tests in view converges on satisfying them.
That is a real risk with an agent, which is very good at producing something
that passes a check it can see. The result passes and proves nothing.

So the testing and evaluation plans are sealed: `zlib` + base64 behind a header.
The engineer that opens `testing.sealed`, greps the tree, or reads a directory
listing absorbs nothing.

**This is a guard against accidents, not a security boundary.** An agent running
as the same user can decode it in one line. What it buys is that leakage has to
be deliberate rather than incidental, and that role-scoped reads are recorded in
the manifest's access log — including refused ones. The same honesty applies
here as to the codemode sandbox: it stops mistakes, not intent.

A second consequence is the point of the design. If the testing plan is the only
thing standing between a plausible implementation and a merge, it has to be
good. That pressure is intentional; it is what makes testing and evaluation
infrastructure grow instead of being written to fit whatever the code already
does.

Once the work lands, the reason to seal is gone. `grogu plan finalize` unseals
every stage into plain Markdown so the pull request carries the plans it
implements.

## Testing is not evaluation

Testing asks whether the change does what it claims: deterministic, pass or
fail. Evaluation asks whether the result is actually good — end-to-end, usually
scenario-based, often non-deterministic, sometimes needing a rubric and repeated
runs.

Most changes need only the first. Add the second (`grogu plan new --eval`) when
a green test suite would not actually tell you the change was worth making.

## Gates, not instructions

`grogu plan gate <id> --stage implement|test|evaluate` exits 3 when the pipeline
may not proceed and prints why.

An instruction in a prompt is something a determined autopilot run can talk
itself out of at three in the morning. A non-zero exit code is not. In
particular, when the user asked for a plan directly, `--review-required` records
it and the gate refuses until `grogu plan approve` runs. Autopilot does not get
to decide that the user probably would have approved.

## The loops

Three feedback paths, each ending somewhere specific:

**Engineer or tester → architect.** `grogu plan amend` records a claim and its
evidence. The architect must check it against the code itself before resolving
with `--verified`; that flag is an assertion, and the alternative — deferring to
whichever agent spoke last — is how a wrong plan becomes an agreed plan. The
outcomes are `--accept` (rewrite the stage), `--reject` (the plan stands, with a
reason), and `--guidance` (the plan is fine, the ambiguity was elsewhere).

**Tester → engineer.** Every failure is routed, and routing is the hard part.
Implementation defects go to the engineer; a broken test stays with the tester;
a plan that asked for the wrong thing, or for something unverifiable, goes to
the architect. Loops fail far more often from misrouting than from any single
wrong fix — an engineer "fixing" a broken test is worse than the original
failure, because now the evidence is wrong too.

**Architect → user.** Only for questions of intent.

Both loops are capped, and the caps end in different places. The engineer/tester
exchange escalates to the *architect* after a few unproductive rounds, because
two agents trading fixes without converging is a symptom of an ambiguous plan,
and the architect owns the plan. Resolving the escalation resets the budget. The
amendment loop with the architect ends at the *user*, because the architect is
the top of the pipeline and the only thing left to disagree about is what was
wanted.

## Steering that survives the session

The user steers from their own session, but the work is done by subagents that
cannot be reached from outside. `grogu plan steer` records role-scoped,
sequence-numbered notes, and delivery has three paths:

* **At spawn:** `grogu plan brief --role <role>` includes the full standing set,
  so an agent started an hour after the note still gets it.
* **Mid-flight:** unread notes are appended to the output of *any* `grogu`
  command the agent runs. The agent is already running heartbeats, gates and
  status checks constantly, so steering rides along with something it did
  anyway. Nothing has to remember to poll, because polling is not the mechanism.
* **Binding:** `--requires-replan` moves the plan to `needs_review` and closes
  the gates until the architect revises it. Delivery alone is weak; an agent can
  read a note and reason its way past it.

Ack watermarks are per role rather than per agent, which is why the brief ships
the complete set and only mid-flight polling uses the delta.

## The architect assigns, it does not just describe

A workstream carries more than a file set. It carries the model it should be
built on and, optionally, a review it must pass before the tester sees it.

Both are things only the architect can judge. From inside a workstream every
part looks equally important; from above, one is a mechanical rename and another
is the concurrency that will quietly corrupt data in production. So the stronger
model and the second pair of eyes go where they are worth paying for, rather
than uniformly (expensive) or nowhere (worse).

Assigning different models across workstreams is also cheap diversity: two
instances of the same model agree with each other's mistakes.

`grogu plan review` records the outcome, and refuses a passing verdict that says
nothing about what was examined — a bare pass is indistinguishable from a review
that never happened. Until each required review has one, `gate --stage test`
stays shut.

## Parallelism is declared, not inferred

The architect declares workstreams with the file globs they own.
`grogu plan workstreams --check` refuses overlapping sets, resolving globs
against the working tree when the files exist. Fan-out happens only along clean,
independent workstreams, one worktree each.

Agents are not free. A sequence of dependent steps does not get faster with more
of them; it gets more expensive and acquires merge conflicts.

## Improving the pipeline itself

Every signal needed for a retrospective is already in the manifest, so
`grogu plan retro <id>` is arithmetic rather than an agent re-reading a
transcript. An improvement loop that is expensive to run does not get run.

Each signal points at what should change:

| Signal | What it means | Target |
| --- | --- | --- |
| accepted amendments | the plan was wrong and somebody else found it | architect overlay |
| escalations | the plan was ambiguous | architect overlay |
| plan-routed defects | the plan could not be verified as written | architect overlay |
| repeated implementation defects | conventions were not written down | engineer overlay |
| test-routed defects | the harness itself is unreliable | test infrastructure |
| user steering with `--requires-replan` | the user had to correct the plan | architect overlay |

That last one is the most valuable: every time the user has to steer, something
they considered obvious was missing from the overlay.

`grogu plan friction` aggregates across plans and marks signals that have
repeated. One bad plan is noise; the same finding three times is a change to
make. Agents also record friction directly with `grogu plan friction --note`,
so "the fixtures take four minutes to build" reaches a review queue instead of a
final message nobody reads.

## Repository-specific roles

The harness carries the pipeline; each repository carries its own knowledge:

```
<repo>/.grogu/roles/architect.md
<repo>/.grogu/roles/engineer.md
<repo>/.grogu/roles/tester.md
```

`grogu plan brief --role <role>` assembles the shared contract, the repository's
overlay and current steering into one prompt. Architecture, invariants,
validation commands and known traps belong in the overlay, where they can be
corrected by the retro loop — not in the harness, which cannot know them and
cannot keep them current.

## Models

| Role | Model | Why |
| --- | --- | --- |
| architect | `claude-opus-5` | planning errors are cheap to fix here and expensive later |
| designer | `claude-opus-5` | design is where weak output is hardest to spot and costliest to unwind |
| engineer | `claude-sonnet-5` | general implementation, escalating when the work warrants it |
| tester | `grok-4.5` | a different family from the engineer, on purpose |

The tester's model is not arbitrary. It audits the engineer's work, and two
models from the same family share the same blind spots — including the
comfortable ones.
