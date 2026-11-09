"""A tiny MCP client for the board (used by the trial to measure what reading a board costs)."""
import base64
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def read_image(base: str, token: str = "tok-claude-1") -> bytes:
    async with Client(StreamableHttpTransport(base + "/agent/mcp", headers={"Authorization": "Bearer " + token})) as c:
        r = await c.call_tool("read_board", {"format": "image"})
        item = r.content[0]
        return base64.b64decode(item.data)
