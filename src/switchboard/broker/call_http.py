"""The two routes only a call's key opens (DESIGN.md §52.12, #623).

A call hosted on a person's machine (``meeting/hosted.py``) is run by a relay with a key made for that
one call. The key is a bearer here and nowhere else: ``Authorization: Bearer sbc_…`` on

- ``POST /mcp/meeting/push``: segments the bot heard (speaker, text, start, end), which the broker joins
  into lines and posts as it posts its own bot's (§52). The first push, an empty one too, marks the call
  live.
- ``POST /mcp/meeting/speech``: a long poll, answered with what is to be spoken (while voice is on), the
  room's names as the transcriber's hints, the voice switch and whether the call has ended.

Every other route answers an agent token or a person's session and refuses this key, and these two refuse
an agent token, a session and a browser (any request with an ``Origin``): a call key does one thing. A key
that was never registered is ``unknown_key`` (401), one whose machine's sign-in was revoked is ``revoked``
(401), and one whose call is over is ``ended`` (410): a relay that is told so stops and leaves.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from starlette.routing import Route

from switchboard.broker.agent_http import Receive, Scope, Send, _header, _reply
from switchboard.meeting import keys
from switchboard.meeting.hosted import BODY_MAX, Refused, parse_segments, wait_in
from switchboard.models import Meeting

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from switchboard.broker.app import BrokerState

PUSH_PATH = "/mcp/meeting/push"
SPEECH_PATH = "/mcp/meeting/speech"
REFUSED_STATUS = {"ended": 410}  # every other refusal of a key is a 401


class _CallRoute:
    """What both routes do first: POST only, no browser, a call key that opens a call, a small JSON body."""

    def __init__(self, state: "BrokerState") -> None:
        self.state = state

    async def enter(self, scope: Scope, receive: Receive, send: Send) -> tuple[Meeting, Any] | None:
        """The call this request's key opens and its JSON body, or None after the refusal was answered."""
        if scope["type"] != "http" or scope.get("method") != "POST":
            await _reply(send, 405, {"error": "method", "message": "POST only"})
            return None
        if _header(scope, b"origin") is not None:
            await _reply(send, 403, {"error": "forbidden", "message": "a relay, not a browser"})
            return None
        scheme, _, value = (_header(scope, b"authorization") or "").partition(" ")
        value = value.strip()
        if scheme.lower() != "bearer" or not value.startswith(keys.PREFIX):
            await _reply(send, 401, {"error": "unauthorized", "message": "a call key is needed"})
            return None
        hosted = self.state.hosted_calls
        got = hosted.check(value) if hosted is not None else None
        if got is None or got.call is None:
            why = got.why if got is not None else "unknown_key"
            await _reply(
                send,
                REFUSED_STATUS.get(why, 401),
                {
                    "error": why,
                    "message": "the call key was not accepted" if why != "ended" else "the call has ended",
                },
            )
            return None
        raw = b""
        while True:
            msg = await receive()
            raw += msg.get("body", b"")
            if len(raw) > BODY_MAX:
                await _reply(send, 413, {"error": "too_large", "message": "too large"})
                return None
            if not msg.get("more_body"):
                break
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        return got.call, body


async def _refused(send: Send, e: Refused) -> None:
    extra = (b"retry-after", str(max(1, int(e.retry_after + 0.999))).encode()) if e.retry_after else None
    await _reply(send, e.status, {"error": e.code, "message": e.message}, *([extra] if extra else []))


class MeetingPush(_CallRoute):
    """``/mcp/meeting/push``: segments in, lines posted (§52.12)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        got = await self.enter(scope, receive, send)
        if got is None:
            return
        call, body = got
        try:
            segments = parse_segments(body)
            res = self.state.hosted_calls.push(call, segments)
        except Refused as e:
            await _refused(send, e)
            return
        await _reply(send, 200, res)


class MeetingSpeech(_CallRoute):
    """``/mcp/meeting/speech``: the long poll for what to say, the hints and the end (§52.12)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        got = await self.enter(scope, receive, send)
        if got is None:
            return
        call, body = got
        try:
            res = await self.state.hosted_calls.speech(call, wait_in(body))
        except Refused as e:
            await _refused(send, e)
            return
        await _reply(send, 200, res)


def install(app: "FastAPI", state: "BrokerState") -> None:
    app.router.routes.append(Route(PUSH_PATH, endpoint=MeetingPush(state)))
    app.router.routes.append(Route(SPEECH_PATH, endpoint=MeetingSpeech(state)))
