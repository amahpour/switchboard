"""FakeAgent: a scripted MCP client driving a test-mode broker's ``/mcp`` (DESIGN.md §9.6, §49).

It speaks MCP over Streamable HTTP exactly like a harness's client would (the same ``initialize``,
``tools/call`` and ``_meta``), with an agent token, as the ``switchboard-test-agent`` client that only a
test-mode broker serves. It names its session (``X-Switchboard-Test-Session``) and how its answers are
confirmed (``X-Switchboard-Test-Ack``). The token is a row written the way ``agent_tokens.make`` does,
straight into the home's database (one per home, cached), so a hosted broker with no owner yet serves
it too. The broker side is the real thing: the endpoint, the token check, the tools, the engine.

``hook()`` calls the hidden hook tool the plugins' hooks call, so a test can report synthetic hook
events (a PostToolUse carrying a batch token, a Stop that waits) for the test agent's session.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx2
import mcp.types as mt
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from switchboard import db
from switchboard.broker import agent_tokens
from switchboard.broker.agent_http import PATH, TEST_ACK_HEADER, TEST_CLIENT, TEST_SESSION_HEADER
from switchboard.clock import SystemClock
from switchboard.mcp.client import call_sync
from switchboard.paths import Paths
from switchboard.store import Store

_TOKENS: dict[str, str] = {}  # a home -> the token FakeAgent made for it


def endpoint(home: str | Path) -> tuple[str, str, str]:
    """``(the broker's /mcp URL, an agent token, the Host to send)`` for the test-mode broker running
    from ``home``. The Host is the one the broker answers to: ``switchboard.localhost:<port>``, or its
    public URL's host when it is hosted (§30); the connection itself goes to the loopback address."""
    paths = Paths.from_home(home)
    key = str(paths.home)
    status = call_sync(paths.sock, "sys.status")
    url = f"http://127.0.0.1:{status['port']}{PATH}"
    host = urlparse(status["url"]).netloc
    if key not in _TOKENS or not _live(paths, _TOKENS[key]):
        token = agent_tokens.new_token()
        con = db.connect(paths.db)
        try:
            store = Store(con, SystemClock())
            device = f"test-{secrets.token_hex(3)}"
            if (
                store.agent_token_add(None, device, agent_tokens.token_hash(token)) is None
            ):  # pragma: no cover
                raise RuntimeError("could not make an agent token for the test agent")
        finally:
            con.close()
        _TOKENS[key] = token
    return url, _TOKENS[key], host


def _live(paths: Paths, token: str) -> bool:
    con = db.connect(paths.db, readonly=True)
    try:
        return Store(con, SystemClock()).agent_token_by_hash(agent_tokens.token_hash(token)) is not None
    finally:
        con.close()


class FakeAgent:
    def __init__(
        self,
        home: str | Path,
        key: str,
        *,
        ack: str = "next_call",
        client_name: str = TEST_CLIENT,
    ):
        self.home = Path(home)
        self.key = key
        self.ack = ack
        self.client_name = client_name
        self.session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def start(self) -> "FakeAgent":
        url, token, host = endpoint(self.home)
        http = httpx2.AsyncClient(
            headers={
                "Host": host,
                "Authorization": f"Bearer {token}",
                TEST_SESSION_HEADER.decode(): self.key,
                TEST_ACK_HEADER.decode(): self.ack,
            },
            timeout=httpx2.Timeout(300.0),
        )
        self._stack = AsyncExitStack()
        await self._stack.enter_async_context(http)
        streams = await self._stack.enter_async_context(streamable_http_client(url, http_client=http))
        r, w = streams[0], streams[1]
        self.session = await self._stack.enter_async_context(
            ClientSession(r, w, client_info=mt.Implementation(name=self.client_name, version="1"))
        )
        await self.session.initialize()
        return self

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except BaseException:  # the connection may already be gone
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

    async def hook(self, event: str, **fields: Any) -> dict[str, Any]:
        """A hook report for this session, as the plugins' hooks send it (§49.4): ``{"out": {...} | None,
        "batch_id": ...}``, the engine's own answer (a test-mode broker answers its test agent so)."""
        return await self.call("hook", event=event, **{k: str(v) for k, v in fields.items()})

    async def join(self, room: str, name: str) -> dict[str, Any]:
        return await self.call("join", room=room, screen_name=name)

    async def leave(self, room: str) -> dict[str, Any]:
        return await self.call("leave", room=room)

    async def who(self, room: str) -> dict[str, Any]:
        return await self.call("who", room=room)

    async def say(
        self, room: str, text: str, reply_to: int | None = None, since: int | None = None
    ) -> dict[str, Any]:
        return await self.call("say", room=room, text=text, reply_to=reply_to, since=since)

    async def read(self, room: str, limit: int | None = None, before: int | None = None) -> dict[str, Any]:
        return await self.call("read", room=room, limit=limit, before=before)

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
