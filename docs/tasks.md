# Task tracking

Grogu tracks work in two places on purpose, because the two have different
lifetimes and different audiences.

| Layer | Location | Committed | Audience |
| --- | --- | --- | --- |
| Issues | GitHub, from `.github/ISSUE_TEMPLATE/` | n/a | people, across machines |
| Task records | `<repo>/.grogu/tasks/<id>.json` | yes | people and sessions in the repository |
| Session state | `<repo>/.grogu/state/` | no | one machine, while sessions run |

Issues remain the source of truth for anything that crosses a machine boundary.
The repository task store is what a Grogu session can read, claim and update
without network access, and what a reviewer sees in a pull request diff.

This store is repository-owned only. Copilot session SQL todos live in the
runtime's own SQLite session state, are owned by that running Copilot session,
and Grogu must not try to cancel or delete them from the repository task
store.

For a session-facing quick reference of the claim/heartbeat/release lifecycle
and inbox relay, see the `grogu-tasks` skill
(`.github/skills/grogu-tasks/SKILL.md`); this document is the full design
write-up.

## Commands

```sh
grogu task new "Ship the autopilot default" --body "Verify the flag matrix"
grogu task adopt 42                 # mirror GitHub issue #42 into the store
grogu task list [--status active] [--mine] [--all] [--json]
grogu task show <id> [--json]       # id, id prefix, or #<issue>
grogu task claim <id> [--ttl 1800] [--force]
grogu task heartbeat <id> [--ttl 1800]
grogu task update <id> [--status …] [--note "…"] [--issue N] [--label …]
grogu task release <id> [--status review] [--note "…"]
grogu task gc                       # release leases whose session is gone
```

Every command accepts `--repo <path>`; without it Grogu uses the Git work tree
containing the current directory.

Statuses are `open`, `active`, `blocked`, `review`, `done`, `cancelled`.

## Record format

One file per task keeps Git merges trivial: two people adding tasks touch two
different files, and a status change is a small, readable diff.

```json
{
  "schema_version": 2,
  "id": "t-20260807-5f7320",
  "title": "Ship the autopilot default",
  "body": "Verify the flag matrix",
  "status": "active",
  "priority": "normal",
  "labels": [],
  "issue": 42,
  "assignee": "ana",
  "assignee_owner": "ana",
  "assignee_agent": "friction-task-store",
  "assignee_session_id": "8b6d9e4b-4f7c-4b7f-9e26-9a7a4af0a4e0",
  "created_at": "2026-08-07T00:44:53+00:00",
  "created_by": "ana",
  "created_by_owner": "ana",
  "created_by_agent": "friction-task-store",
  "updated_at": "2026-08-07T00:45:13+00:00",
  "revision": 3,
  "log": [{"at": "…", "by": "ana", "event": "claim"}]
}
```

`log` is append-only, so the record explains itself without a separate history.
`revision` increments on every mutation and gives reviewers and future tooling a
cheap conflict signal.

Grogu records owner/agent/session provenance on new or touched tasks where it
can: `created_by`, `created_by_owner`, `created_by_agent`,
`created_by_session_id`, `assignee`, `assignee_owner`, `assignee_agent`,
`assignee_session_id`, and `parent_task_id`. Legacy v1 task JSON is accepted
and upgraded in place on the next write.

When a Grogu-owned task is finalized, any stale subordinate Grogu-owned work
items that point back to it through `parent_task_id` are cancelled rather than
deleted. User-created or unrelated tasks are left alone.

## Concurrency contract

1. **One writer at a time.** Every mutation runs inside an exclusive `flock` on
   `.grogu/state/store.lock`, and each file is written to a temporary file and
   `os.replace`d into place. A reader never sees a half-written record, and two
   sessions cannot interleave a read-modify-write.
2. **Leases, not assignments.** `grogu task claim` writes
   `.grogu/state/leases/<id>.json` with the holder, host, session PID, session
   id and an expiry. Claiming a task that is already held fails unless you pass
   `--force`, which records the takeover in the task log.
3. **Leases die with their session.** `grogu` exports `GROGU_SESSION_PID` (its
   own PID) and `GROGU_SESSION_ID` into the Copilot environment. A lease taken
   inside a session is bound to that PID, so on the machine that holds it the
   lease is released the moment the session exits — no waiting for the TTL. On
   any other machine the TTL is the only signal, which is why `--ttl` exists and
   why long work should call `grogu task heartbeat`.
4. **Leases are never shared through Git.** `.grogu/state/` is ignored by this
   repository's `.gitignore` and additionally writes its own `.gitignore`
   containing `*` the first time it is created, so pointing Grogu at somebody
   else's repository cannot leak local state into their commits.
5. **Task files are shared through Git.** Commit `.grogu/tasks/` changes with the
   work they describe.

## Working agreement for sessions

```sh
grogu task gc                       # forget dead leases
grogu task list                     # what is open and unheld
grogu task claim t-2026…            # take exactly one
grogu task update t-2026… --note "found the failing test"
grogu task release t-2026… --status review --note "PR #17"
```

`.github/AGENTS.md` instructs Grogu sessions to follow the same sequence, so a
second session started in another terminal picks a different task instead of
racing the first one.
