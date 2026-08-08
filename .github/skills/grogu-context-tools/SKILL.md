---
name: grogu-context-tools
description: Explore repository/task/graph state and chain or filter Grogu tool calls without flooding context, using grogu aggregate and grogu codemode.
---

Start with `grogu aggregate <git|graph|tasks|traces|relationships|service>` for a single bounded, cacheable summary instead of raw `git status`/`git log`, listing every task, or dumping the whole knowledge graph. Use it to see what changed or what needs attention (`git` for recent activity, `traces` for recent failures, `tasks` for what's open) before deciding what to look into more deeply.

When a question needs chaining or filtering more than one of those sources together, or narrowing a large collection down to a few matching items (e.g. "which open tasks are labeled urgent"), use `grogu codemode exec` instead: write a short script that calls the same tools as plain functions (`git_summary`, `graph_context`, `tasks_summary`, `service_metadata`, `task_create`, `memory_remember`), chain/filter/combine them, and only `print()` the bounded answer. Run `grogu codemode tools` or `grogu codemode search <term>` first to see what's callable — only the printed output and a sampled run log return to the session, keeping large intermediate results out of context.

Codemode's sandbox is a guard against accidents (CPU-time/wall-clock limit), **not a security boundary** — it has no network restriction, so a script can call `requests`/`urllib` against arbitrary APIs directly.

To call configured local MCP servers (e.g. `playwright`) as plain functions, pass `--mcp`: it binds `mcp_servers()`/`mcp_tools(server)`/`mcp_call(server, tool, **kwargs)` into the script and auto-selects a compatible Python 3.10+ interpreter. This is opt-in only — it bypasses Copilot CLI's own confirmation gate for destructive tool calls, so only use it when the task genuinely needs to chain MCP tool calls, and treat it with the same caution as running arbitrary code with real credentials.

See `docs/aggregate.md` and `docs/codemode.md` for full design detail and the current roadmap.
