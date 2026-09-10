# Subagent governance and plan concurrency hardening — implementation plan

No design stage is needed. This is CLI/state/documentation work only.

## Workstreams

### 1. plan-state-core
Branch: `workstream/p-20260910-d88a40/plan-state-core`
Depends on: none
Paths: `src/grogu_plans.py`, `tests/test_grogu_plans.py`

Owns the plan-manifest logic:
- write preconditions for `grogu plan write` (base digest or revision compare-and-swap under the existing lock);
- clear conflict diagnostics that name the stale base, current revision, and last writer;
- per-agent last-write tracking and revocation/superseding of stale writer identities while preserving revision history and role binding;
- required-review migration from legacy scalar manifests to a list/set of required kinds, including duplicate/unknown rejection and gate enforcement for every declared kind;
- local governance state that can record elapsed/tool-call/AI-credit budgets, checkpoint deadlines, blocked completion when no checkpoint exists, and checkpoint recovery records.

### 2. plan-cli-watch
Branch: `workstream/p-20260910-d88a40/plan-cli-watch`
Depends on: `plan-state-core`
Paths: `src/grogu_cli.py`, `src/grogu_watch.py`, `tests/test_grogu_cli.py`

Owns the user-visible wiring:
- repeatable `--review` parsing and help text;
- status/gate/watch output that reports every required review kind and the current budget/checkpoint/recovery state;
- the user-facing stop guidance for runaway Task-tool agents, pointing to Copilot `/tasks` instead of implying direct process control.

### 3. agent-contracts
Branch: `workstream/p-20260910-d88a40/agent-contracts`
Depends on: `plan-cli-watch`
Paths: `.github/agents/architect.md`, `.github/agents/engineer.md`, `.github/agents/supervisor.md`, `.github/agents/tester.md`, `docs/pipeline.md`

Owns the prompt/docs contract:
- checkpoint and artifact deadlines are explicit;
- agents are told which status fields are observable;
- cancellation guidance is documented as a Copilot `/tasks` workflow, because Grogu cannot directly kill Task-tool agents.

### 4. task-store
Branch: `workstream/p-20260910-d88a40/task-store`
Depends on: none
Paths: `src/grogu_tasks.py`, `tests/test_grogu_tasks.py`, `docs/tasks.md`

Owns the repository task store boundary:
- owner/agent metadata for repo tasks and plan-local work items;
- grouped status and stale cleanup on release/close;
- an explicit boundary note that session SQL todos remain runtime-owned and are not touched here.

## Integration branch

After those PRs land, create one disposable local branch from `origin/main`:
`integration/p-20260910-d88a40`.

Merge the four PR branches into it only for combined verification, then throw it away.
