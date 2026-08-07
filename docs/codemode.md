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
Do not run untrusted code with `grogu codemode exec`.

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
  also call `github-mcp-server`, `playwright`, and similar tools as plain
  functions. This needs Grogu to act as an MCP client to those servers,
  either via the `mcp` Python SDK or a hand-rolled JSON-RPC 2.0 stdio
  client — neither is wired up yet.
* **Phase 3.** If Copilot CLI itself grows native support for code-execution
  tool calling, prefer that over Grogu re-implementing an MCP-to-code proxy;
  file a feature request against `github/copilot-cli` referencing this
  pattern instead of expanding Phase 2.
