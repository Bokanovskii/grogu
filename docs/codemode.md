# Codemode: programmatic tool calling

`grogu codemode` lets a session write and run code that calls Grogu's tools
directly, instead of the model driving one tool call at a time through the
usual request/response loop. This is the pattern Anthropic describes in
["Code execution with MCP"](https://www.anthropic.com/engineering/code-execution-with-mcp)
(and that Cloudflare calls "Code Mode"): present tools as plain, callable
functions in code, let the agent write a short script that calls several of
them, filters/aggregates/loops over the results in the execution
environment, and only return what the script explicitly prints or logs.
Intermediate results — a large `git log`, every task record, a big graph
traversal — never have to pass back through the model one call at a time.

This is a different feature from `grogu aggregate` (see `docs/aggregate.md`),
which returns one bounded, pre-shaped summary per call. `codemode` is for
when a session needs to combine or filter several of Grogu's data sources
with real control flow (loops, conditionals, intermediate variables) before
deciding what's worth returning.

For a session-facing quick reference (when to use `aggregate` vs.
`codemode`, and the `--mcp` safety caveat), see the `grogu-context-tools`
skill (`.github/skills/grogu-context-tools/SKILL.md`); this document is the
full design write-up.

## Why this instead of one tool call per step

* **Progressive disclosure.** `grogu codemode tools`/`search` return short
  name+signature+summary triples, and `grogu codemode generate` writes one
  documentation file per tool so a session can `ls`/`read` only the tools it
  actually needs, instead of loading every tool definition up front.
* **Context-efficient results.** A script can call `tasks_summary()`,
  `git_summary()`, and `graph_context()`, combine and filter the results in
  Python, and `print()` only the few lines that matter. The full output is
  still written to a run log (see below) in case more detail is needed.
* **Real control flow.** Loops and conditionals over tool results run in the
  execution environment, not as a chain of separate tool calls each round-
  tripping through the model.

## Tools (Phase 1: Grogu's own capabilities)

`grogu_codemode.py` currently binds Grogu's own repository-local
capabilities as callable functions: `git_summary`, `graph_context`,
`tasks_summary`, `service_metadata`, `task_create`, and `memory_remember`
(the first four reuse `grogu_context.py`/`grogu_memory.py`/`grogu_tasks.py`
directly; the latter two also mutate repository state). List them and their
signatures with:

```sh
grogu codemode tools
grogu codemode search task
```

## Running code

```sh
grogu codemode exec --code "print(git_summary()['branch'])"
grogu codemode exec --file ./script.py
echo "print(tasks_summary())" | grogu codemode exec
```

Every tool is bound as a plain function taking the same keyword arguments as
its `grogu_codemode.TOOLS` signature — no client object, no JSON-RPC
envelope. `--repo` targets a repository other than the current working
tree; `--timeout` bounds wall-clock time (default 20s, capped at 120s).

Execution is sandboxed with a subprocess and a CPU-time resource limit; this
is a guard against accidental runaway scripts, **not a security boundary**.
In particular, there is **no network restriction**: a script can `import
requests`/`urllib` and call any API the host machine can reach, exactly like
any other Python process. It can also import anything installed in this
environment. Do not run untrusted code with `grogu codemode exec`.

### Calling generic APIs vs. calling MCP servers

* **Generic REST/HTTP APIs** work right now, with no special support needed
  — a script just imports `requests`/`urllib` and calls out, the same as any
  Python code would. This was verified directly: a script run through
  `grogu codemode exec` successfully reached `https://api.github.com` over
  HTTPS with no additional wiring.
* **Configured MCP servers** (e.g. `playwright` in `~/.copilot/mcp-config.json`)
  are callable as plain functions via `grogu codemode exec --mcp` (Phase 2,
  below) — `mcp_servers()`, `mcp_tools(server)`, and
  `mcp_call(server, tool, **kwargs)` are bound into the sandbox alongside
  Grogu's own tools when the flag is passed. This is opt-in, not default,
  because it bypasses Copilot CLI's own confirmation gate for destructive
  actions (see the Phase 2 write-up below).

### Output handling

`stdout`/`stderr` are truncated to `MAX_OUTPUT_BYTES` (4000 bytes) in the
returned JSON, so a chatty script can't flood the model's context. The full,
untruncated output is always written to a run log under
`.grogu/state/codemode/runs/<run_id>.log`, so a session can go read more if
the truncated summary isn't enough, without losing the rest of the output.

## Tool discovery files

`grogu codemode generate` writes one file per tool under
`.grogu/state/codemode/tools/` describing its signature and summary, so a
session can discover what's available by listing a directory rather than
loading every tool definition into context at once. These files are
documentation for discovery, not directly importable bindings — the actual
callable functions are injected into the sandbox by `grogu codemode exec`
itself. This directory lives under `.grogu/state/`, alongside other
per-machine, regeneratable Grogu state, and is never committed.

## MCP servers (Phase 2)

`grogu codemode exec --mcp` binds three extra functions into the sandbox for
every MCP server configured in `~/.copilot/mcp-config.json` (local/stdio
servers only — remote/HTTP-type servers are out of scope for now):

```
mcp_servers()                       # -> ["playwright", ...]
mcp_tools(server)                   # -> [{"name", "description", "input_schema"}, ...]
mcp_call(server, tool, **kwargs)    # -> the tool's extracted result (str, dict, or list)
```

`grogu codemode mcp-servers` and `grogu codemode mcp-tools <server>` expose
the same listing calls outside of a sandboxed run, for discovery.

This requires a Python 3.10+ interpreter with the `mcp` package installed
somewhere on the machine — `mcp` itself only imports on 3.10+, and Grogu's
own baseline stays on whatever Python it always used. `grogu_mcp.
find_compatible_python()` locates one automatically (preferring the
interpreter already running `grogu codemode exec`, falling back to
`python3.11`/`.12`/`.13` on `PATH`) and `execute()` re-execs the sandboxed
script under it only when `--mcp` is passed; without the flag, nothing
changes. If no compatible interpreter is found, `exec --mcp` fails with a
clear error rather than a cryptic import failure.

Each configured server gets one persistent background thread with its own
asyncio event loop and a live `ClientSession`, lazily started on first use
and reused for the remainder of the `exec` process — necessary because
servers like `playwright` are stateful across calls (a `browser_navigate`
followed by a `browser_snapshot` needs to hit the same browser session, not
a freshly spawned one). The bridge is closed automatically via `atexit` when
the sandboxed script finishes.

Verified end to end against the real, configured `playwright` server: listed
its 24 tools, called `browser_navigate` then `browser_snapshot` against the
same live session, and confirmed both invalid-arguments and unknown-tool
errors surface as a normal Python `RuntimeError` with the server's own error
text. Also covered by an automated test suite (`GroguMcpTests`,
`CodemodeMcpExecTests` in `tests/test_grogu_cli.py`) against a small local
fixture MCP server (`tests/fixtures/mcp_echo_server.py`), so these tests
don't depend on any specific external server being installed. Those tests —
and `--mcp` itself — are skipped/unavailable under Grogu's default
interpreter if it's below 3.10 or lacks `mcp`; run
`python3.11 -m pytest tests/test_grogu_cli.py -k Mcp` (or whatever compatible
interpreter is on the machine) to exercise them for real.

**Known open issue, not yet resolved:** calling an MCP tool via `--mcp`
bypasses Copilot CLI's own per-tool confirmation gate for destructive
actions entirely — a write-capable MCP tool just executes. The only
mitigation right now is that `--mcp` is opt-in and the risk is documented
here, in `AGENTS.md`, and in the CLI's own `--help` text. No allowlisting or
extra confirmation step has been designed yet; treat scripts using `--mcp`
with the same caution as running arbitrary code with real credentials.

## Roadmap

* **Phase 1 (done).** Bind Grogu's own tools (git, knowledge graph, tasks,
  service metadata) as callable functions in a sandboxed script.
* **Phase 2 (done).** Proxy configured local MCP servers as plain functions
  via `grogu codemode exec --mcp` — see above.
* **Phase 3.** If Copilot CLI itself grows native support for code-execution
  tool calling, prefer that over Grogu re-implementing an MCP-to-code proxy;
  file a feature request against `github/copilot-cli` referencing this
  pattern instead of expanding Phase 2. Separately, consider: remote/HTTP
  MCP servers (currently excluded from `load_servers()`), and a real
  confirmation/allowlist mechanism for destructive MCP tool calls made
  through codemode.

