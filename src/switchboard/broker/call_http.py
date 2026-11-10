"""The routes of a hosted call (DESIGN.md §54, #623).

A call hosted on a person's machine (``meeting/hosted.py``) is registered by the host script and run by a
relay with a key made for that one call. Four routes:

- ``POST /mcp/meeting/host``: the host script registers a call: the room, the sha256 of the key it made, the
  platform and the model. It is opened by what the plugin's other scripts already present at ``/mcp/listen``
  and ``/mcp/session``, an agent token (``sbat_``) or a listener key (``sbl_``, which acts only for its own
  session, §49.11), through the same door (``AgentAuth.door``). It refuses a call key, a session cookie and
  any request with an ``Origin``. The tool list has no tool for this (the maintainer's yes, 2026-11-05): the
  model never handles the key or even its hash. With ``from_room: true``, this same registration
  returns its person's one-use private room handoff (§54), never through a model-facing tool.
- ``POST /mcp/meeting/push``: segments the bot heard (speaker, text, start, end), which the broker joins
  into lines and posts as it posts its own bot's (§52). The first push, an empty one too, marks the call
  live.
- ``POST /mcp/meeting/speech``: a long poll, answered with what is to be spoken (while voice is on), the
  room's names as the transcriber's hints, the voice switch and whether the call has ended.

- ``POST /mcp/meeting/leave``: the bot left or could not join; end only this key's call.

The call key (``sbc_``) is a bearer for the last three and nothing else: every other route answers an agent
token or a person's session and refuses it. Those three refuse an agent token, a session and a browser (any
request with an ``Origin``): a call key does one thing. A key that was never registered is ``unknown_key``
(401), one whose machine's sign-in was revoked is ``revoked`` (401), and one whose call is over is ``ended``
(410): a relay that is told so stops and leaves.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from starlette.routing import Route

from switchboard.broker.agent_http import ID_MAX, Receive, Scope, Send, _header, _id, _reply
from switchboard.broker.service import ServiceError
from switchboard.meeting import keys
from switchboard.meeting.hosted import BODY_MAX, Refused, done_in, parse_segments, wait_in
from switchboard.models import Meeting

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from switchboard.broker.app import BrokerState

HOST_PATH = "/mcp/meeting/host"
PUSH_PATH = "/mcp/meeting/push"
SPEECH_PATH = "/mcp/meeting/speech"
LEAVE_PATH = "/mcp/meeting/leave"
HOST_BODY_MAX = 4096  # the registration is a room, a hash, a platform, a model and a session id
REFUSED_STATUS = {"ended": 410}  # every other refusal of a key is a 401
HOST_STATUS = {"not_member": 403}  # a ServiceError code whose status is not ServiceError.HTTP's


async def _read_body(receive: Receive, send: Send, limit: int) -> bytes | None:
    """The request's body, or None after answering 413 once it passes ``limit`` bytes."""
    raw = b""
    while True:
        msg = await receive()
        raw += msg.get("body", b"")
        if len(raw) > limit:
            await _reply(send, 413, {"error": "too_large", "message": "too large"})
            return None
        if not msg.get("more_body"):
            return raw


class _CallRoute:
    """The call-key routes: POST only, no browser, a key that opens a call, a bounded JSON body."""

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
        raw = await _read_body(receive, send, BODY_MAX)
        if raw is None:
            return None
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        return got.call, body


async def _hung_up(receive: Receive) -> None:
    """Return when the relay closes the request. The body was read whole, so the next ``receive`` answers
    ``http.disconnect`` only once the connection is gone, as the listener's does (§49.4)."""
    while (await receive())["type"] != "http.disconnect":
        pass


async def _refused(send: Send, e: Refused) -> None:
    extra = (b"retry-after", str(max(1, int(e.retry_after + 0.999))).encode()) if e.retry_after else None
    await _reply(send, e.status, {"error": e.code, "message": e.message}, *([extra] if extra else []))


