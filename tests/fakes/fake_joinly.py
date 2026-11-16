"""A scripted joinly (DESIGN.md §52): one MCP session, a transcript resource.

Records ``join_meeting``, ``leave_meeting`` and the ``joinly-settings`` header, and
pushes segments the way the real server does: the resource is the whole transcript,
and a subscriber is told it changed. ``speak_text`` records what it was asked to say and,
like the real one, returns only when the speech is over: ``hold_speech`` keeps it in the
middle of a line until ``release_speech``, and ``interrupt_next`` makes the next reply the
one joinly gives when someone talks over it. ``synth_s_per_char`` models the time joinly takes to make speech
before any of it plays. ``get_video_snapshot`` returns ``frame`` as an
image, claiming ``frame_type``. ``share_image`` (the patch of deploy/joinly, §52.9) records the picture's
decoded bytes in ``shared`` and ``stop_share_image`` counts in ``share_stops``; ``share_error`` is the
sentence the tool fails with, and ``share_tools = False`` is a joinly without the patch.
``hold_join`` keeps ``join_meeting`` waiting until ``release_join``,
so a test can look at a call that is still joining.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import struct
import threading
import time
import zlib
from typing import Any

import mcp_types
import uvicorn
from fastmcp import FastMCP
from mcp.server.context import ServerRequestContext
from starlette.types import ASGIApp, Receive, Scope, Send

URI = "transcript://live/segments"
INTERRUPTED = 'Interrupted by detected speech. Spoken until now: "Heading to the..."'


def tiny_png() -> bytes:
    """A real 2 by 2 PNG, a few dozen bytes: a stand-in for a shared screen."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    rows = b"".join(b"\x00" + b"\x20\x60\xc0" * 2 for _ in range(2))
    header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    body = chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + body


class _Headers:
    """The ``joinly-settings`` header of each request, for the test to read."""

    def __init__(self, app: ASGIApp, sink: list[str], refuse: Any = None):
        self.app = app
        self.sink = sink
        self.refuse = refuse

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            raw = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            got = raw.get("joinly-settings")
            if got:
                self.sink.append(got)
                if self.refuse is not None and self.refuse() and b'"captions"' in got.encode():
                    await send({"type": "http.response.start", "status": 500, "headers": []})
                    await send({"type": "http.response.body", "body": b"unknown stt"})
                    return
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
        # hold_join: a join_meeting waits (counted in join_attempts) until release_join, so a test can look
        # at the broker while a call is still "joining". The join then fails if fail_join is set.
        self.hold_join = False
        self.join_attempts = 0
        self._join_gate = asyncio.Event()
        self.drop = False
        # the patched joinly's `transcription_status` (#764): the answer when captions are asked for;
        # None is a joinly without the tool (stock 0.5.4 never starts the session: see refuse_captions)
        self.captions: dict[str, Any] | None = {"ok": True, "reason": "on"}
        self.refuse_captions = False  # stock joinly: the captions settings make the session fail to start
        self.patched = True  # speak_text takes a voice (deploy/joinly); False: stock joinly 0.5.4
        self.spoken: list[str] = []
        self.voices: list[str | None] = []  # the voice each line was asked for (the patched speak_text)
        self.unknown_voices: set[str] = set()  # voices this stand-in's Kokoro does not have
        self.hold_speech = False
        # Making speech takes time before any of it plays, as in joinly: ``synth_s_per_char`` seconds a
        # character of a call's text; ``started`` is when each call began to play (time.monotonic), and
        # ``arrived`` when it came in.
        self.synth_s_per_char = 0.0
        self.arrived: list[float] = []
        self.started: list[float] = []
        self.interrupt_next = False
        self.fail_speech = False
        self._gate = asyncio.Event()
        self.snapshots = 0
        self.fail_snapshot = False
        self.frame = tiny_png()
        self.share_tools = True  # the patch's share_image and stop_share_image; False: stock joinly 0.5.4
        self.shared: list[bytes] = []  # the pictures share_image was given, decoded
        self.share_raw: list[str] = []  # and the text it was given: what travelled
        self.share_stops = 0
        self.share_error: str | None = None
        self.hold_share = False  # share_image waits (counted in share_attempts) until release_share
        self.share_attempts = 0
        self._share_gate = asyncio.Event()
        self.frame_type = "image/png"
        self.loop: asyncio.AbstractEventLoop | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    async def _synthesize(self, text: str) -> None:
        self.arrived.append(time.monotonic())
        if self.synth_s_per_char:
            await asyncio.sleep(self.synth_s_per_char * len(text))
        self.started.append(time.monotonic())

    def start(self) -> "FakeJoinly":
        fake = self
        mcp = FastMCP("joinly")

        @mcp.tool
        async def join_meeting(meeting_url: str, participant_name: str, passcode: str | None = None) -> str:
            fake._note_loop()
            fake.join_attempts += 1
            if fake.hold_join:
                await fake._join_gate.wait()
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

        if fake.patched:

            @mcp.tool
            async def speak_text(text: str, voice: str | None = None) -> str:
                fake._note_loop()
                fake.spoken.append(text)
                fake.voices.append(voice)
                await fake._synthesize(text)
                if voice is not None and voice in fake.unknown_voices:
                    return "Finished speaking. The voice was not found, so the session's voice was used."
                if fake.fail_speech:
                    raise RuntimeError("could not speak")
                if fake.hold_speech:
                    await fake._gate.wait()
                if fake.interrupt_next:
                    fake.interrupt_next = False
                    return INTERRUPTED
                return "Finished speaking."
        else:

            @mcp.tool
            async def speak_text(text: str) -> str:
                fake._note_loop()
                fake.spoken.append(text)
                fake.voices.append(None)
                await fake._synthesize(text)
                if fake.fail_speech:
                    raise RuntimeError("could not speak")
                if fake.hold_speech:
                    await fake._gate.wait()
                if fake.interrupt_next:
                    fake.interrupt_next = False
                    return INTERRUPTED
                return "Finished speaking."

        @mcp.tool
        async def get_video_snapshot() -> mcp_types.ImageContent:
            fake._note_loop()
            fake.snapshots += 1
            if fake.fail_snapshot:
                raise RuntimeError("Cannot take snapshot. Not currently in a meeting.")
            return mcp_types.ImageContent(
                type="image", data=base64.b64encode(fake.frame).decode(), mimeType=fake.frame_type
            )

        @mcp.tool
        async def transcription_status() -> str:
            fake._note_loop()
            if fake.captions is None:
                raise RuntimeError("no such tool")
            return json.dumps(fake.captions)

        if fake.share_tools:

            @mcp.tool
            async def share_image(image: str) -> str:
                fake._note_loop()
                fake.share_attempts += 1
                if fake.hold_share:
                    await fake._share_gate.wait()
                if fake.share_error is not None:
                    raise RuntimeError(fake.share_error)
                fake.share_raw.append(image)
                fake.shared.append(base64.b64decode(image, validate=True))
                return "Sharing the picture."

            @mcp.tool
            async def stop_share_image() -> str:
                fake._note_loop()
                fake.share_stops += 1
                return "Stopped sharing the picture."

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
            lambda: fake.refuse_captions,
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

    def release_join(self) -> None:
        """Let every join that is waiting in ``join_meeting``, and every later one, go on."""
        self.hold_join = False
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._join_gate.set)

    def release_share(self) -> None:
        """Let every share_image that is waiting, and every later one, go on."""
        self.hold_share = False
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._share_gate.set)

    def release_speech(self) -> None:
        """Let every line that is waiting in ``speak_text``, and every later one, finish."""
        self.hold_speech = False
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._gate.set)

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
