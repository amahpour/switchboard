"""A scripted joinly (DESIGN.md §52): one MCP session, a transcript resource.

Records ``join_meeting``, ``leave_meeting`` and the ``joinly-settings`` header, and
pushes segments the way the real server does: the resource is the whole transcript,
and a subscriber is told it changed.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from typing import Any

import mcp_types
import uvicorn
from fastmcp import FastMCP
from mcp.server.context import ServerRequestContext
from starlette.types import ASGIApp, Receive, Scope, Send

URI = "transcript://live/segments"


class _Headers:
    """The ``joinly-settings`` header of each request, for the test to read."""

    def __init__(self, app: ASGIApp, sink: list[str]):
        self.app = app
        self.sink = sink

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            raw = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            got = raw.get("joinly-settings")
            if got:
                self.sink.append(got)
        await self.app(scope, receive, send)


class FakeJoinly:
    """One meeting server. ``push`` appends segments and notifies subscribers."""

    def __init__(self) -> None:
        self.segments: list[dict[str, Any]] = []
        self.joins: list[dict[str, Any]] = []
        self.leaves = 0
        self.settings: list[str] = []
        self.sessions: list[Any] = []
        self.fail_join = False
        self.drop = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def start(self) -> "FakeJoinly":
        fake = self
        mcp = FastMCP("joinly")

        @mcp.tool
        async def join_meeting(meeting_url: str, participant_name: str, passcode: str | None = None) -> str:
            fake._note_loop()
            if fake.fail_join:
                raise RuntimeError("could not join")
            fake.joins.append(
                {
                    "meeting_url": meeting_url,
                    "participant_name": participant_name,
                    "passcode": passcode,
                }
            )
            return "joined"

        @mcp.tool
        async def leave_meeting() -> str:
            fake._note_loop()
            fake.leaves += 1
            return "left"

        @mcp.resource(URI)
        def transcript() -> str:
            fake._note_loop()
            if fake.drop:
                raise RuntimeError("the session dropped")
            return json.dumps({"segments": fake.segments})

        async def _subscribe(
            ctx: ServerRequestContext, params: mcp_types.SubscribeRequestParams
        ) -> mcp_types.EmptyResult:
            fake._note_loop()
            if ctx.session not in fake.sessions:
                fake.sessions.append(ctx.session)
            return mcp_types.EmptyResult()

        mcp._mcp_server.add_request_handler(
            "resources/subscribe", mcp_types.SubscribeRequestParams, _subscribe
        )
        app = _Headers(
            mcp.http_app(path="/mcp/", transport="streamable-http", host_origin_protection=False),
            self.settings,
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp/"
        config = uvicorn.Config(app, log_level="warning", access_log=False, log_config=None, lifespan="on")
        self._server = uvicorn.Server(config)
        server = self._server

        def run() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(server.serve(sockets=[sock]))

        self._thread = threading.Thread(target=run, name="fake-joinly", daemon=True)
        self._thread.start()
        limit = time.monotonic() + 10
        while self._server is None or not self._server.started:
            if time.monotonic() > limit:
                raise RuntimeError("fake joinly did not start")
            time.sleep(0.01)
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(10)

    def _note_loop(self) -> None:
        if self.loop is None:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                return

    def push(self, segments: list[dict[str, Any]]) -> None:
        """Append ``segments`` and tell every subscriber. Called from the test thread."""
        if self.loop is None:
            raise RuntimeError("fake joinly has no session yet")

        async def _go() -> None:
            self.segments.extend(segments)
            note = mcp_types.ResourceUpdatedNotification(
                params=mcp_types.ResourceUpdatedNotificationParams(uri=URI)
            )
            for session in list(self.sessions):
                await session.send_notification(note)

        fut = asyncio.run_coroutine_threadsafe(_go(), self.loop)
        fut.result(timeout=5)