class MeetingPush(_CallRoute):
    """``/mcp/meeting/push``: segments in, lines posted (§54)."""

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
    """``/mcp/meeting/speech``: the long poll for what to say, the hints and the end (§54). A relay that
    hangs up ends its poll at once, before it is given a line it will not read."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        got = await self.enter(scope, receive, send)
        if got is None:
            return
        call, body = got
        try:
            poll = asyncio.ensure_future(self.state.hosted_calls.speech(call, wait_in(body), done_in(body)))
        except Refused as e:
            await _refused(send, e)
            return
        gone = asyncio.ensure_future(_hung_up(receive))
        try:
            await asyncio.wait({poll, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            gone.cancel()
            poll.cancel()  # a no-op once it has answered
            await asyncio.gather(poll, gone, return_exceptions=True)
        if poll.cancelled():
            return  # the relay is gone: nobody to answer
        try:
            res = poll.result()
        except Refused as e:
            await _refused(send, e)
            return
        await _reply(send, 200, res)


class MeetingLeave(_CallRoute):
    """``/mcp/meeting/leave``: end only the call authenticated by this key (§54)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        got = await self.enter(scope, receive, send)
        if got is None:
            return
        call, body = got
        if not isinstance(body, dict):
            await _reply(send, 400, {"error": "bad_request", "message": "a JSON object is needed"})
            return
        try:
            self.state.hosted_calls.left(call, body.get("reason"))
        except Refused as e:
            await _refused(send, e)
            return
        await _reply(send, 200, {"ended": True})


class MeetingHost:
    """``/mcp/meeting/host``: the host script registers a call (§54). The door is the listener's and the
    session hook's (``AgentAuth.door``): an agent token, or a listener key, which is only ever taken there.
    The key is for its own session, which the broker already knows (named by a hook of its connection, or
    pinned by its first request): that session is never read from the body, and a body that names another is
    refused as it is at the listener (403 ``wrong_session``). A token may name a session in the body, and
    then the room must have a member of that session on the token's machine; with none named, any member on
    that machine will do. A call key opens nothing here, and neither does a cookie or an ``Origin``."""

    def __init__(self, state: "BrokerState") -> None:
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await _reply(send, 405, {"error": "method", "message": "POST only"})
            return
        if _header(scope, b"origin") is not None:
            await _reply(send, 403, {"error": "forbidden", "message": "a script, not a browser"})
            return
        auth = self.state.agent_auth
        door = await auth.door(scope, send, "an agent token is needed")
        if door is None:
            return
        raw = await _read_body(receive, send, HOST_BODY_MAX)
        if raw is None:
            return
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        asked = body.get("session") if isinstance(body, dict) else None
        if not isinstance(body, dict) or (asked is not None and _id(asked) is None):
            bad = f"a JSON object; session, if given, is up to {ID_MAX} characters"
            await _reply(send, 400, {"error": "bad_request", "message": bad})
            return
        session, harness = asked, None
        if door.key is not None:
            bound = self.state.listener_keys.scope_of(door.key)
            if bound is None:
                await _reply(
                    send,
                    409,
                    {"error": "no_session", "message": "the listener key is not for a session yet"},
                )
                return
            session, harness = bound.session_id, bound.harness
            if asked is not None and await auth.other_session(door, asked, send, harness=harness):
                return
        try:
            res = self.state.hosted_calls.register(door.row, body, session=session, harness=harness)
        except Refused as e:
            await _refused(send, e)
            return
        except ServiceError as e:
            status = HOST_STATUS.get(e.code, e.http_status)
            await _reply(send, status, {"error": e.code, "message": e.message})
            return
        await _reply(send, 200, res)


def install(app: "FastAPI", state: "BrokerState") -> None:
    app.router.routes.append(Route(HOST_PATH, endpoint=MeetingHost(state)))
    app.router.routes.append(Route(PUSH_PATH, endpoint=MeetingPush(state)))
    app.router.routes.append(Route(SPEECH_PATH, endpoint=MeetingSpeech(state)))
    app.router.routes.append(Route(LEAVE_PATH, endpoint=MeetingLeave(state)))
