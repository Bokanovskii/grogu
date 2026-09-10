# Subagent governance and plan concurrency hardening — testing plan

The tests are split by workstream and run independently before the combined integration branch. The testing plan is sealed because the engineer must not see the cases in advance.

## 1) plan-state-core branch tests
Branch under test: `workstream/p-20260910-d88a40/plan-state-core`

Run the new or updated `tests/test_grogu_plans.py` cases that cover:
- backward compatibility for legacy manifests: scalar review fields still load, older records without the new budget/checkpoint fields still summarise, and preserved role bindings still identify the writer;
- concurrency races: two writers with the same stale base digest or revision do not clobber each other, the loser gets a clear compare-and-swap diagnostic that names the base, current revision, and last writer, and the surviving write keeps the previous revision history intact;
- stale writer revocation: once an agent identity is superseded, later writes from that identity are refused or rebound according to the new binding rules, without losing the preserved revision trail;
- required-review migration: a manifest that used the legacy scalar review field still upgrades to the new required-review set/list, duplicate kinds and unknown kinds are rejected, and every declared kind is required before the gates open;
- governance state: elapsed/tool-call/AI-credit budget metadata is recorded when observable, completion is blocked when no checkpoint or artifact deadline has been satisfied, and a checkpoint/recovery record unblocks the plan only after the recovery state is written.

Failure recovery must be exercised with a deliberate interruption or simulated crash so the test proves the recovery record survives a rerun and the plan does not silently advance past a missing checkpoint.

## 2) plan-cli-watch branch tests
Branch under test: `workstream/p-20260910-d88a40/plan-cli-watch`

Run the CLI/watch tests that verify:
- `grogu plan workstream` accepts repeatable `--review` flags and rejects duplicates or unknown review kinds;
- `grogu plan status`, `grogu plan gate`, and `grogu watch` surface every declared required review kind, the current budget/checkpoint/recovery state, and any stale-write conflict in the same wording that the core emitted;
- the stop guidance for a runaway Task-tool agent points to Copilot `/tasks` and never claims Grogu can directly cancel the Task-tool process.

The tests should assert the rendered output, not just the return code, so that missing status text or misleading cancellation text fails loudly.

## 3) agent-contracts branch tests
Branch under test: `workstream/p-20260910-d88a40/agent-contracts`

Run text-focused assertions over `.github/agents/*.md` and `docs/pipeline.md` to confirm:
- checkpoints and artifact deadlines are explicit;
- the observable budget/status vocabulary matches the CLI/watch output;
- the immediate cancellation workflow is the Copilot `/tasks` path, and the docs do not promise Grogu-level process cancellation it cannot actually perform.

## 4) task-store branch tests
Branch under test: `workstream/p-20260910-d88a40/task-store`

Run the task-store tests that verify:
- old `.grogu/tasks/*.json` records still load cleanly when owner/agent metadata is absent;
- new owner/agent metadata is recorded on claim/update/release and can be grouped by status without losing the underlying task history;
- stale ownership state is cleared on release/close/final state transitions while completed history remains available for review;
- nothing in the change touches the session SQL todo tables, because those are runtime-owned and out of scope for this repository.

## 5) integration branch tests
Integration branch: `integration/p-20260910-d88a40`

After merging all four PR branches into the disposable integration branch from `origin/main`, rerun the branch-specific tests together plus a combined smoke pass that covers:
- plan creation;
- write with a base precondition;
- multiple required reviews;
- a blocked checkpoint or deadline;
- recovery and release/cleanup;
- the CLI/watch/status wording that ties the whole thing together.

This combined pass must include at least one concurrency race reproduction and one failure-recovery reproduction so the integrated result proves the new state machine is stable when the branches interact.
