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

The larger case is not dependencies at all. Most substantial work — a search
index, a rate limiter, a sync protocol, a permissions model — is a problem other
people have already solved, failed at publicly, and written up. A model asked to
design one from recall produces something like the average of everything written
about it, which reads as competent and quietly omits the two or three decisions
that actually determine whether it works at scale. Reading the current state of
the art first is the difference between a design that has considered the
tradeoffs and one that has merely reproduced the consensus shape.

So the architect looks things up before choosing them — `web_search` for the
current landscape and for how the problem is being solved now, `web_fetch` for
the vendor's own documentation and changelog, the GitHub tools for whether a
project is still maintained — and records the source and the date in the plan.
The engineer inherits those decisions and cannot otherwise distinguish a
researched choice from a remembered one.

Proportionality is judged by consequence, not size: a change that is wrong by an
edit needs none of this, and a change that is wrong by a rewrite — storage,
protocol, data model, anything with migration or money attached — earns real
reading first.

This is not fully enforceable; nothing on disk can prove a search happened.
What is checkable is the trace it leaves, so writing an implementation plan
that adopts something external and cites no source prints a warning. A warning
rather than a refusal, because the detection is heuristic and a false positive
that blocks a plan is worse than one that is merely read and dismissed.

## The design stage

Work with a user-visible surface gets a fourth role. `grogu plan triage` flags
it, `grogu plan new --design` or `grogu plan shape --add design` adds the stage,
and the designer writes it. Nothing else may: the architect that adds the stage
is refused if it tries to write the spec.

That left the architect able to open a stage it could not brief, and the only
channel that reached a designer was `plan steer --role designer`, which arrives
attributed to the user — an architect's statement of work put into the user's
mouth, to the role whose whole job is weighting the user's taste above its own.
`grogu plan commission <id> designer --brief "..."` is that channel, and it
arrives in the designer's brief under the architect's name.

A plan carries more than prose. `grogu plan attach <id> --file check.py
--stage design --note "..."` puts an artifact in the plan directory, names it in
every later role's brief, and ships it in the pull request. This exists because
the first designer wrote a script that re-derived every fenced block in its spec
from the spec's own stated rules — which caught a real contradiction between its
prose and its examples — and had nowhere to put it, so the tester's only options
were to rebuild it or to assert the examples without checking they were mutually
derivable. Attachments are scanned against the published-destination rules on
the way in, because unlike a plan body they are usually a file lifted whole out
of a working directory.

`.grogu/plans/.gitignore` keeps `manifest.json`, `revisions/` and sealed stages
out of the index. The manifest carries session ids, actor strings and the full
text of every amendment and steering note; the revisions directory holds
superseded drafts. `finalize` ships the plan Markdown and the attachments, which
are the account of what was done, and nothing else.

Attach a check with `--verifier` and it stops being a comment: `grogu plan
verify` runs it, records the result against the commit it ran at, and the test
gate refuses while an attached verifier has never been run, has failed, or last
passed at a commit the tree has moved past.

An unwritten design stage blocks the *implement* gate, not only the test gate:
there is no point building against a spec that does not exist yet.

