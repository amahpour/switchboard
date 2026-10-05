"""FakeAgent: a scripted MCP client driving a real ``switchboard mcp --harness test``.

It speaks MCP over stdio through ``mcp.client.stdio`` exactly like a harness
would, so the server, the broker connection, identity verification and the
credential handling are all the real thing. The MCP server's parent (and so
the participant's "agent" process) is this pytest process.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import mcp.types as mt
from conftest import child_env
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class FakeAgent:
    def __init__(
        self,
        home: str | Path,
        key: str,
        *,
        ack: str = "next_call",
        env: dict[str, str] | None = None,
        harness_test: bool = True,
        client_name: str = "fake-agent",
    ):
        self.home = Path(home)
        self.key = key
        self.ack = ack
        self.env = child_env(**(env or {}))
        self.harness_test = harness_test
        self.client_name = client_name
        self.session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    @property
    def argv(self) -> list[str]:
        a = ["-m", "switchboard", "mcp", "--home", str(self.home)]
        if self.harness_test:
            a += ["--harness", "test", "--test-session", self.key, "--ack", self.ack]
        return a

    async def start(self) -> "FakeAgent":
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=sys.executable, args=self.argv, env=self.env)
        r, w = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(
            ClientSession(r, w, client_info=mt.Implementation(name=self.client_name, version="1"))
        )
        await self.session.initialize()
        return self

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except BaseException:  # the stdio pipes may already be gone
                pass
            self._stack = None

    async def __aenter__(self) -> "FakeAgent":
        return await self.start()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # --------------------------------------------------------------- tools
    async def raw(self, tool: str, args: dict[str, Any], **kw: Any) -> mt.CallToolResult:
        assert self.session is not None
        return await self.session.call_tool(tool, args, **kw)

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        res = await self.raw(tool, {k: v for k, v in args.items() if v is not None})
        assert not res.is_error, res
        text = res.content[0].text  # type: ignore[union-attr]
        out = json.loads(text)
        self.calls.append((tool, args, out))
        return out

    async def list_tools(self) -> list[mt.Tool]:
        assert self.session is not None
        return (await self.session.list_tools()).tools

    async def join(self, room: str, name: str) -> dict[str, Any]:
        return await self.call("join", room=room, screen_name=name)

    async def leave(self, room: str) -> dict[str, Any]:
        return await self.call("leave", room=room)

    async def who(self, room: str) -> dict[str, Any]:
        return await self.call("who", room=room)

    async def say(self, room: str, text: str, reply_to: int | None = None) -> dict[str, Any]:
        return await self.call("say", room=room, text=text, reply_to=reply_to)

    async def read(self, room: str, limit: int | None = None) -> dict[str, Any]:
        return await self.call("read", room=room, limit=limit)

    async def wait(self, room: str, timeout_s: int | None = None) -> dict[str, Any]:
        return await self.call("wait", room=room, timeout_s=timeout_s)

    async def pass_(self, room: str | None = None, note: str | None = None) -> dict[str, Any]:
        return await self.call("pass", room=room, note=note)

    async def away(self, message: str | None = None) -> dict[str, Any]:
        return await self.call("away", message=message)

    def wait_task(self, room: str, timeout_s: int | None = None) -> asyncio.Task[dict[str, Any]]:
        return asyncio.get_running_loop().create_task(self.wait(room, timeout_s))


def ids_in(text: str | None) -> list[int]:
    """Message ids listed in a rendered batch."""
    import re

    return [int(x) for x in re.findall(r"^- id=(\d+) ", text or "", re.M)]
