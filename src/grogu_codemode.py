"""Codemode: generate code bindings for Grogu's tools and run agent code
against them in a bounded sandbox.

This implements the "code execution with MCP" pattern (Anthropic, Nov 2025;
Cloudflare calls it "Code Mode"): instead of calling tools one at a time and
passing every intermediate result back through the model, the agent writes a
short script that calls tools directly and filters/aggregates the results in
code. Only the script's own bounded output returns to the model; the full
result is always available on disk if the agent needs to look closer.

Two things make this different from ``grogu aggregate``:

* ``grogu aggregate`` *is* a fixed set of bounded operations, invoked one at a
  time from the CLI. Codemode instead exposes those (and other) capabilities
  as plain importable Python functions and lets the agent combine, loop over,
  and filter them in one execution — the tool-composition and control-flow
  benefits the pattern is about.
* Tool definitions are written to a file tree (one file per tool) so an agent
  can discover what is available by listing a directory and read only the
  tools it needs, instead of every tool description being loaded up front.

Scope: v1 binds Grogu's own repository-local capabilities (Git, the
knowledge graph, tasks). Proxying arbitrary external MCP servers through the
same sandbox is tracked separately (see the codemode design issue) because it
requires Grogu to act as both an MCP client and an MCP server.
"""

from __future__ import annotations

import datetime as dt
import json
import resource
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Optional

import grogu_context
import grogu_mcp
import grogu_memory
import grogu_tasks

TOOLS_DIRNAME = ".grogu/state/codemode/tools"
RUNS_DIRNAME = ".grogu/state/codemode/runs"
MAX_OUTPUT_BYTES = 4000
DEFAULT_TIMEOUT_SECONDS = 20
MAX_TIMEOUT_SECONDS = 120


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


# Each tool is a small, pure-ish function taking a resolved repository root
# (and any extra keyword arguments the agent supplies) and returning a JSON-
# serializable value. ``summary`` is the one-line description shown by
# ``search``/``list`` for progressive disclosure; the generated file's own
# docstring carries the full description.
def _tool_git_summary(root: Path, **_: object) -> dict:
    return grogu_context.git_summary(root)


def _tool_tasks_summary(root: Path, limit: int = 20, **_: object) -> dict:
    return grogu_context.tasks_summary(root, limit=limit)


def _tool_service_metadata(root: Path, **_: object) -> dict:
    return grogu_context.service_metadata(root)


def _tool_graph_context(
    root: Path,
    query: str = "",
    node_id: str = "",
    depth: int = 1,
    limit: int = 40,
    **_: object,
) -> dict:
    store = grogu_memory.MemoryStore(root)
    return grogu_context.graph_context(
        store, query=query, node_id=node_id, depth=depth, limit=limit
    )


def _tool_task_create(
    root: Path,
    title: str,
    body: str = "",
    labels: Optional[list] = None,
    priority: str = "normal",
    **_: object,
) -> dict:
    return grogu_tasks.TaskStore(root).create(
        title, body=body, labels=labels, priority=priority
    )


def _tool_memory_remember(
    root: Path,
    node_type: str,
    name: str,
    summary: str,
    paths: Optional[list] = None,
    tags: Optional[list] = None,
    **_: object,
) -> dict:
    return grogu_memory.MemoryStore(root).remember(
        node_type, name, summary, paths=paths, tags=tags,
        provenance={"kind": "codemode"},
    )


TOOLS = {
    "git_summary": {
        "function": _tool_git_summary,
        "summary": "Branch, upstream drift, and changed-file counts by role.",
        "signature": "git_summary() -> dict",
    },
    "tasks_summary": {
        "function": _tool_tasks_summary,
        "summary": "Task counts by status/label and the most recently updated tasks.",
        "signature": "tasks_summary(limit: int = 20) -> dict",
    },
    "service_metadata": {
        "function": _tool_service_metadata,
        "summary": "Name/version/description from top-level manifests only.",
        "signature": "service_metadata() -> dict",
    },
    "graph_context": {
        "function": _tool_graph_context,
        "summary": "Bounded knowledge-graph neighborhood (query, node, or full bounded list).",
        "signature": (
            "graph_context(query: str = '', node_id: str = '', depth: int = 1, "
            "limit: int = 40) -> dict"
        ),
    },
    "task_create": {
        "function": _tool_task_create,
        "summary": "Create a repository task record.",
        "signature": (
            "task_create(title: str, body: str = '', labels: list = None, "
            "priority: str = 'normal') -> dict"
        ),
    },
    "memory_remember": {
        "function": _tool_memory_remember,
        "summary": "Add a compact, provenance-backed knowledge-graph node.",
        "signature": (
            "memory_remember(node_type: str, name: str, summary: str, "
            "paths: list = None, tags: list = None) -> dict"
        ),
    },
}


def list_tools() -> list:
    """Bounded name + one-line summary for every tool (progressive disclosure)."""
    return [
        {"name": name, "summary": spec["summary"], "signature": spec["signature"]}
        for name, spec in sorted(TOOLS.items())
    ]


