"""Release long polls before the HTTP server drains on restart (DESIGN.md §15, §49)."""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import uvicorn
from sse_starlette.sse import AppStatus
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from switchboard.broker.app import BrokerState

RESTART_MESSAGE = "switchboard is restarting; try again in a minute"
RETRY_AFTER_S = 1


class BrokerServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, state: BrokerState) -> None:
        super().__init__(config)
        self.broker = state

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The SSE library otherwise cancels POST streams as soon as SIGTERM arrives,
        # before shutdown() can answer them. This server closes its own streams instead.
        # Threaded test servers do not own signals and must not change this process-wide flag.
        own_drain = (
            threading.current_thread() is threading.main_thread()
            and AppStatus.enable_automatic_graceful_drain
        )
        if own_drain:
            AppStatus.disable_automatic_graceful_drain()
        try:
            with super().capture_signals():
                yield
        finally:
            if own_drain:
                AppStatus.enable_automatic_graceful_drain_mode()

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        # Lifespan cleanup runs AFTER Uvicorn waits for requests, including day-long Stop hooks.
        self.broker.begin_shutdown()
        await super().shutdown(sockets)


class RestartGate:
    """Refuse new agent requests once draining starts, before they can open another sink."""

    def __init__(self, app: ASGIApp, state: BrokerState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == "/mcp" or path.startswith("/mcp/")) and self.state.draining:
            response = JSONResponse(
                {"error": "restarting", "message": RESTART_MESSAGE},
                status_code=503,
                headers={"Retry-After": str(RETRY_AFTER_S), "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        if scope["type"] == "http" and path == "/mcp" and scope.get("method") == "GET":
            # MCP's idle server-to-client SSE stream has no sink. End it too, or Uvicorn
            # never reaches the lifespan that owns its session manager.
            started = finished = False

            async def forward(message: Message) -> None:
                nonlocal started, finished
                if message["type"] == "http.response.start":
                    started = True
                elif message["type"] == "http.response.body" and not message.get("more_body", False):
                    finished = True
                await send(message)

            work = asyncio.ensure_future(self.app(scope, receive, forward))
            stopping = asyncio.create_task(self.state.restart_event.wait())
            try:
                await asyncio.wait({work, stopping}, return_when=asyncio.FIRST_COMPLETED)
                if work.done():
                    await work
                else:
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    if started and not finished:
                        await send({"type": "http.response.body", "body": b""})
            finally:
                work.cancel()
                stopping.cancel()
                await asyncio.gather(work, stopping, return_exceptions=True)
            return
        await self.app(scope, receive, send)