The design stage runs *before* the user's review, and is exempt from the review
hold. That looks backwards until you try it the other way: `plan approve`
refuses while any stage body is missing, so a held plan whose designer could not
finish would deadlock, and a plan reviewed without its spec is a plan reviewed
without the part the user has the most opinions about.

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
design` refuses a spec that skips a required section, leans on adjectives, or
hands back the skeleton with its instructions still in it. That last check was
added after an architect piped `grogu design template` straight into `plan
write` and it was accepted: the skeleton has every required heading and uses no
adjectives, so the one artifact guaranteed to pass every structural check was
the empty one — a single pipe between an unwritten spec and a plan the user
would be asked to approve.
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

Taste arrives two ways, and `grogu design status` names which. The user can
**adopt** a named set — `grogu design seed --apple` — which is a real answer to
"what does good look like", applied in full, with nothing further owed. Or they
can **state** a principle in their own words, which is stronger because it is
about their product. Status used to call an adopted set "seeded defaults" and
report "nothing learned from you", which told a user who had deliberately
chosen Apple's set that they still owed an action they could not find.

`grogu design remember` records something the user actually said, and refuses
to run for any role: it asserts the user's own taste, so only the user may
assert it.
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

One path that *is* closed: `--role` used to be a bare assertion, so an engineer
could type `grogu plan show <id> testing --role tester` and read the plan it was
about to be judged against. A session that has declared itself in `GROGU_ROLE`
can no longer claim to be a different role — the request is refused rather than
answered. An agent that never declares a role is still bound only by the
contract, and that remains true by design: the user runs these commands too.

A second consequence is the point of the design. If the testing plan is the only
thing standing between a plausible implementation and a merge, it has to be
good. That pressure is intentional; it is what makes testing and evaluation
infrastructure grow instead of being written to fit whatever the code already
does.

Once the work lands, the reason to seal is gone. `grogu plan finalize` unseals
every stage into plain Markdown, writes `record.md` — one page saying which
optional stages were declined and why, which amendments were accepted and on
what evidence, what the tester found, what the user corrected mid-flight — and
stages all of it, so the pull request carries the plans it implements without
anyone remembering to `git add`. `manifest.json` is not staged: it is working
state, carrying session ids and the raw text of every exchange.

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

If an architect applies that hold to the wrong plan, the supported inverse is:

```sh
grogu plan shape <id> --clear-review --why "the hold targeted the wrong plan" \
  --role architect
# Or, from the person's own undeclared shell:
grogu plan shape <id> --clear-review --why "the hold targeted the wrong plan" \
  --as-user
