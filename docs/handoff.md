# Forwarding an update to a running session

The question this answers: a Grogu session is already running, possibly with
background subagents, and you learn something it needs to know. How does the
update reach it without restarting the work?

## What Copilot CLI actually supports

Verified against Copilot CLI 1.0.78 (`copilot --help`, and the option table in
the shipped bundle):

| Path | Direction | Supported? |
| --- | --- | --- |
| Typing in the session's own terminal | you → session | yes, and it is the fastest route when you are at that terminal |
| `--remote` / `--remote-export` | you → your session, from GitHub web and mobile | yes, documented; opt in at launch |
| `write_agent` tool | session → its own background subagents | yes, but only the model can call it, from inside the session |
| `--connect [sessionId]` | you → a *remote* session | yes, for remote sessions only |
| `grogu session new` | current session → a new remote Grogu session | yes; starts a separate Copilot process |
| `--ui-server`, `--headless`, `--server`, `--managed-server` | external process → running session over JSON-RPC | present but `hideHelp()`, undocumented and unstable — Grogu does not use them |
| Anything else that would push text into a running local TUI | — | does not exist |

There is no supported API for a third process to inject a message into a
running local interactive session, and Grogu does not fake one. It does not
write to the session's terminal, drive it with a pseudo-terminal, or call the
hidden JSON-RPC server. A wrapper that typed into somebody's TUI would race the
user's own keystrokes and would break on any Copilot UI change.

When a session needs a separate remotely accessible workspace, it can run
`grogu session new`. Grogu adds `--remote` unless an explicit remote setting
was supplied, then launches the normal Grogu wrapper in a child process. The
new session is independent and appears in the GitHub web/mobile remote-session
list; it does not receive messages from the parent session automatically.

Editing `companyAnnouncements` in `~/.copilot/settings.json` mid-session does
produce a visible `Company announcement:` notification in a running session,
because Copilot watches that file. It is still the wrong tool: it is a UI
notification, the text never enters the agent's context, and Grogu already owns
those keys for the banner.

## The protocol Grogu implements

A file-backed inbox that the session pulls, instead of a push nobody supports.

```sh
# You, in any other terminal:
grogu task tell t-20260807-5f7320 "the staging database was rotated, use STAGING_URL_2"

# The running session, at its next checkpoint:
grogu task inbox --json                     # anything waiting, for any task?
grogu task inbox t-20260807-5f7320 --consume
```

Messages append to `.grogu/state/inbox/<task-id>.jsonl` under the same
exclusive lock as every other store mutation, so a `tell` never corrupts a
concurrent read. `--consume` stamps `delivered_at` instead of deleting, so the
exchange stays auditable, and `grogu task inbox <id> --all` replays it.

`.github/AGENTS.md` tells sessions to check the inbox at each checkpoint and,
when they are running background subagents, to relay the update with the
in-session `write_agent` tool. That is the only step that can reach a subagent,
and only the session itself can take it — which is exactly why the protocol is
pull-based.

Practical guarantees and limits:

* **Delivery is at the next checkpoint**, not instant. A session deep in a long
  tool call sees the update when that call finishes.
* **The inbox is per machine.** `.grogu/state/` is never committed. To reach a
  session on another machine, comment on the GitHub issue, or launch that
  session with `--remote` and use GitHub web or mobile.
* **Nothing is auto-executed.** A queued update is context for the agent, not a
  command that bypasses the permission model.

## When you want the update to be blocking

Queue it *and* say so where the session cannot miss it: `grogu task update <id>
--status blocked --note "waiting on rotated credentials"`. The next session that
runs `grogu task list` sees the block even if the original session has exited.
