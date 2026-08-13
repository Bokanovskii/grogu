# Grogu operating contract

Grogu is a thin, Copilot CLI-first harness. Preserve Copilot CLI behavior and pass through explicit model/provider choices. Never silently route work to Azure or another provider.

Prefer cached, versioned, local context over re-reading or re-indexing unchanged project history. Keep project history separate from the cross-project relationship catalog. Treat repository, project, service, environment, and session as distinct entities.

At session start, use the repository-local intelligence contract: run
`grogu memory status`, refresh with `grogu memory index` when the Git head is
stale, and use `grogu memory context` to obtain bounded context. The
`.grogu/intelligence/` directory belongs to the target repository; never store
target-repository context in the Grogu source checkout or in global user state.

Prefer `grogu aggregate <git|graph|tasks|traces|relationships|service>` for a
bounded, cacheable summary over raw `git status`/`git log`, listing every
task, or dumping the whole knowledge graph, and prefer `grogu codemode exec`
when a question needs chaining or filtering more than one of those sources
together (invoke the `grogu-context-tools` skill for the full walkthrough,
including calling configured MCP servers like `playwright` as plain
functions and its safety caveats).

Before claiming or editing a task, follow the repository task-store working
agreement so concurrent sessions never race or silently overwrite each
other's work (invoke the `grogu-tasks` skill for the claim/heartbeat/release
lifecycle and inbox relay steps).

Record and query the cross-project relationship catalog with `grogu project
{init, list, relate, graph}`, not by hand-tracking which repositories depend
on each other.

Run `grogu doctor` when the environment seems misconfigured (missing Copilot
binary, missing or unreadable `.github/AGENTS.md`, trace/catalog database
paths) before assuming a code change is required.

At the start of every launch, Grogu fast-forwards the primary checkout's
clean `main` to `origin/main` (never switching branches or discarding
work), then checks for stale self-modification worktrees (branch merged
into `main`, remote branch deleted, or pull request merged per `gh`) and
removes any with no uncommitted changes; `grogu doctor` reports whether
`main` is behind `origin/main` and any stale worktrees still standing. Use
`grogu worktree list` / `grogu worktree prune [--dry-run]` to inspect or
clean them up by hand, `GROGU_SYNC_MAIN=0` to disable the automatic main
sync, and `GROGU_PRUNE_WORKTREES=0` to disable the automatic worktree
check.

For coding work, explore narrowly: inspect Git state and project instructions, locate the relevant implementation and tests, trace the smallest useful call chain, edit minimally, and run targeted validation. Do not claim success without evidence.

When a session's work spans several unrelated concerns, split it into one
single-purpose pull request per concern, each in its own worktree, and merge
them all into one local, never-pushed integration branch to build and test the
combined result (invoke the `split-prs-integrate` skill for the branch,
re-merge, and cleanup steps). Review stays small; testing stays single-pass.
Fix review feedback on the branch that owns the change, never on the
integration branch.

When asked to simplify or remove something (an unnecessary dependency, a workaround, a flag), first map every place it touches — call sites, tests, CLI flags/help text, docs, related modules — in one pass before editing anything. Fixing the first occurrence and moving on leaves the rest inconsistent; a later pass over the same ground wastes a full iteration the first pass could have caught.

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

Shared Grogu behavior changes belong in a branch and pull request. When Grogu
modifies its own source checkout, do the work in a dedicated `git worktree`
(e.g. `git worktree add ../grogu-worktrees/<branch> -b <branch> main`), never
by checking out a feature branch directly in the primary checkout. The
primary checkout is the target that installed `grogu` launchers resolve to;
it must always stay on `main` and reflect `origin/main`, which every launch
now enforces automatically (fast-forward via `git fetch origin && git pull
--ff-only origin main`, skipped if the checkout is dirty or not on `main`);
run that sequence by hand if you need it sooner than the next launch. Remove
the worktree (`git worktree remove <path>`) once its pull request merges or
is abandoned.

To start another Grogu session that can be opened from GitHub web or mobile,
run `grogu session new`. Pass Copilot options after `--`, for example
`grogu session new -- --model gpt-5.4 --name "follow-up"`. This starts a
separate session; it does not inject text into the current one.