```

A declared architect or the person explicitly passing `--as-user` may use it;
an undeclared caller is refused rather than assumed to be the architect.
`--as-user` is also refused when `GROGU_ROLE` identifies an agent. The non-empty
reason is recorded with actor, time, authorization role and `as_user` in the
manifest event log. It works only before approval on a `draft` or
`needs_review` plan. It clears only `review_required`: stage text and state are
untouched, and an independent `needs_review` steering block remains in force.
Approved, amending, superseded, complete and unknown states are refused rather
than repaired by weakening another gate.

Role checks default to deny. Completing a sealed stage or finalizing a plan
requires a declared role — `GROGU_ROLE`, `--role`, or `--as-user` for the user
— because a check that only applies to callers who identify themselves is
optional to exactly the role it exists to stop.

An identified agent's role claim is sticky. `GROGU_AGENT` is the explicit
identity; `grogu plan brief` persists it with the role in the session binding,
and `agents_seen` records the association on the plan. Later commands recover
that identity when a fresh shell has lost `GROGU_ROLE` and `GROGU_AGENT`, and a
conflicting `--role` is refused before a sealed stage is returned. A new tester
or architect remains legitimate because it has a different `GROGU_AGENT`; do
not reuse one agent name across roles.

Be clear about how strong that is. Everything here runs as the same user, so a
determined agent can invent a fresh identity or pass `--as-user`; `--as-user`
is refused when `GROGU_ROLE` is set, but remains available to a human whose
shell inherited only a persisted binding. This is the same class of protection
as the seal itself: it stops drift, shortcuts and plausible-sounding
rationalisation. It is not a cryptographic security boundary.

Verification expires. A defect filed after the tests were marked complete
reopens the testing and evaluation stages, because "complete" was a claim about
a build that the fix is about to replace. The dual matters just as much: testing
cannot be *marked* complete while a defect is open, because a pass recorded over
a known failure is not a pass. Without both, a defect could auto-close when the
engineer re-completed implementation, and the plan would ship with nothing
having been re-run.

Rounds count bounces, not bugs. A first test pass that finds three real problems
is a good test pass; what signals that the engineer and tester are not
converging is a failure arriving on a route that was already fixed once. And
while an escalation is open the defects stay open too, because they are the
evidence the architect was called in to look at.

That rule has a blind spot on its own, so there is a second trigger. An engineer
who never fixes anything never produces a bounce, and failures would pile up on
one route forever with the round count sitting at zero. Six unresolved defects
escalates as a stall, counted across every route rather than per route, because
five open implementation failures alongside five open test failures is a plan
that has plainly stopped. Six is twice the round cap and no better
justified than that; it is the point past which the next failure report is not
telling anybody anything new. Once the architect has ruled on a pile, the stall
trigger holds until the pile is cleared, so a tester filing before the engineer
sweeps does not call the architect straight back for something it has already
seen.

There is a third trigger for the slowest failure of all. A green test pass
resets the round count, so a plan that produces one fresh bug after every clean
run bounces forever without anyone asking whether the plan is the problem.
Three full retest cycles — green, defect, green again — escalates on the next
failure, with a claim that says the question is the plan rather than the fix.

All three caps are guesses that should be revised once real plans have run
through the loop.

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

## The supervisor

Four roles do the work. A fifth coordinates them, and for a long time it was
the session the user happened to be typing into, with no name and no limits.

The supervisor is the session the user talks to. It decides whether a request
needs the pipeline at all — most do not — spawns the roles that do, carries the
user's steering into agents that cannot be interrupted from outside, harvests
what they report, and fixes the harness itself. It writes no plan, no spec, no
implementation and no test.

Naming it is not a formality; it removes powers rather than granting them.
`grogu plan approve` refuses every declared role, so a supervisor that declares
itself gives up the ability to approve a plan on the user's behalf — which is
the one thing an autopilot run must never do and, while the supervisor was
just an anonymous shell, the one thing nothing stopped it doing. It cannot
write or complete a stage either; if a plan is wrong it raises an amendment and
the architect adjudicates.

The subtler boundary is attribution. The supervisor's whole job on the steering
channel is carrying somebody else's words, so `grogu plan steer --relayed`
marks a note as the user's and anything else is attributed to the supervisor.
That distinction is load-bearing for exactly one reader: the designer, whose
job is weighting the user's taste above its own inference. A designer told that
the supervisor's guess is the user's stated preference is being corrupted at
the one input it runs on. This is the same reason the architect commissions
instead of steering, and the same reason `grogu design remember` refuses every
role.

Nothing here can detect a relay the supervisor invented. What it does is put
the claim in the record, where the user reads it in the finished plan.

## Steering that survives the session

The user steers from their own session, but the work is done by subagents that
cannot be reached from outside. `grogu plan steer` records role-scoped,
sequence-numbered notes, and delivery has three paths:

* **At spawn:** `grogu plan brief --role <role>` includes the full standing set,
  so an agent started an hour after the note still gets it.
* **Relayed:** `grogu plan steer` prints which agents are running right now, so
  the session that spawned them can push the note in with `write_agent` while
  the user is still at the terminal. This is the only path that does not wait,
  because a running subagent cannot be reached by anything except its spawner.
* **Mid-flight:** unread notes are appended to the output of *any* `grogu`
  command the agent runs. This is the fallback, and its latency is honest: an
  engineer deep in an edit may not run a `grogu` command for half an hour, so
  the agent prompts ask for a poll at decision points — before starting a
  component, before completing a stage, after a failed run — rather than on a
  clock. The notice goes to stderr for a human at a terminal, and to stdout when
  stdout is redirected, because an agent harness that captures only stdout was
  otherwise dropping it.
* **Binding:** `--requires-replan` moves the plan to `needs_review` and closes
  the gates until the architect revises it. Delivery alone is weak; an agent can
  read a note and reason its way past it.

Each agent is shown a note exactly once: delivery acks it. Repeating it on every
subsequent command bought nothing and charged for the same guidance a dozen
times over in a context window that is the scarcest thing here. The cost is that
an agent which discards the output has lost the note, which is why the binding
kind lives in state instead — the gate refuses and quotes the text back at the
point where it actually bites, rather than relying on the agent still having it
in view.

Ack watermarks are per *agent*, not per role. With parallel workstreams several
engineers run at once, and a role-wide watermark meant the first one to poll
marked the note read for all of them — steering that reaches one of three agents
is worse than steering that reaches none, because it looks delivered. An agent
is identified by its working directory, since each workstream gets its own
worktree, or by `GROGU_AGENT` where they share one. Getting this wrong shows a
note twice, which is the right direction to fail in.

### Seeing what to steer

Steering the pipeline while blind to it is guessing, so `grogu watch` shows the
board: every agent active in the window, its role and plan, the last `grogu`
command it ran and how long ago, how many of its calls failed, and — per plan —
the stage states, open defects, open amendments, escalations, and the notes each
role has not read yet. `-f` redraws it; `--json` is for scripting.

The feed underneath it is passive by design. No agent is asked to report status,
and no model spends a token producing it: every `grogu` invocation already passes
through a single exit path, so that path records that it happened. An agent that
has gone quiet is visible precisely because the record is a side effect of work
rather than a description of it — a stuck agent stops emitting, and a lying agent
cannot say otherwise.

Two deliberate limits. Only the *subcommand name* is stored, never arguments:
`grogu plan steer "…"` and `grogu plan friction --note "…"` carry exactly the
text that should not accumulate in a file nobody remembers exists. And the log
lives in `GROGU_HOME`, not in a repository, so agents spread across worktrees
appear on one board and nothing about it is ever committed.

Agent identity is `GROGU_AGENT` when set and otherwise the working directory;
role and plan are attributes bound to that identity. When a fresh shell loses
the environment, the role and explicit agent name come from the session binding
recorded for that directory. Give each agent its own worktree and always give
agents sharing a checkout distinct `GROGU_AGENT` values, or attribution becomes
ambiguous.

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
independent workstreams, one worktree each — literally: `grogu plan
workstream-worktree <id> --name <name>` gets or creates a dedicated `git
worktree` for that workstream, at a path and branch computed the same way for
every caller (`workstream/<plan>/<name>`, under `<repo>-worktrees/`), and
prints a `cd <path>` line so the engineer or the supervisor that spawned it can
use the answer directly instead of parsing it. `grogu plan workstreams` shows
which workstreams already have one; `--list`/`--remove` on the same command
inspect or clean them up.

Disjoint path globs alone only constrain what an agent is *supposed* to touch.
Harness friction #40 was a workstream's untracked scratch files landing in a
shared checkout outside any declared path — the guarantee had nothing actually
enforcing it. A dedicated worktree per workstream removes the shared checkout
entirely, so there is nowhere for one workstream's stray file to cross into
another's.

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

Friction comes in two kinds and they belong in different places. A gap in this
repository's conventions is this repository's problem and stays in
`.grogu/plans/friction.json`. A gap in *Grogu* — a command that should exist,
output that had to be parsed by hand, three calls where one would do — goes to
`$GROGU_HOME/friction.json`, pooled across every repository.

You do not have to pick correctly. Nineteen notes were filed during one
dogfooding session and fifteen of them named a failing `grogu` command while
omitting `--harness` — filed, by agents who had read this page, into the bucket
nobody reads. A note that quotes a grogu command is now pooled whatever flag it
arrived with, and `--repo-only` overrides that for the rare note that mentions
one while genuinely being about the project.

That split matters more than it looks. An engineer in some unrelated repository
who hits a missing grogu command would otherwise write the complaint into that
repository, while the harness is fixed here — filed in the one place its reader
never looks. Pooling is also the only scope at which the signal exists: the same
gap hit once is an anecdote, hit in four repositories it is the next change.

Nobody has to remember to read it. Once three notes are pending, the user's own
session is told, on whatever `grogu` command it next runs, at most once a day —
and never on the friction report itself, which would spend the day's one prompt
on the command that least needed it. Every role records friction, including the
architect, and so does an ordinary Grogu session outside any plan, which is
where most of it is actually hit.

### Knowing when to open a PR against Grogu

A list of complaints is not a work queue, so the notes are clustered and judged
rather than counted. Counting is the wrong measure: five agents describing one
missing command is a single change, and five unrelated papercuts are five. Notes
are grouped by token overlap — greedy, deliberately unclever, because the corpus
is tens of short sentences and a wrong grouping costs a glance, not a mistake —
and a cluster becomes **ripe** when any of these holds:

| signal | why it is enough |
| --- | --- |
| hit in two or more repositories | cannot be explained by one project's quirks |
| hit three or more times | repetition inside one repository counts, just later |
| open thirty days or more | a real gap nobody got to is still a real gap |

Ripe clusters are what the daily banner announces, and it says where they get
fixed: outside Grogu it points at the Grogu repository, and a session *inside*
Grogu is told to propose the work now. `grogu plan friction --ripe` prints the
detail, `--ripe --all` shows the clusters that are still accumulating.

Claiming closes the loop. `grogu plan friction --claim f1 --reference <pr>`
marks a cluster as being dealt with, which drops it out of the ripe set so it is
never proposed twice, and `--harness --resolve <seq>` retires the notes when the
fix ships. The result is that the question "is there harness work worth doing?"
is answered without either of us having to ask it.

`grogu plan friction` aggregates across plans and marks signals that have
repeated. One bad plan is noise; the same finding three times is a change to
make. Agents also record friction directly with `grogu plan friction --note`,
so "the fixtures take four minutes to build" reaches a review queue instead of a
final message nobody reads.

## Skills the agents write for the agents that come after them

Friction covers the case where Grogu or the repository is *wrong*. The other
case is that nothing is wrong and the knowledge is simply missing: an engineer
works out which command actually proves a change here, spends an hour on it,
finishes the task, and the next agent starts from an empty context and spends
the hour again.

```
grogu skill propose <name> --description "when does this apply" --file body.md \
    --why "what happened that made this worth writing"
