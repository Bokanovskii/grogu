---
name: grogu-pipeline
description: Route substantial work through the architect, designer, engineer and tester agents using plan artifacts, stage gates, role-scoped steering and the amendment loop.
---

Substantial work goes through four roles that hand each other files, not
conversation. Plans live on disk; agents are given a plan id and a role.

## Decide whether to plan at all

```sh
grogu plan triage "<the user's request>"
```

Deterministic, and free. `direct` means answer or act now — steering,
questions, retrieval and obvious one-line changes must not burn a planning
cycle. `plan` means route through the architect before editing anything. An
explicit "just do it" from the user always wins.

## Architect

```sh
grogu plan new "<title>" --task <task-id> [--design] [--eval] [--review-required]
grogu plan brief --role architect --plan <id>
grogu plan write <id> implementation --role architect --file -
grogu plan write <id> testing        --role architect --file -
```

Always at least two plans: implementation and testing. Add `--eval` when the
change needs an end-to-end judgement of quality rather than a pass/fail on
behavior. Use `--review-required` whenever the user asked for a plan directly —
the gates then refuse work until `grogu plan approve` runs, and autopilot does
not waive that.

Add `--design` when the change has a user-visible surface; `grogu plan triage`
flags this as `design: true`.

The shape of a plan is not fixed at creation. An architect spawned onto an
existing plan changes it with `grogu plan shape <id> --add eval|design`,
`--decline eval|design --why '...'`, or `--require-review`. Only the architect
may; the other roles are refused.

Research anything external before committing to it — `web_search` for current
state, `web_fetch` for the vendor's own docs, the GitHub tools for whether a
project is still alive — and cite the source and date in the plan. Model
recall of versions, limits, pricing and deprecations goes stale silently, and a
stale recommendation reads exactly like a current one. Writing an
implementation plan that adopts something external with no citation prints a
warning.

The architect runs on the strongest model available; being wrong here is
cheapest to fix and most expensive to leave.

## Designer

```sh
grogu plan brief --role designer --plan <id>
grogu design recall --scope cli|web|ios|macos|api
grogu design template "<change>"
grogu plan write <id> design --role designer --file -
```

Only the designer writes the design stage, and the engineer *may* read it — a
test is a proxy for correctness so showing it corrupts the signal, while a
design spec is the requirement, so withholding it just guarantees the wrong
interface. The store rejects a spec that skips a required section or leans on
adjectives; give numbers, literal copy and exact output instead.

Design taste is user-scoped and cross-repository. `grogu design remember` is for
what the user said; `grogu design suggest` is for what you inferred, and it does
not apply until they confirm it.

After implementation, the designer is spawned again to look at the interface
running — screenshots of every state for a visual surface, captured output for a
terminal one — and records the verdict:

```sh
grogu plan design-review <id> --verdict pass --evidence shot-empty.png
```

The test gate stays shut until that pass exists, and a pass requires evidence.

## Engineer

```sh
grogu plan gate <id> --stage implement          # exit 3 = do not start
grogu plan show <id> --stage implementation --role engineer
grogu plan stage <id> implementation complete
```

The engineer cannot read the testing or evaluation plans, and the store refuses
if it tries. An implementation written against its own tests proves only that
the tests were satisfiable.

When the plan is wrong, the engineer does not improvise:

```sh
grogu plan amend <id> --role engineer --claim "..." --evidence "..."
```

## Tester

```sh
grogu plan gate <id> --stage test
grogu plan show <id> --stage testing --role tester
grogu plan defect <id> --role tester --route implementation|test|plan|design --report "..."
grogu plan defect <id> --role engineer --resolve d1 --note "what changed"
```

Run the tester on a different model family from the engineer; it is auditing
work, and same-family models share blind spots. Routing is the part that
matters: code wrong goes to the engineer, harness wrong stays with the tester,
plan wrong goes to the architect, and an interface that does not match the spec
goes to the designer. "Passes but the acceptance criteria are
unverifiable" is a plan defect, not a pass.

## The loops, and where they end

* **Engineer or tester → architect.** The architect must verify the claim
  against the code itself before `grogu plan resolve ... --verified`. It may
  `--accept`, `--reject`, or break a deadlock with `--guidance`.
