# Grogu

Grogu is a thin, Copilot CLI-first developer harness. It launches the Copilot
CLI you already have, adds a small amount of shared state around it — task
tracking, traces, a project catalog, its own mark in the banner — and gets out
of the way. Copilot's interaction model, permissions and output are unchanged.

## Requirements

* [GitHub Copilot CLI](https://github.com/github/copilot-cli) on `PATH`
  (developed against 1.0.78)
* Python 3.8 or newer, and `git`
* macOS or Linux (the harness uses `flock` and POSIX signals)
* Optional: [`gh`](https://cli.github.com), for `grogu task adopt`

## Install

Nothing is compiled and nothing is written outside your home directory. Clone
the repository anywhere and put the launcher on your `PATH`:

```sh
git clone https://github.com/Bokanovskii/grogu.git
cd grogu
ln -s "$PWD/bin/grogu" /usr/local/bin/grogu   # or any directory on your PATH
grogu doctor
```

`bin/grogu` resolves symlinks before locating the repository, so the link can
live anywhere and the checkout can be moved. No committed file contains an
absolute path; everything machine-specific is resolved at runtime or comes from
the environment.

`grogu doctor` prints what Grogu found and where it will write. A non-zero exit
means the Copilot CLI or the instruction files are missing.

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
| `grogu`, `grogu --model …`, `grogu --banner` | `--autopilot` prepended |
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
| `~/.grogu/` (or `$GROGU_HOME`) | Grogu | traces and the project catalog, per user |
| `~/.copilot/settings.json` | Copilot | Grogu adds `banner`, `companyAnnouncements` and — only if you have none — `statusLine`, and restores them on exit |
| `<repo>/.grogu/tasks/` | the repository | committed task records, shared through Git |
| `<repo>/.grogu/state/` | the machine | leases, inbox, lock file; ignored by Git, and self-ignoring in any repository |

Environment variables:

| Variable | Default | Effect |
| --- | --- | --- |
| `GROGU_HOME` | `~/.grogu` | Where traces and the catalog live |
| `GROGU_AUTOPILOT` | `1` | `0` stops Grogu adding `--autopilot` |
| `GROGU_BANNER` | `1` | `0` leaves `~/.copilot/settings.json` untouched |
| `GROGU_STATUS_LINE` | `1` | `0` installs the mark without the animated status line |
| `GROGU_TAB_COLOR` | `1` | `0` leaves the iTerm2 tab alone |
| `GROGU_ACTOR` | `$USER@$HOSTNAME` | Identity recorded on tasks and leases |
| `GROGU_AZURE` | `0` | Reserved; no provider is ever chosen for you |

Grogu also exports `GROGU_SESSION_ID` and `GROGU_SESSION_PID` into the Copilot
environment, and appends its `.github` directory to
`COPILOT_CUSTOM_INSTRUCTIONS_DIRS`. Existing values are preserved.

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

## Tasks, and several people at once

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
