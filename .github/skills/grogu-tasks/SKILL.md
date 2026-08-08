---
name: grogu-tasks
description: Claim, work, and release tasks using Grogu's lease system so concurrent sessions never race or silently overwrite each other's work.
---

Before claiming or editing a task, follow the repository task-store working agreement (`docs/tasks.md`):

1. `grogu task gc` — forget dead leases from crashed/abandoned sessions.
2. `grogu task list` — see what's open and unheld.
3. `grogu task claim <id>` — take exactly one task.
4. `grogu task heartbeat <id>` — run during long work so the lease survives.
5. `grogu task update <id> --note "…"` — record progress as you go.
6. `grogu task release <id> --status review --note "…"` — release when done.

`grogu task tell <id> <text>` queues a message for whichever session holds a task. Check `grogu task inbox` at each checkpoint for messages queued for the current session, and relay anything relevant to a running background subagent with the in-session `write_agent` tool — that is the only way to reach a subagent, and only the session itself can take it.

This is how a second session started in another terminal, or without a worktree, picks a different task instead of racing or silently overwriting the first one.