def search_tools(query: str) -> list:
    """Bounded, case-insensitive name/summary search over the tool list."""
    needle = query.lower()
    return [
        tool
        for tool in list_tools()
        if needle in tool["name"].lower() or needle in tool["summary"].lower()
    ]


def _tool_file_source(name: str, spec: dict) -> str:
    return textwrap.dedent(
        f'''\
        """{spec["summary"]}

        Call as: {spec["signature"]}
        Invoke through ``grogu codemode exec`` — this file documents the tool's
        interface for discovery, but the sandbox binds it at run time.
        """
        '''
    )


def generate_tool_tree(root: Path) -> Path:
    """Write one documentation file per tool under ``.grogu/state/codemode/tools/``.

    An agent discovers tools by listing this directory and reading only the
    files it needs, rather than every tool definition being loaded up front.
    """
    directory = root / TOOLS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    for name, spec in TOOLS.items():
        (directory / f"{name}.py").write_text(_tool_file_source(name, spec), encoding="utf8")
    return directory


def _limit_resources() -> None:
    # Best-effort caps so a runaway script cannot exhaust the host; this is
    # not a security boundary, only a guard against accidents.
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (MAX_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS))
    except (ValueError, OSError):
        pass


def _bootstrap_source(root: Path, src_dir: Path) -> str:
    tool_calls = "\n".join(
        f"def {name}(*args, **kwargs):\n"
        f"    return _TOOLS[{name!r}][\"function\"](_ROOT, *args, **kwargs)\n"
        for name in TOOLS
    )
    mcp_bootstrap = ""
    if grogu_mcp.available():
        mcp_bootstrap = textwrap.dedent(
            """
            import atexit as _atexit
            import grogu_mcp as _grogu_mcp

            def mcp_servers():
                return _grogu_mcp.list_servers()

            def mcp_tools(server):
                return _grogu_mcp.list_tools(server)

            def mcp_call(server, tool, **kwargs):
                return _grogu_mcp.call_tool(server, tool, **kwargs)

            _atexit.register(_grogu_mcp.close_all)
            """
        )
    template = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, {src_dir!r})
        from pathlib import Path as _Path
        import grogu_codemode as _codemode

        _ROOT = _Path({root!r})
        _TOOLS = _codemode.TOOLS

        {tool_calls}
        {mcp_bootstrap}
        """
    )
    return template.format(
        src_dir=str(src_dir), root=str(root), tool_calls=tool_calls, mcp_bootstrap=mcp_bootstrap
    )


def execute(
    root: Path,
    code: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Run agent-written code with every tool bound as a plain function call.

    Only ``stdout``/``stderr``, truncated to ``MAX_OUTPUT_BYTES``, return to
    the caller. The full, untruncated output is always written to a run log
    under ``.grogu/state/codemode/runs/`` so a session can read more if the
    truncated summary is not enough — keeping large intermediate results out
    of the model's context by default, without losing them.

    If the ``mcp`` package is available (installed by ``setup.sh`` as part of
    Grogu's Python 3.10+ baseline), the script also gets ``mcp_servers()``/
    ``mcp_tools(server)``/``mcp_call(server, tool, **kwargs)`` for calling
    configured MCP servers directly — no separate flag needed, the same way a
    script can already call `requests`/`urllib` against arbitrary APIs with
    no special opt-in. Each server only connects lazily, on its first actual
    ``mcp_call``, so scripts that never touch MCP pay no extra cost. If
    ``mcp`` isn't installed, ``mcp_call`` etc. simply aren't defined and a
    script that tries to use them fails with an ordinary ``NameError``, the
    same as calling any other undefined name.
    """
    root = Path(root).expanduser().resolve()
    src_dir = Path(__file__).resolve().parent
    bounded_timeout = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS))
    run_id = uuid.uuid4().hex[:12]

    script = _bootstrap_source(root, src_dir) + "\n\n" + code
    runs_dir = root / RUNS_DIRNAME
    runs_dir.mkdir(parents=True, exist_ok=True)
    log_path = runs_dir / f"{run_id}.log"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=bounded_timeout,
            preexec_fn=_limit_resources if sys.platform != "win32" else None,
        )
        timed_out = False
        stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        stdout = (error.stdout or "") if isinstance(error.stdout, str) else ""
        stderr = (error.stderr or "") if isinstance(error.stderr, str) else ""
        returncode = None
    log_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "started_at": now(),
                "returncode": returncode,
                "timed_out": timed_out,
                "stdout": stdout,
                "stderr": stderr,
            },
            indent=2,
        )
        + "\n",
        encoding="utf8",
    )

    def _bounded(text: str) -> tuple:
        encoded = text.encode("utf8")
        if len(encoded) <= MAX_OUTPUT_BYTES:
            return text, False
        return encoded[:MAX_OUTPUT_BYTES].decode("utf8", "ignore"), True

    stdout_bounded, stdout_truncated = _bounded(stdout)
    stderr_bounded, stderr_truncated = _bounded(stderr)
    return {
        "run_id": run_id,
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout": stdout_bounded,
        "stdout_truncated": stdout_truncated,
        "stderr": stderr_bounded,
        "stderr_truncated": stderr_truncated,
        "log_path": str(log_path),
    }
