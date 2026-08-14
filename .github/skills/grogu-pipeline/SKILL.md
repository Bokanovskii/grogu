---
name: grogu-pipeline
description: Route substantial work through the architect, engineer and tester agents using plan artifacts, stage gates, role-scoped steering and the amendment loop.
---

Substantial work goes through three roles that hand each other files, not
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
grogu plan new "<title>" --task <task-id> [--eval] [--review-required]
grogu plan brief --role architect --plan <id>
grogu plan write <id> implementation --role architect --file -
grogu plan write <id> testing        --role architect --file -
```

Always at least two plans: implementation and testing. Add `--eval` when the
change needs an end-to-end judgement of quality rather than a pass/fail on
behavior. Use `--review-required` whenever the user asked for a plan directly —
the gates then refuse work until `grogu plan approve` runs, and autopilot does
not waive that.

The architect runs on the strongest model available; being wrong here is
cheapest to fix and most expensive to leave.

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
grogu plan defect <id> --role tester --route implementation|test|plan --report "..."
```

Run the tester on a different model family from the engineer; it is auditing
work, and same-family models share blind spots. Routing is the part that
matters: code wrong goes to the engineer, harness wrong stays with the tester,
plan wrong goes to the architect. "Passes but the acceptance criteria are
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
unread notes appended to the output of any `grogu` command they run, so nobody
has to remember to poll. `--requires-replan` closes the gates until the
architect folds the note in.

Relay to a running subagent with `write_agent` by pointing at the command, never
by pasting the text — one source of truth.

## Parallel work

```sh
grogu plan workstream <id> --name api --path 'src/api/**'
grogu plan workstreams <id> --check
```

Fan out only along declared workstreams with disjoint file sets and a clean
check. Dependent steps do not become faster by being given more agents.

## Finishing and improving

```sh
grogu plan stage <id> testing complete
grogu plan finalize <id>      # unseal every stage so the PR carries the plans
grogu plan retro <id>         # what this plan cost, and what to change
grogu plan friction           # signals recurring across plans
```

Accepted amendments, escalations and user steering are all planning misses.
Fix the repository's role overlays in `.grogu/roles/{architect,engineer,tester}.md`,
or the harness itself, rather than only the plan in front of you.

See `docs/pipeline.md` for the design and the reasoning behind each constraint.
