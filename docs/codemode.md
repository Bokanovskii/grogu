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

These are two different things and only one works today:

* **Generic REST/HTTP APIs** work right now, with no special support needed
  — a script just imports `requests`/`urllib` and calls out, the same as any
  Python code would. This was verified directly: a script run through
  `grogu codemode exec` successfully reached `https://api.github.com` over
  HTTPS with no additional wiring.
* **Configured MCP servers** (e.g. `playwright`, `github-mcp-server` in
  `~/.copilot/mcp-config.json`) are **not** callable as codemode functions
  yet. Talking to them means speaking the MCP JSON-RPC 2.0 protocol over
  stdio, which needs an actual MCP client — either the `mcp` Python SDK
  (not installed in this environment) or a hand-rolled implementation.
  Tracked as Phase 2 below.

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

## Roadmap

* **Phase 1 (done).** Bind Grogu's own tools (git, knowledge graph, tasks,
  service metadata) as callable functions in a sandboxed script.
* **Phase 2.** Proxy real external MCP servers already configured for
  Copilot CLI (e.g. `~/.copilot/mcp-config.json`) so a codemode script can
  also call `github-mcp-server`, `playwright`, and similar *MCP* tools as
  plain functions the same way it already calls Grogu's own tools. Calling
  arbitrary non-MCP REST APIs already works today via plain `requests`/
  `urllib` — this phase is specifically about the MCP JSON-RPC protocol.

  Speaking MCP itself is not the hard part — verified directly: the `mcp`
  Python SDK completed a full handshake against the configured `playwright`
  server and listed its 24 tools in about fifteen lines of code. What's
  actually unsolved:

  * **Python version.** The `mcp` SDK requires Python ≥3.10; Grogu's own
    runtime is 3.8. Either Grogu's minimum version moves, or the bridge runs
    as a separate 3.10+ subprocess and talks back to the 3.8 sandbox.
  * **Async vs. sync.** The SDK is asyncio-native; `grogu codemode exec`
    runs plain synchronous scripts. Exposing an MCP tool as an ordinary
    blocking function call means bridging an event loop into that sandbox.
  * **Statefulness.** `execute()` spawns one subprocess per `exec` call.
    Playwright's server holds a live browser session across calls
    (navigate, then click, then read) — spawning a fresh MCP server per
    `exec` invocation would drop that state every time. This needs a
    persistent bridge process that outlives a single `exec` call, with its
    own lifecycle, crash recovery, and cleanup.
  * **Dynamic schema-to-function generation.** Grogu's own 6 tools are
    hand-written Python functions. MCP tools expose a live JSON Schema that
    differs per server; keeping the "call it like a plain function"
    ergonomics means generating signatures from that schema at runtime
    instead of hand-writing them.
  * **Confirmation semantics.** Copilot CLI already gates destructive tool
    calls behind per-call confirmation. Calling MCP tools directly from
    inside a sandboxed subprocess bypasses that entirely — a write-capable
    MCP tool would just execute. This needs a real design decision, not
    just a client implementation.
* **Phase 3.** If Copilot CLI itself grows native support for code-execution
  tool calling, prefer that over Grogu re-implementing an MCP-to-code proxy;
  file a feature request against `github/copilot-cli` referencing this
  pattern instead of expanding Phase 2.