grogu skill proposals            # what is waiting on a decision
grogu skill show <n>             # the body, and any proposal linked to it
grogu skill accept <n>           # writes .github/skills/<name>/SKILL.md
grogu skill decline <n> --note "why"
grogu skill contest <n> --note "why that reason no longer holds"
grogu skill suggest              # lessons that keep recurring, unwritten
```

Four things keep this from becoming a landfill.

**Proposing is not installing.** A skill is read by every agent that comes
after, which is the same authority a role contract has, so the four pipeline
roles propose and only the user or the supervisor accepts. Accepting writes a
file into `.github/skills/`, where it is reviewed in a diff like any other
change. An agent that could install one could rewrite the instructions the next
agent works under, from inside a single task, with nobody reading it.

**Proposals are pooled across repositories**, like harness friction, because
repetition across independent contexts is the only available evidence that a
lesson generalises. Near-matches are *linked*, never merged. That distinction
was bought expensively: the first version folded a near-match into the older
proposal, and an adversarial probe filed a dependency-licence audit and a
podcast mastering procedure under the same honest one-line description — "run
narrow validation before reporting success", which is true of both and tells you
nothing about either — and watched the second lesson disappear while its author
was told it had been recorded.

The lesson generalises past the threshold that let it through. "These two texts
share words" and "these two agents learned the same thing" are different
questions, and no tuning of the first answers the second; a matcher that is
sometimes wrong is fine, and a matcher that is sometimes wrong *and destroys
one input* is not. So both survive, each carries a `related_to` link to the
other, and whoever decides reads them side by side. A false link costs a glance.
A false merge cost a lesson.

Two signals are compared rather than one: the headline (name and description)
and the procedure (the body). Either alone is a bad judge in opposite
directions — agents writing one lesson often disagree completely about what to
call it, and unrelated lessons routinely share a summary. Only both agreeing
counts as the same lesson, and that verdict is used exclusively to *refuse*,
never to discard.

Linking is deliberately looser than refusing, because a link is a note to the
reviewer and a refusal is a lesson lost. Even so, word counting cannot see a
paraphrase: two agents wrote the same rollback-rehearsal procedure in different
vocabulary and scored 0.16, which no threshold recovers without linking
everything to everything. So the harness stops guessing at that point and hands
the question to the one party that can answer it — the agent proposing, which
has the lesson in mind — by naming the nearest existing proposals on every
`skill propose`. That list can only be produced *after* the proposal is filed,
since the proposal is what it compares against, so the way to act on it is
`grogu skill link <yours> <theirs>`, which amends both. Telling the agent to add
a flag and propose again meant filing the same lesson twice.

Both refusals name a way past themselves. `--not-the-same <skill-or-number>`
records that the agent read the thing it was pointed at and judged this to be a
different lesson; the override is shown by `skill proposals` and `skill show`,
so whoever reviews it sees what was overridden and can disagree. The target has
to resolve to an installed skill or a declined proposal -- an override naming
something the agent was never shown is not a note that it read anything, and
while the field took free text a probe put a credential-shaped string in it,
which then sat unredacted in a store that is pooled and reviewed in public. Without an escape hatch, every false
refusal is a lesson that is never written at all, which is the worse of the two
failures — a false acceptance is caught by the person who reads the proposal.

**An already-answered lesson comes back with the answer.** A proposal matching
an installed skill sends the agent to read that skill instead. A proposal
matching one that was declined is refused *with the reason it was declined for*
— a fresh context has no memory of being told no, so without that the same
rejected lesson returns every time an agent hits the same wall. The attempt is
still counted: a lesson declined once and re-derived by five agents is evidence
the decline was wrong, and `grogu skill contest` is how an agent argues that
without re-proposing under a new name.

**A body under 160 characters (once trimmed) is refused, and one over 20,000
is too.** A standing instruction that vague
costs every future agent a guess about what it meant. Write what to do, in what
order, and how the result is checked. At the other end, a skill is the
procedure and not the material: a five-megabyte paste did not fail, it sat
there for minutes, which reads as a hang rather than as a mistake.

Like every other role boundary here, who may accept is trust-on-assert: an
agent that simply never sets `GROGU_ROLE` is the user as far as any of this can
tell. What is closable is the case that happens in practice — an agent that has
already said who it is and then drops the variable on one command — and the
session binding remembers that, which is enough to refuse. The binding is read
across the whole tree rather than at one exact path, because `cd src` was
otherwise enough to look like an anonymous shell; and it stops counting after
twelve hours, because a binding is never cleared — the shell that made it just
stops existing — and refusing forever on an agent that finished last week locks
the user out of his own checkout with a message about somebody who is not there.

Nothing here survives deleting `$GROGU_HOME/skills.json` (`~/.grogu/skills.json`
by default -- the store is pooled across repositories, so it is not inside any
of them). There is no journal
and no tombstone: pending proposals, decline reasons and echo counts all go
with it, and the only record left is whatever was already accepted and
committed under `.github/skills`. That is a real limitation and not a guarded
one.

`grogu skill suggest` answers the other half: recurring harness friction that
nobody has turned into either a fix or a procedure. It reads the friction
clusters that already exist rather than inventing a detector, because the
harness only ever sees its own commands — it cannot watch an agent repeat itself
in bash, so self-report is the only channel there is.

## What must not leave

Grogu reads private repositories, mail and messages, and writes to public ones.
The realistic failure is not that it decides to publish something private; it is
that private context follows it out through work it was asked to do — a
credential pasted into a config file and committed, a plan quoting a staging
connection string and attached to a pull request, a complaint about the harness
written in a private repository and proposed as an issue in this public one.

So the rule is enforced where the data crosses a boundary, not in a prompt:

| Boundary | Check |
| --- | --- |
| a commit | `grogu guard staged`, run by the pre-commit hook `grogu guard install` writes |
| a published plan | `grogu plan finalize` scans every stage and refuses |
| harness friction | redacted as it is written, because it is pooled across repositories |
| arbitrary text | `grogu guard scan <path>` or on stdin |

Findings come in two classes, and the distinction is the whole reason the guard
is usable. A **credential** blocks everywhere: there is no destination at which
a live token is fine. **Personal data** — an address, a phone number, a card
number — blocks only where the destination is published, because a colleague's
email in a private repository is not a leak, and a guard that fires on it is one
everybody learns to pass `--no-verify` around. An overridden guard is worse than
no guard, because it also carries an assurance.

The detectors are tuned for precision over recall for the same reason. They
recognise vendor credential shapes and hard-coded assignments, and deliberately
stay quiet on `os.environ[...]`, `${VAR}`, `config(...)` and placeholders like
`your-api-key-here` — the correct way to write the thing must never be flagged.
This means the guard will miss a secret with no recognisable shape. It is an
accident guard, not a security boundary, and the honest framing matters: it
stops the mistake, not an attacker, and never the user's own judgement about
what is fit to publish.

Being specific about that matters more than the detectors do, because the
failure mode of a guard is not missing something — it is being trusted to have
looked. It checks three places, and these are not among them:

| Not covered | Why |
| --- | --- |
| PR and issue bodies, review comments | written through `gh`, which is not hooked |
| Commit *messages* | only file contents are scanned |
| Web search and fetch arguments | a query is egress too, and unguarded |
| Agent transcripts and tool arguments | outside the harness entirely |
| Already-committed history | nothing is rewritten |
| `git commit --no-verify`, `grogu-allow-secret` | deliberate opt-outs an agent can use too |
| Secrets with no recognisable shape | the detectors are structural by design |
| Repository-local `.grogu` files | only the cross-repo friction pool is redacted |

So it is defence in depth beneath judgement, not instead of it. Anything Grogu
is about to publish — a PR body, an issue, a message, a search query — still has
to be read before it goes.

Two details are load-bearing. Only *added* lines are scanned, so a commit that
removes a leaked key is never blocked — blocking it would be blocking the fix.
And reports print a label and a location, never the value, so the scan output
is not itself a place the secret now lives.

The escape hatch is per line and stays in the diff. A line that genuinely must
contain a credential shape — a detector fixture, a documentation example — is
marked `grogu-allow-secret` in a comment, which a reviewer sees. The alternative
escape hatch is `git commit --no-verify`, which exempts the entire commit and,
worse, is a habit; the marker exists so nobody acquires it.

## Repository-specific roles

The harness carries the pipeline; each repository carries its own knowledge:

```
<repo>/.grogu/roles/architect.md
<repo>/.grogu/roles/designer.md
<repo>/.grogu/roles/engineer.md
<repo>/.grogu/roles/tester.md
```

`grogu plan brief --role <role>` assembles the shared contract, the repository's
overlay and current steering into one prompt. Architecture, invariants,
validation commands and known traps belong in the overlay, where they can be
corrected by the retro loop — not in the harness, which cannot know them and
cannot keep them current.

## Models

| Role | Model | Reasoning | Why |
| --- | --- | --- | --- |
| architect | `gpt-5.6-sol` | max | planning errors are cheap to fix here and expensive later |
| designer | `gpt-5.6-sol` | max | design is still high-stakes even though its output is byte-exact blocks |
| engineer | `gpt-5.6-sol` | medium | general implementation, escalating when the work warrants it |
| tester | `grok-4.5` | default | a different family from the engineer, on purpose |
| supervisor | `grok-4.6` | default | it decides what needs a plan at all, and it edits the harness |

The tester's model is not arbitrary. It audits the engineer's work, and two
models from the same family share the same blind spots — including the
comfortable ones.

That was a guess when it was made; the literature since supports it. Judge
models score their own output higher even when the origin is hidden, because
the mechanism is perplexity — they prefer text that reads like something they
would have written (Wataoka et al., *Self-Preference Bias in LLM-as-a-Judge*,
NeurIPS 2024, arXiv:2410.21819). The bias runs at the level of the model
*family*, not the individual checkpoint (Spiliopoulou et al., arXiv:2508.06709,
August 2025; also EMNLP 2025 main.86), which is precisely the case of a Sonnet
tester reading Sonnet code. Worse, agreement from an apparent peer flips
correct answers to incorrect ones at high rates, and the effect is pretrained
rather than an artifact of alignment (arXiv:2605.12991). What is *not*
established is a measured bug-catch delta for a cross-family tester in a
write-and-run-tests role specifically; the mechanism is evidenced, the
end-to-end ablation is not.

The designer runs at the same model and reasoning effort as the architect, on
the reasoning that design is high-stakes and the cost of being wrong there is
what matters, not which model is nominally better at the work. What a designer
actually emits is byte-exact fenced blocks — the literal strings and widths the
engineer copies — and edit fidelity is a separate dimension from reasoning
depth: Aider's architect/editor results attribute it to certain model classes
rather than uniformly to the heaviest reasoners
(`aider.chat/2024/09/26/architect.html`). If design quality does not
distinguishably benefit from the shared model/effort, the seed suggestions get
vaguer or the spec starts deferring decisions to the engineer, split it back
onto a lighter model.

The engineer-never-sees-the-test-plan isolation, the most novel thing here, has
no published ablation behind it at all. It is software-engineering intuition
about coding-to-the-test, extrapolated to models.

The round caps sit at the low end of published practice, which is the right
end: Aider retries an edit 3 times, Claude Code surfaces to the user after
about 5 self-correction rounds, OpenHands allows 30 environment steps per run.
Grogu bounces 3 times and holds at 6 pending defects. What Grogu has no
equivalent of is a **spend ceiling** — round counts bound the number of loops,
not the cost of one, and an architect doing real external research can consume
a large context per round. The harness currently sees no token or cost figures
at all, so this cannot be enforced today; it is a known gap, not a decision.
