"""Minimal stdio MCP server used only by tests, to exercise the
seaglass-preferred path in grogu_imessage.py without depending on a real
seaglass installation. Mimics just enough of seaglass's `search_messages`
tool shape (see seaglass/mcp_server.py and seaglass/search/format.py in the
seaglass repo) for grogu_imessage._flatten_seaglass_result to flatten it.
"""
from mcp.server.mcpserver import MCPServer

server = MCPServer("seaglass-stub")


@server.tool()
def search_messages(query: str, max_sessions: int = 8, redact: bool = False) -> dict:
    """Return a single canned session/message so callers can assert on
    the flattened shape without needing a real index. Raises when `query`
    is the sentinel "__fail__", to exercise the SQL-LIKE fallback path."""
    if query == "__fail__":
        raise ValueError("intentional failure")
    return {
        "n_sessions": 1,
        "n_results": 1,
        "confidence": "high",
        "sessions": [
            {
                "chat_id": 42,
                "day": "2024-01-01",
                "score": 0.9,
                "messages": [
                    {
                        "message_id": 1001,
                        "ts": 1704067200.0,
                        "is_from_me": False,
                        "sender": "+15551234567",
                        "text": f"seaglass result for {query!r}",
                        "has_attachment": False,
                    }
                ],
                "context_messages": [
                    {
                        "message_id": 1000,
                        "ts": 1704067100.0,
                        "is_from_me": True,
                        "sender": "me",
                        "text": "context before the hit",
                        "has_attachment": False,
                    }
                ],
            }
        ],
    }


if __name__ == "__main__":
    server.run()
