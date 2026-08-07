# Grogu operating contract

Grogu is a thin, Copilot CLI-first harness. Preserve Copilot CLI behavior and pass through explicit model/provider choices. Never silently route work to Azure or another provider.

Prefer cached, versioned, local context over re-reading or re-indexing unchanged project history. Keep project history separate from the cross-project relationship catalog. Treat repository, project, service, environment, and session as distinct entities.

At session start, use the repository-local intelligence contract: run
`grogu memory status`, refresh with `grogu memory index` when the Git head is
stale, and use `grogu memory context` to obtain bounded context. The
`.grogu/intelligence/` directory belongs to the target repository; never store
target-repository context in the Grogu source checkout or in global user state.

Prefer `grogu aggregate <git|graph|tasks|traces|relationships|service>` over
raw `git status`/`git log`, listing every task, or dumping the whole
knowledge graph — it returns one bounded, cacheable summary per call instead
of an unbounded dump. Start here when exploring what's changed or what needs
attention (`grogu aggregate git` for recent activity, `grogu aggregate
traces` for recent failures, `grogu aggregate tasks` for what's open) before
deciding what to look into more deeply. When a question needs chaining or
filtering more than one of those sources together, or filtering a large
collection down to a few matching items (e.g. "which open tasks are labeled
urgent"), use `grogu codemode exec` instead: write a short script that calls
the same tools as plain functions (`git_summary`, `graph_context`,
`tasks_summary`, `service_metadata`, `task_create`, `memory_remember`),
chain/filter/combine them in the script, and only `print()` the bounded
answer — run `grogu codemode tools` or `grogu codemode search <term>` first
to see what's callable. This keeps large intermediate results out of
context; only the printed output and a sampled run log return to the
session. Codemode's sandbox has no network restriction of its own (only a
CPU-time/wall-clock guard) — a script can also call `requests`/`urllib`
against arbitrary APIs directly if the task calls for it, but it cannot yet
call configured MCP servers (e.g. `playwright`) as functions; that needs an
MCP client Grogu does not implement (see `docs/codemode.md`).

Before claiming or editing a task, follow the repository task-store working
agreement (`docs/tasks.md`): `grogu task gc` to forget dead leases, `grogu
task list` to see what's open and unheld, `grogu task claim <id>` to take
exactly one, `grogu task heartbeat <id>` during long work so the lease
survives, `grogu task update <id> --note "…"` to record progress, and `grogu
task release <id> --status review --note "…"` when done. `grogu task tell
<id> <text>` queues a message for whichever session holds a task; check
`grogu task inbox` at each checkpoint for messages queued for the current
session, and relay anything relevant to a running background subagent with
the in-session `write_agent` tool — that is the only way to reach a
subagent, and only the session itself can take it. This is how a second
session started in another terminal or without a worktree picks a different
task instead of racing or silently overwriting the first one.

Record and query the cross-project relationship catalog with `grogu project
{init, list, relate, graph}`, not by hand-tracking which repositories depend
on each other.

Run `grogu doctor` when the environment seems misconfigured (missing Copilot
binary, missing or unreadable `.github/AGENTS.md`, trace/catalog database
paths) before assuming a code change is required.

For coding work, explore narrowly: inspect Git state and project instructions, locate the relevant implementation and tests, trace the smallest useful call chain, edit minimally, and run targeted validation. Do not claim success without evidence.

Record useful outcomes and failures with `grogu telemetry record`. Telemetry
must be redacted, identify the repository and task when available, and include
evidence such as test results rather than secrets or full conversation text.
Improvement proposals should be backed by repeated telemetry or evaluation
evidence and land through a branch and pull request.

After verified work, preserve durable repository knowledge with
`grogu memory remember` and connect it to implementation files or related
concepts with `grogu memory link`. Prefer short architecture, decision,
convention, workflow, and service summaries over copied source or transcripts.

Personal memory about the user (relationships, preferences, goals, events,
facts, interests) is separate from repository intelligence and lives under
`grogu personal`, never inside `.grogu/intelligence/` or any repository path.
Only use `grogu personal remember` when the user has explicitly stated a fact
about themselves or explicitly asked it to be remembered. Anything observed
passively — inferred from conversation, email, or another integration —
must go through `grogu personal suggest` and stay pending until the user
runs `grogu personal confirm`; never confirm a candidate on the user's
behalf. Use `grogu personal recall` for bounded context instead of dumping
the whole personal graph into a prompt.

Use read-only operations by default. Ask for confirmation before destructive changes, external messages, sending email, deployment, spending money, or other irreversible side effects. Never expose secrets or personal data in traces.

When a web interface or browser behavior needs validation, use the configured Playwright MCP capability when available. Prefer isolated/headless checks and targeted assertions; browser access does not authorize external side effects.

Shared Grogu behavior changes belong in a branch and pull request.

To start another Grogu session that can be opened from GitHub web or mobile,
run `grogu session new`. Pass Copilot options after `--`, for example
`grogu session new -- --model gpt-5.4 --name "follow-up"`. This starts a
separate session; it does not inject text into the current one.
