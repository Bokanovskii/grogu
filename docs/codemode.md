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
`codemode`, and the MCP safety caveat), see the `grogu-context-tools`
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

### Generic scripting, not just Grogu/MCP tools

Because a codemode script is plain Python with no import restriction, it is
also a reasonable way to search/filter/transform arbitrary text — grep-like
scans across many files, JSON/log parsing, regex extraction across a large
document set, or piping several tools' output together with `re`/`json`/
`pathlib` — whenever the result needs to be reduced to a small answer before
it reaches the session's context, the same motivation as chaining Grogu's
own tools. It isn't limited to the bound tool functions listed by
`grogu codemode tools`; those are just the pre-wired conveniences.

### Calling generic APIs vs. calling MCP servers

* **Generic REST/HTTP APIs** work right now, with no special support needed
  — a script just imports `requests`/`urllib` and calls out, the same as any
  Python code would. This was verified directly: a script run through
  `grogu codemode exec` successfully reached `https://api.github.com` over
  HTTPS with no additional wiring.
* **Configured MCP servers** (e.g. `playwright` in `~/.copilot/mcp-config.json`)
  are callable the same way, with no special flag: `mcp_servers()`,
  `mcp_tools(server)`, and `mcp_call(server, tool, **kwargs)` are bound into
  every sandbox automatically whenever the `mcp` package is installed (see
  Phase 2, below). This is intentionally consistent with the point above —
  codemode already lets a script reach arbitrary APIs with no opt-in, so
  gating MCP specifically behind a flag would have added a flag to remember
  without adding any actual safety.

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

`grogu codemode exec` binds three extra functions into the sandbox for
every MCP server configured in `~/.copilot/mcp-config.json` (local/stdio
servers only — remote/HTTP-type servers are out of scope for now):

```
mcp_servers()                       # -> ["playwright", ...]
mcp_tools(server)                   # -> [{"name", "description", "input_schema"}, ...]
mcp_call(server, tool, **kwargs)    # -> the tool's extracted result (str, dict, or list)
```

`grogu codemode mcp-servers` and `grogu codemode mcp-tools <server>` expose
the same listing calls outside of a sandboxed run, for discovery.

This requires the `mcp` package, which needs Python 3.10+. Grogu's own
baseline requirement is Python 3.10+ (see README.md) for exactly this
reason, so codemode runs `mcp_call` in the *same* interpreter as everything
else — no separate interpreter is located or re-exec'd. `./setup.sh`
best-effort installs `mcp` automatically when `python3` on `PATH` qualifies;
`grogu doctor` reports `python_meets_minimum`/`mcp_available` so a session
can check state directly. If `mcp` genuinely isn't installed (e.g. an
offline setup), `mcp_call`/`mcp_tools`/`mcp_servers` are simply not defined
in the sandbox and a script that calls one gets an ordinary `NameError` —
the same experience as calling any other undefined name, not a special
error path.

Each configured server gets one persistent background thread with its own
asyncio event loop and a live `ClientSession`, lazily started on the first
actual `mcp_call` and reused for the remainder of the `exec` process —
necessary because servers like `playwright` are stateful across calls (a
`browser_navigate` followed by a `browser_snapshot` needs to hit the same
browser session, not a freshly spawned one). Because the connection is
lazy, a script that never calls `mcp_call` pays no extra cost even though
the functions are always bound. The bridge is closed automatically via
`atexit` when the sandboxed script finishes.

Verified end to end against the real, configured `playwright` server: listed
its 24 tools, called `browser_navigate` then `browser_snapshot` against the
same live session, and confirmed both invalid-arguments and unknown-tool
errors surface as a normal Python `RuntimeError` with the server's own error
text. Also covered by an automated test suite (`GroguMcpTests`,
`CodemodeMcpExecTests` in `tests/test_grogu_cli.py`) against a small local
fixture MCP server (`tests/fixtures/mcp_echo_server.py`), so these tests
don't depend on any specific external server being installed. Those tests
are skipped under Grogu's own default interpreter here if it's below 3.10 or
lacks `mcp`; run `python3.11 -m pytest tests/test_grogu_cli.py -k Mcp` (or
whatever compatible interpreter is on the machine) to exercise them for real.

**Known open issue, not yet resolved:** calling an MCP tool bypasses Copilot
CLI's own per-tool confirmation gate for destructive actions entirely — a
write-capable MCP tool just executes, the same way a script calling
`requests.post(...)` against some arbitrary API already does today. No
allowlisting or extra confirmation step has been designed for either case;
this is documented here and in `AGENTS.md` as a standing risk, not a
per-call opt-in.

## Roadmap

* **Phase 1 (done).** Bind Grogu's own tools (git, knowledge graph, tasks,
  service metadata) as callable functions in a sandboxed script.
* **Phase 2 (done).** Proxy configured local MCP servers as plain functions,
  bound into every `grogu codemode exec` sandbox automatically — see above.
* **Phase 3.** If Copilot CLI itself grows native support for code-execution
  tool calling, prefer that over Grogu re-implementing an MCP-to-code proxy;
  file a feature request against `github/copilot-cli` referencing this
  pattern instead of expanding Phase 2. Separately, consider: remote/HTTP
  MCP servers (currently excluded from `load_servers()`), and a real
  confirmation/allowlist mechanism for destructive MCP tool calls made
  through codemode.