* **Engineer ↔ tester.** After a few unproductive rounds the store escalates to
  the architect automatically and closes the gates. Repeated rounds mean the
  plan was ambiguous.
* **Architect → user.** Only when the disagreement is about intent. Everything
  else the architect settles.

## Steering

```sh
grogu plan steer "<text>" [--plan <id>] [--role engineer] [--requires-replan]
```

Notes are stored, role-scoped and sequence-numbered. Agents spawned later
receive the full standing set in `grogu plan brief`; agents already running have
unread notes appended to the output of any `grogu` command they run — as a
`grogu_notice` field under `--json` — so nobody has to remember to poll.
`--requires-replan` closes the gates until the architect folds the note in.

**Each agent is shown a note once.** Delivery acks it, so the same guidance is
never paid for twice in one context. The cost of that is real: an agent that
discards the output has lost the note. Binding notes are therefore held in
state rather than in the banner — the gate refuses and quotes the text back at
the moment it bites, which is the moment it is needed.

**Push, don't wait.** The banner only arrives when the agent happens to run
`grogu`, and an engineer mid-edit may not run one for half an hour. Nothing can
interrupt a running subagent except its spawner, so `grogu plan steer` names
which agents are running and you relay immediately with `write_agent` — by
pointing at the command, never by pasting the text. The banner is the path for
agents nobody is holding a handle to.

**When an agent should poll.** At decision points, not on a clock: before
starting a component, when about to make a choice the plan does not cover,
before completing a stage, and after a test run fails. Polling more often than
that buys latency nobody is waiting on; polling less risks working an hour past
a correction.

## Spawning an agent

Set `GROGU_ROLE`, `GROGU_PLAN` and a stable, unique `GROGU_AGENT` in the
environment of every agent you start. The role is what the gates check and what
steering is addressed to; the plan is what a bare `grogu plan status` resolves
to; the agent name binds that role across commands and fresh shells. Once an
identified agent has acted as the engineer on a plan, reusing its identity with
`--role tester` or `--role architect` is refused. Spawn the other role with its
own identity instead. An agent spawned without this context is not merely
inconvenient: it can miss steering, lose stable attribution, or receive a
missing-role refusal instead of the work it needs.

Start every pipeline role in **background mode**. The pipeline is a loop: the
engineer raises an amendment the architect has to rule on, the tester finds a
defect the engineer has to answer for. A role started in sync mode cannot be
spoken to again, so every one of those exchanges costs a fresh agent that has
to re-read the plan, the code and its own role prompt to get back to where the
last one already was. Background agents keep their context and take a
`write_agent` follow-up, which is the whole reason the loop is affordable.

Never pass `--as-user` to a subagent. It exists so that a human at the terminal
can do what a role may not, and everything here runs as the same user, so it is
the one flag that turns the gates off.

## Parallel work

```sh
grogu plan workstream <id> --name api --path 'src/api/**'
grogu plan workstreams <id> --check
```

Fan out only along declared workstreams with disjoint file sets and a clean
check. Dependent steps do not become faster by being given more agents.

## Finishing and improving

```sh
grogu plan stage <id> testing complete   # needs GROGU_ROLE or --role
grogu guard staged            # nothing private rides out on the commit
grogu plan finalize <id>      # unseal, write record.md, and stage them for the PR
grogu plan retro <id>         # what this plan cost, and what to change
grogu plan friction           # signals recurring across plans
grogu plan friction --note '<what got in the way>'   # record one
```

Completing a sealed stage and finalizing both require a declared role, because
a check that only applies to callers who identify themselves is optional to the
one role it exists to stop. Testing cannot be completed while a defect is open:
a pass recorded over a known failure is not a pass. `finalize` also scans the plans it is about to
publish and refuses to unseal one carrying a credential or personal data; fix
the plan rather than forcing it.

A defect filed after the tests passed reopens the testing stage: the pass
described a build the fix is about to replace, so the tester runs again.

Accepted amendments, escalations and user steering are all planning misses.
Fix the repository's role overlays in
`.grogu/roles/{architect,designer,engineer,tester}.md`,
or the harness itself, rather than only the plan in front of you.

See `docs/pipeline.md` for the design and the reasoning behind each constraint.
