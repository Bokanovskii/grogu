"""Minimal stdio MCP server used only by tests, to exercise grogu_mcp.py
without depending on any real external MCP server being installed/configured.
"""
from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")


@server.tool()
def echo(text: str) -> str:
    """Echo back the given text."""
    return text


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def fail() -> str:
    """Always raises, to exercise error propagation."""
    raise ValueError("intentional failure")


if __name__ == "__main__":
    server.run()
