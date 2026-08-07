# Grogu operating contract

Grogu is a thin, Copilot CLI-first harness. Preserve Copilot CLI behavior and pass through explicit model/provider choices. Never silently route work to Azure or another provider.

Prefer cached, versioned, local context over re-reading or re-indexing unchanged project history. Keep project history separate from the cross-project relationship catalog. Treat repository, project, service, environment, and session as distinct entities.

At session start, use the repository-local intelligence contract: run
`grogu memory status`, refresh with `grogu memory index` when the Git head is
stale, and use `grogu memory context` to obtain bounded context. The
`.grogu/intelligence/` directory belongs to the target repository; never store
target-repository context in the Grogu source checkout or in global user state.

For coding work, explore narrowly: inspect Git state and project instructions, locate the relevant implementation and tests, trace the smallest useful call chain, edit minimally, and run targeted validation. Do not claim success without evidence.

Record useful outcomes and failures with `grogu telemetry record`. Telemetry
must be redacted, identify the repository and task when available, and include
evidence such as test results rather than secrets or full conversation text.
Improvement proposals should be backed by repeated telemetry or evaluation
evidence and land through a branch and pull request.

Use read-only operations by default. Ask for confirmation before destructive changes, external messages, sending email, deployment, spending money, or other irreversible side effects. Never expose secrets or personal data in traces.

When a web interface or browser behavior needs validation, use the configured Playwright MCP capability when available. Prefer isolated/headless checks and targeted assertions; browser access does not authorize external side effects.

When multiple sessions are active, detect related branches, worktrees, and leases before editing. Shared Grogu behavior changes belong in a branch and pull request.

To start another Grogu session that can be opened from GitHub web or mobile,
run `grogu session new`. Pass Copilot options after `--`, for example
`grogu session new -- --model gpt-5.4 --name "follow-up"`. This starts a
separate session; it does not inject text into the current one.
