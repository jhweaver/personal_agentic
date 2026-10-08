"""A tiny MCP server over stdio for tests."""

from mcp.server.mcpserver import MCPServer

server = MCPServer("fake")


@server.tool()
def search_items(query: str) -> str:
    """Search the fake catalog."""
    return f"found 2 items for {query}"


@server.tool()
def send_message(channel: str, text: str) -> str:
    """Post a message to a channel."""
    return f"sent to {channel}: {text}"


@server.tool()
def crash() -> str:
    """Exit the server process (to test reconnecting)."""
    import os

    os._exit(0)


if __name__ == "__main__":
    server.run()
