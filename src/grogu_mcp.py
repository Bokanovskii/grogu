"""MCP client bridge: proxy configured MCP servers as codemode-callable tools.

This is Phase 2 of codemode (see docs/codemode.md): speaking the MCP wire
protocol itself is simple, but two things make it more than "add a client":

* MCP servers can be stateful across calls (Playwright's server holds a
  live browser session). Spawning a fresh server per call would drop that
  state, so each server gets one persistent background thread running its
  own asyncio event loop and MCP session for the lifetime of the
  interpreter process that imported this module (i.e. for the duration of
  one ``grogu codemode exec`` run).
* Calling an MCP tool this way intentionally bypasses Copilot CLI's own
  per-tool confirmation gate for destructive actions. There is no separate
  opt-in flag for this: MCP functions are bound into every codemode sandbox
  automatically whenever this module reports ``available()``, the same as
  a script being free to call ``requests``/``urllib`` against any API with
  no opt-in — see docs/codemode.md for the reasoning.

Grogu's baseline Python requirement is 3.10+ (see README.md), the same
version the ``mcp`` SDK requires, so this module runs in the same
interpreter as the rest of Grogu — no separate interpreter selection is
needed. ``available()`` still exists as a defensive check in case ``mcp``
isn't installed (e.g. ``setup.sh`` couldn't reach the package index).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Optional

MIN_PYTHON = (3, 10)


def _config_path() -> Path:
    override = os.environ.get("GROGU_MCP_CONFIG")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("COPILOT_HOME") or str(Path.home() / ".copilot")
    return Path(home).expanduser() / "mcp-config.json"


def load_servers() -> dict:
    """Local (stdio) MCP servers from the Copilot CLI MCP config.

    Non-local server types (e.g. remote/HTTP) are excluded for now; proxying
    them is a separate, unverified increment.
    """
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf8"))
    except (ValueError, OSError):
        return {}
    servers = payload.get("mcpServers", {})
    return {
        name: spec
        for name, spec in servers.items()
        if isinstance(spec, dict) and spec.get("type", "local") == "local" and spec.get("command")
    }


def available() -> bool:
    """Whether this interpreter can act as an MCP client at all.

    Should be ``True`` on any machine where Grogu's own setup completed
    successfully, since Grogu requires Python 3.10+ and installs ``mcp`` as
    part of ``setup.sh``. Checked defensively rather than assumed, so a
    partial/offline setup fails with a clear message instead of a cryptic
    import error deep inside a sandboxed script.
    """
    if sys.version_info < MIN_PYTHON:
        return False
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


def _extract_content(result) -> object:
    texts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            texts.append(text)
    if not texts:
        return None
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except (ValueError, TypeError):
        return joined


class _Bridge:
    """One persistent event loop + MCP session for a single configured server."""

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session = None
        self._stdio_cm = None
        self._session_cm = None
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._lock = threading.Lock()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        except BaseException as error:  # noqa: BLE001 - surface to the caller
            self._start_error = error
        finally:
            self._ready.set()
        if self._start_error is None:
            self._loop.run_forever()

    async def _connect(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self.spec["command"], args=self.spec.get("args", []) or []
        )
        self._stdio_cm = stdio_client(params)
        read, write = await self._stdio_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

    def _ensure_started(self, timeout: float = 30.0) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run_loop, daemon=True, name=f"grogu-mcp-{self.name}"
                )
                self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise TimeoutError(f"MCP server {self.name!r} did not start in time")
        if self._start_error is not None:
            raise RuntimeError(
                f"MCP server {self.name!r} failed to start: {self._start_error}"
            ) from self._start_error

    def _run_coro(self, coro_factory, timeout: float = 60.0):
        self._ensure_started()
        future = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
        return future.result(timeout=timeout)

    def list_tools(self) -> list:
        result = self._run_coro(lambda: self._session.list_tools())
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.input_schema,
            }
            for tool in result.tools
        ]

    def call_tool(self, tool: str, **kwargs) -> object:
        result = self._run_coro(lambda: self._session.call_tool(tool, kwargs))
        if getattr(result, "is_error", False):
            raise RuntimeError(f"MCP tool {tool!r} on {self.name!r} failed: {_extract_content(result)}")
        return _extract_content(result)

    def close(self) -> None:
        if self._loop is None:
            return

        async def _shutdown() -> None:
            if self._session_cm is not None:
                await self._session_cm.__aexit__(None, None, None)
            if self._stdio_cm is not None:
                await self._stdio_cm.__aexit__(None, None, None)

        try:
            future = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            future.result(timeout=10)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


_BRIDGES: dict = {}
_BRIDGES_LOCK = threading.Lock()


def _bridge(server: str) -> _Bridge:
    servers = load_servers()
    if server not in servers:
        known = ", ".join(sorted(servers)) or "(none configured)"
        raise ValueError(f"no local MCP server named {server!r} configured; known: {known}")
    with _BRIDGES_LOCK:
        if server not in _BRIDGES:
            _BRIDGES[server] = _Bridge(server, servers[server])
        return _BRIDGES[server]


def list_servers() -> list:
    """Names of every configured local MCP server (no connection made yet)."""
    return sorted(load_servers())


def list_tools(server: str) -> list:
    """Tool name/description/input schema for a server, connecting lazily."""
    return _bridge(server).list_tools()


def call_tool(server: str, tool: str, **kwargs) -> object:
    """Call one MCP tool on ``server`` and return its extracted content."""
    return _bridge(server).call_tool(tool, **kwargs)


def close_all() -> None:
    """Best-effort shutdown of every started bridge; safe to call repeatedly."""
    with _BRIDGES_LOCK:
        bridges = list(_BRIDGES.values())
        _BRIDGES.clear()
    for bridge in bridges:
        bridge.close()
