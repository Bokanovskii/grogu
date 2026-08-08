# Grogu

Grogu is a thin, Copilot CLI-first developer harness. It launches the Copilot
CLI you already have, adds a small amount of shared state around it — task
tracking, traces, a project catalog, its own mark in the banner — and gets out
of the way. Copilot's interaction model, permissions and output are unchanged.

## Requirements

* [GitHub Copilot CLI](https://github.com/github/copilot-cli) on `PATH`
  (developed against 1.0.78)
* Python 3.10 or newer, and `git`
* macOS or Linux (the harness uses `flock` and POSIX signals)
* Optional: [`gh`](https://cli.github.com), for `grogu task adopt`

## Install

Nothing is compiled and no repository-relative path is assumed. Clone the
repository anywhere and run the setup script:

```sh
git clone https://github.com/Bokanovskii/grogu.git
cd grogu
./setup.sh
```

The script installs a `grogu` symlink in the first writable directory already
on `PATH`, or in `~/.local/bin` when none is available. It adds that directory
to the appropriate shell startup file when needed. Use
`./setup.sh --install-dir DIR` to choose a location or
`./setup.sh --no-path-update` to avoid changing shell configuration.

`bin/grogu` resolves symlinks before locating the repository, so the link can
live anywhere and the checkout can be moved. All paths are resolved at runtime
or supplied through environment variables.

Setup also checks that `python3` on `PATH` is 3.10+ and, if so, best-effort
installs the `mcp` package so `grogu codemode exec` can call configured MCP
servers (e.g. `playwright`) out of the box — see
[Codemode](#codemode-programmatic-tool-calling) below. Neither step is
required for the rest of Grogu; a warning is printed and setup continues if
either can't complete (e.g. offline, or `python3` is older).

`grogu doctor` prints what Grogu found and where it will write, including
`python_meets_minimum`/`mcp_available`. A non-zero exit means the Copilot CLI
or the instruction files are missing.

Setup prints green checks and red crosses for core and optional capabilities.
iMessage is optional and requires macOS Full Disk Access for the terminal
running Grogu. Gmail is optional, disabled by default, and requires
`GROGU_GMAIL_ENABLED=1` plus an OAuth access token. Missing messaging access
does not prevent Grogu from running; setup prints the exact fix and the
follow-up status command.

## Launching

```sh
grogu                       # Copilot, in autopilot mode
grogu --plan                # Copilot in plan mode; Grogu adds nothing
grogu -p "summarise HEAD"   # non-interactive prompt, mode untouched
grogu --plain               # Copilot exactly as it ships
```

**Autopilot is the default.** A normal launch becomes `copilot --autopilot
<your arguments>`. Grogu adds the flag only when the invocation is a fresh
local session and you have not already chosen a mode, because Copilot rejects
`--autopilot` alongside `--mode` or `--plan`.

| Invocation | What Grogu passes |
| --- | --- |
| `grogu` | `copilot --autopilot` |
| `grogu --model …`, `grogu --banner` | `--autopilot` prepended |
| `grogu --autopilot`, `--mode …`, `--plan` | unchanged, never duplicated |
| `grogu -i "…"`, `grogu -p "…"` | unchanged; you chose the launch mode |
| `grogu --continue`, `--resume`, `--connect` | unchanged; the session keeps its own mode |
| `grogu login`, `mcp`, `update`, `skill`, … | unchanged; subcommands start no session |
| `grogu --help`, `--version`, `--acp`, `--cloud` | unchanged |
| `grogu --plain …` | Copilot as it ships: no autopilot, no instructions, no banner |

Set `GROGU_AUTOPILOT=0` to turn the default off everywhere. Autopilot changes
the agent's *mode*, not its permissions; it never implies `--allow-all-tools`.

## Configuration boundaries

Grogu touches four places, and nothing else.

| Location | Owner | Lifetime |
| --- | --- | --- |
| `~/.grogu/` (or `$GROGU_HOME`) | Grogu | traces, the project catalog, and personal memory (`memory/`), per user |
| `~/.copilot/settings.json` | Copilot | Grogu adds `banner`, `companyAnnouncements` and — only if you have none — `statusLine`, and restores them on exit |
| `<repo>/.grogu/tasks/` | the repository | committed task records, shared through Git |
| `<repo>/.grogu/state/` | the machine | leases, inbox, lock file; ignored by Git, and self-ignoring in any repository |

`~/.grogu/memory/` (see `docs/personal-memory.md`) holds durable, user-scoped
memory about the person Grogu assists — never repository content, and never
written under any `<repo>/.grogu/` path.

Environment variables:

| Variable | Default | Effect |
| --- | --- | --- |
| `GROGU_HOME` | `~/.grogu` | Where traces, the catalog, and personal memory live |
| `GROGU_AUTOPILOT` | `1` | `0` stops Grogu adding `--autopilot` |
| `GROGU_BANNER` | `1` | `0` leaves `~/.copilot/settings.json` untouched |
| `GROGU_STATUS_LINE` | `1` | `0` installs the mark without the animated status line |
| `GROGU_TAB_COLOR` | `1` | `0` leaves the iTerm2 tab alone |
| `GROGU_ACTOR` | `$USER@$HOSTNAME` | Identity recorded on tasks and leases |
| `GROGU_AZURE` | `0` | Reserved; no provider is ever chosen for you |

Grogu also exports `GROGU_SESSION_ID`, `GROGU_SESSION_PID`, and
`GROGU_PERSONAL_MEMORY_DIR` into the Copilot environment, and appends its
`.github` directory to `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`. Existing values are
preserved.

## Messaging skills

The opt-in `imessage` and `gmail` skills provide read/search assistance and
draft-first outbound workflows. They are isolated from ordinary launches.
iMessage requires macOS and Full Disk Access. Gmail is disabled by default and
uses an access token supplied through the environment; credentials are never
stored by Grogu.

```sh
grogu imessage status
grogu imessage draft --recipient "+15551234567" --message "Hello"
grogu imessage send DRAFT_ID --confirm

GROGU_GMAIL_ENABLED=1 grogu gmail status
GROGU_GMAIL_ENABLED=1 grogu gmail search "from:billing newer_than:30d"
grogu gmail draft --to "person@example.com" --subject "Hello" --message "..."
GROGU_GMAIL_ENABLED=1 grogu gmail send DRAFT_ID --confirm
```

Sending requires explicit confirmation after showing the exact recipient and
body. Message content and credentials are excluded from telemetry.

## Remote sessions

Start a new Grogu session that is available from GitHub web and mobile with:

```sh
grogu session new
```

This starts Copilot with `--remote` and keeps Grogu's instructions, banner,
autopilot default, and explicit model choices. Additional Copilot options go
after `--`:

```sh
grogu session new -- --model gpt-5.4 --name "Grogu build session"
```

The session then appears in the GitHub Copilot app's remote-session list. The
command is also available to a running Copilot session through its shell tool,
so Grogu can start a separate remote session without replacing the current one.

## Extending it

* **Instructions** live in `.github/AGENTS.md`; they are added to Copilot's
  instruction directories, not substituted for the user's own.
* **Skills** live in `.github/skills/<name>/SKILL.md`. Add a directory, add a
  skill; nothing needs to be registered.
* **Commands** live in `src/grogu_cli.py` as a subparser plus a handler, and are
  added to `GROGU_COMMANDS` so the launcher does not forward them to Copilot.
  Any argument Grogu does not recognise belongs to Copilot.

## Tasks, issues, handoff, and history

Grogu has deliberately separate layers for local coordination, shared
repository work, and cross-machine collaboration:

| Layer | Location | Purpose |
| --- | --- | --- |
| Task record | `<repo>/.grogu/tasks/<id>.json` | Durable work item shared through Git |
| Task state | `<repo>/.grogu/state/` | Machine-local leases, locks, and inbox files |
| GitHub issue | GitHub | Cross-machine/person source for externally coordinated work |
| Task log | `log` in each task record | Append-only mutation history |
| Project catalog | `$GROGU_HOME/catalog.db` | User-local registry of named project paths |

### Repository-backed task files

`grogu task new` creates one JSON file per task under `.grogu/tasks/`. Each
record contains its title, body, status, labels, assignee, timestamps, revision,
and append-only event log. Because each task has its own file, unrelated
changes merge cleanly and task state can be reviewed in a pull request.

`claim` creates a short-lived lease under `.grogu/state/leases/`. The lease
prevents two sessions from working on the same task, expires after its TTL, and
is tied to the Grogu session process on the local machine. `heartbeat` extends
it; `release` removes it and can transition the task to `review`, `done`, or
another status. The volatile state directory is ignored and never shared
through Git.

### GitHub issue adoption

`grogu task adopt 42` uses the GitHub CLI to read issue 42 and mirror its title,
body, and labels into a local task record. The task stores the issue number, so
`grogu task show '#42'` can resolve it later. Adoption is intentionally a
local synchronization step; GitHub remains the source of truth for work that
must cross machines. It requires an authenticated `gh` installation.

### Updates to running sessions

`grogu task tell <id> "message"` appends a message to the task's local inbox.
At a checkpoint, the running session executes
`grogu task inbox <id> --consume`, reads pending messages, marks them delivered,
and relays them to its own background agents. This is pull-based because the
Copilot CLI has no supported API for a separate process to inject text into a
running local TUI.

### Project catalog

`grogu project init "Name" --path /path/to/project` writes a
`.grogu/project.json` manifest in that project and registers its name, slug,
path, and timestamps in `$GROGU_HOME/catalog.db`. `grogu project list` lists
those registrations. The catalog is user-local and is not committed to any
repository.

Stable repository identities can be related without copying repository context
into Grogu:

```sh
grogu project relate frontend backend depends-on \
  --evidence '{"source":"service configuration"}'
grogu project graph
```

Relationships are explicit, timestamped catalog records. Automatic discovery
and cross-project summary generation remain future capabilities.

## Repository intelligence

Grogu never stores another repository's context in the Grogu source checkout.
When it works in a target repository, it creates and incrementally maintains
that repository's `.grogu/intelligence/` directory:

```sh
grogu memory index
grogu memory status
grogu memory context --limit 40
grogu memory context --related --limit 20
```

The directory contains a stable repository manifest, a deterministic file
inventory, and a knowledge graph of architecture, decisions, conventions,
workflows, services, and evidence-backed learnings. Normal Grogu launches
refresh the inventory and expose the intelligence location and stable
repository identity to Copilot. Git remains Git's responsibility; Grogu does
not copy branch, commit, or history data into the repository-local index. The
machine-readable contracts live in
`schemas/repository-intelligence.schema.json`,
`schemas/project-relationship.schema.json`, and
`schemas/telemetry-event.schema.json`. See [docs/memory.md](docs/memory.md).

Use `grogu memory remember` and `grogu memory link` to add compact,
provenance-backed architecture knowledge and relationships. Use
`grogu memory context --node ... --depth ...` to traverse a bounded
neighborhood, or `--related` to pull bounded context from repositories that
are explicitly connected in the project relationship catalog.

## Bounded context aggregation

`grogu aggregate` batches, filters, and aggregates Git, the knowledge graph,
tasks, telemetry, and the relationship catalog into bounded, cacheable
summaries instead of unbounded dumps:

```sh
grogu aggregate git
grogu aggregate graph --query auth
grogu aggregate tasks --limit 20
grogu aggregate traces
grogu aggregate relationships
grogu aggregate service
```

Every operation returns the same stable envelope, including a signature
derived only from its data so identical repository state produces identical
output. See [docs/aggregate.md](docs/aggregate.md).

## Codemode: programmatic tool calling

`grogu codemode` lets a session write and run code that calls Grogu's tools
as plain functions, instead of driving one tool call at a time — the
"code execution with MCP" pattern:

```sh
grogu codemode tools                              # list every tool
grogu codemode search task                         # find tools by name/summary
grogu codemode exec --code "print(git_summary()['branch'])"
grogu codemode generate                            # write per-tool docs for discovery
```

Configured local MCP servers (e.g. `playwright`) can also be called as plain
functions, with no extra flag — `mcp_call`/`mcp_tools`/`mcp_servers` are
bound automatically whenever the `mcp` package is installed:

```sh
grogu codemode mcp-servers                         # list configured servers
grogu codemode mcp-tools playwright                # list a server's tools
grogu codemode exec --code "print(mcp_call('playwright', 'browser_navigate', url='https://example.com'))"
```

`./setup.sh` best-effort installs `mcp` as part of Grogu's Python 3.10+
baseline. If it isn't installed, `mcp_call` etc. are simply undefined and a
script that calls one gets a plain `NameError` — the same as any other
undefined name, not a special error path.

Output is truncated for the model but always logged in full under
`.grogu/state/codemode/runs/`. See [docs/codemode.md](docs/codemode.md).

## Personal memory

Separate from repository intelligence, `grogu personal` holds durable,
user-scoped memory about the person Grogu assists — relationships,
preferences, goals, events, facts, and interests — under
`GROGU_HOME/memory/`, never inside a repository:

```sh
grogu personal remember --type person --name "Jamie" --summary "Sister, lives in Denver"
grogu personal recall --query denver --limit 20
grogu personal suggest --type event --name "jamie-birthday" \
  --summary "Mentioned Jamie's birthday is in March" --source gmail --confidence 0.5
grogu personal review
grogu personal confirm <candidate-id>
```

Explicit `remember` writes are confirmed immediately. Passively observed
`suggest` candidates are queued separately and only join confirmed memory
after an explicit `confirm`; nothing is inferred and persisted silently. The
schema lives in `schemas/personal-memory.schema.json`. See
[docs/personal-memory.md](docs/personal-memory.md).

## Telemetry and self-improvement

Grogu records structured, locally stored, redacted evidence rather than
conversation transcripts:

```sh
grogu telemetry record --event verification --outcome passed \
  --repository-id "$GROGU_REPOSITORY_ID" \
  --payload '{"tests":28}'
grogu telemetry list
grogu telemetry summary
```

Telemetry is intended to support a repeatable improvement loop: observe a
failure or outcome, add a regression/evaluation case, change Grogu on a branch,
verify the result, and retain the change only when evidence improves. See
[docs/self-improvement.md](docs/self-improvement.md).

`grogu task` is a repository-backed tracker built for concurrent sessions: one
JSON file per task under `.grogu/tasks/` (committed, merge-friendly), leases
under `.grogu/state/` (never committed) that expire and die with the session
that took them, and GitHub issue templates for anything that crosses a machine.

```sh
grogu task new "Ship the autopilot default"
grogu task list
grogu task claim t-20260807-5f7320
grogu task release t-20260807-5f7320 --status review --note "PR #17"
```

See [docs/tasks.md](docs/tasks.md) for the storage and locking contract.

To hand a late update to a session that is already running:

```sh
grogu task tell t-20260807-5f7320 "the staging database was rotated"
```

The session picks it up with `grogu task inbox --consume` at its next
checkpoint and relays it to any background subagents itself. Copilot has no
supported API for a third process to push text into a running local session, so
Grogu does not pretend otherwise; see [docs/handoff.md](docs/handoff.md) for the
full protocol and for the routes that *are* supported.

## The Grogu mark

Grogu marks its sessions with a pixel-art frog child in Copilot's startup banner
and an animated one-line Grogu in the status line, plus a green iTerm2 tab
titled `Grogu`. Copilot's own startup renderer is untouched.

An animated character *inside the banner* is not possible without replacing the
Copilot UI: the banner renders once at startup and only the status line has a
refresh interval. [docs/banner.md](docs/banner.md) explains the evidence and the
alternatives.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Local state stays outside the repository, and `.scratch/` is ignored, so a test
run leaves the working tree clean.
