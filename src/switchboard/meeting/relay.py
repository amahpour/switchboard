"""The relay of a call hosted on a person's machine (DESIGN.md §54, #623).

``switchboard meeting relay`` runs in switchboard's image beside a joinly of its own (the host script's
Compose project, ``plugins/claude/meeting/compose.yaml``) and is the bridge between that joinly and the
broker. It is what ``bridge.py`` is for the broker's own bot, turned around: the broker does not reach out to
it, it reaches in over HTTPS, holding the key made for this one call.

- It reads the call's key and the meeting link from stdin or from files only its owner can read, never from
  an argument or the environment, and never prints or logs either. It prints the key's **fingerprint**
  (the same eight characters the room's call bar shows, ``meeting/keys.py``) so its owner can check it.
- It asks the broker for the room's names (``POST /mcp/meeting/speech``, answered at once), joins the call
  through joinly with them as hints, and marks the call live with a first, empty push.
- It then does two things until the call ends. It reads joinly's transcript and pushes each new segment to
  ``POST /mcp/meeting/push`` (the broker joins them into lines and posts them, the same code its own bot's
  lines go through). And it long-polls ``POST /mcp/meeting/speech`` for the ``@call`` lines of agents, while
  voice is on, and has joinly say them (``speak_text``).
- It leaves the call and exits when the broker says so: ``ended`` in a poll, a 410 on any request (a person
  ended the call, the room closed, the broker stopped), or a 401 (the key is not accepted, or its machine's
  sign-in was revoked). A 429 is waited out for as long as ``Retry-After`` says. An error is retried with a
  growing pause, up to ``QUIET_S``, which is how long the broker waits before it ends a call that has
  heard nothing (``hosted.QUIET_S``): past it nothing is gained by trying.

The **exit code** says why it stopped (``Exit``), so the container's status is an answer. The pieces that
need a network are small and replaceable: the broker's side is ``BrokerClient`` (the standard library's
HTTP, a thread per request, closed from outside when the relay stops) and joinly's is ``JoinlyClient``
(``joinly.py``); ``Relay`` takes both, so its tests script them. Nothing here waits on the clock except
through ``Relay.sleep``.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import os
import re
import signal
import socket
import ssl
import stat
import sys
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol, TextIO

from switchboard.meeting import joinly, keys
from switchboard.meeting.lines import Segment
from switchboard.meeting.links import LinkError, parse_link

PUSH_PATH = "/mcp/meeting/push"
SPEECH_PATH = "/mcp/meeting/speech"
LEAVE_PATH = "/mcp/meeting/leave"
DEFAULT_VOICE = "bm_george"  # the Kokoro voice the broker's own bot speaks with by default (§52)
DEFAULT_JOINLY = "http://127.0.0.1:8000/mcp/"

# The numbers the broker holds a source to (``meeting/hosted.py``). They are written out here so the relay
# does not import the broker; ``tests/unit/test_meeting_relay.py`` fails if they drift apart.
BATCH_MAX = 100  # segments in one push
SPEECH_WAIT_S = 25.0  # how long a speech poll waits
QUIET_S = 300.0  # how long the broker waits for a key it hears nothing from before it ends the call

JOIN_TIMEOUT_S = (
    540.0  # a first start loads a 1.6 GB model before joinly joins; the broker allows ten minutes
)
JOINLY_START_S = 180.0  # joinly's container may still be starting when the relay is
JOINLY_OPEN_S = 20.0  # one try to connect to it
SPEECH_MIN_GAP_S = 0.5  # polls never come faster than this, whatever the broker answers
REQUEST_TIMEOUT_S = 30.0  # a push; a poll waits ``SPEECH_WAIT_S`` and this much on top
BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 30.0
RETRY_AFTER_MAX_S = 60.0  # a longer Retry-After is waited out in pieces this long
PENDING_MAX = 2000  # segments waiting to be pushed; past it the oldest go, with one line of warning
DEPARTURE_S = 3.0  # best effort, within Docker's stop grace after the final push
FINAL_PUSH_S = 5.0  # a relay told to stop reads the transcript once more, and waits this long to push it
REPLY_MAX = 64 * 1024  # the most of an answer it reads
SECRET_FILE_MAX = 4096

LOOPBACK = ("localhost", "127.0.0.1", "::1")


class Exit(IntEnum):
    """Why the relay stopped: its exit code."""

    ENDED = 0  # the call is over: the broker said so (a person ended it, the room closed, the broker stopped)
    USAGE = 2  # a key or link it could not read or did not accept, or an argument that is wrong
    REFUSED = 3  # the broker did not accept the key (401): wrong, or its machine's sign-in was revoked
    JOIN_FAILED = 4  # joinly could not join the call: a bad link, a waiting room nobody admitted, no audio
    JOINLY_LOST = 5  # joinly could not be reached, or dropped while in the call
    SERVER_LOST = 6  # the broker could not be reached for ``QUIET_S``
    STOPPED = 7  # told to stop (SIGTERM, as ``docker stop`` sends it, or ^C)


WHY = {
    Exit.ENDED: "the call has ended",
    Exit.USAGE: "the key, the link or an argument was not usable",
    Exit.REFUSED: "the broker did not accept the call key",
    Exit.JOIN_FAILED: "joinly could not join the call",
    Exit.JOINLY_LOST: "joinly was lost",
    Exit.SERVER_LOST: "the broker could not be reached",
    Exit.STOPPED: "stopped",
}


class UsageError(Exception):
    """An argument, key or link that cannot be used. The message never holds the key or the link."""


# ----------------------------------------------------------------------------- what is read, and how
class Secret(str):
    """A string that does not show itself: ``str``, ``repr`` and ``format`` all say ``<secret>``. The key and
    the link are held as one, so an f-string, a ``%s`` or a traceback that meets one prints nothing of it.
    ``reveal`` is the one way to the text, used where it is sent."""

    __slots__ = ()

    def __str__(self) -> str:
        return "<secret>"

    def __repr__(self) -> str:
        return "<secret>"

    def __format__(self, spec: str) -> str:
        return "<secret>"

    def reveal(self) -> str:
        return self.encode("utf-8").decode("utf-8")


def server_target(address: str, *, allow_http: bool = False) -> tuple[str, str, int, str]:
    """``(scheme, host, port, path prefix)`` of the broker's address. It must be ``https``; a plain ``http``
    address is only taken for this machine's own (``localhost``), or when ``allow_http`` says the network
    in between is trusted: the key is sent as a bearer, so it must not cross an open network in the clear."""
    parts = urllib.parse.urlsplit(address.strip())
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise UsageError("--server is the broker's address, such as https://switchboard.example.com")
    if parts.scheme == "http" and not (allow_http or host in LOOPBACK or host.endswith(".localhost")):
        raise UsageError(
            "--server is a plain http address: the key would cross the network in the clear."
            " Use https, or --allow-http on a network you trust"
        )
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise UsageError("--server has a port that is not a number") from None
    return parts.scheme, host, port, parts.path.rstrip("/")


def read_secrets(
    key_source: str, link_source: str | None, *, stdin: TextIO | None = None, mode_check: bool = True
) -> tuple[Secret, Secret | None]:
    """The call key and the meeting link, from files only their owner reads or from stdin (``-``: the key's
    line first, then the link's). With ``mode_check`` a file readable by anyone else is refused. Each is
    checked for its shape, and nothing in an error repeats what was read. ``link_source`` None is a relay
    that joins no call."""
    stdin = stdin if stdin is not None else sys.stdin
    key = _read_line(key_source, "the call key", stdin, mode_check)
    if keys.KEY_RE.fullmatch(key) is None:
        raise UsageError("the call key is not an sbc_ key of 64 hex characters")
    if link_source is None:
        return Secret(key), None
    link = _read_line(link_source, "the meeting link", stdin, mode_check)
    try:
        parse_link(link)
    except LinkError:
        raise UsageError("the meeting link is not an https link to a Zoom, Meet or Teams call") from None
    return Secret(key), Secret(link)


def _read_line(source: str, what: str, stdin: TextIO, mode_check: bool) -> str:
    if source == "-":
        line = stdin.readline(SECRET_FILE_MAX + 1)
    else:
        try:
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise UsageError(f"{what}: the file cannot be opened") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise UsageError(f"{what}: not a regular file")
            if mode_check and st.st_mode & 0o077:
                raise UsageError(
                    f"{what}: the file can be read by others (mode {st.st_mode & 0o777:03o}): chmod 600"
                )
            line = os.read(fd, SECRET_FILE_MAX + 1).decode("utf-8", "replace").split("\n", 1)[0]
        finally:
            os.close(fd)
    line = line.strip()
    if not line or len(line) > SECRET_FILE_MAX:
        raise UsageError(f"{what}: nothing to read")
    return line


# --------------------------------------------------------------------------- the broker's side
@dataclass(frozen=True)
class Reply:
    """What the broker answered: the status, the JSON object it sent (empty when it sent none), and how long
    ``Retry-After`` asks to wait."""

    status: int
    body: dict[str, Any]
    retry_after: float | None = None


class Unreachable(Exception):
    """The broker could not be reached, or hung up. The message never holds the key."""


class Broker(Protocol):
    async def post(self, path: str, body: dict[str, Any], timeout: float) -> Reply: ...

    def abort(self) -> None: ...


class BrokerClient:
    """The broker's key routes over HTTPS, with the standard library: one connection a request, in a thread,
    which ``abort`` closes from outside so a long poll ends the moment the relay is told to stop. TLS uses the
    machine's own trust store, as the broker's sign-in to an identity provider does (``oidc.py``)."""

    def __init__(self, address: str, key: Secret, *, allow_http: bool = False) -> None:
        self.scheme, self.host, self.port, self.prefix = server_target(address, allow_http=allow_http)
        self._key = key
        self._open: set[http.client.HTTPConnection] = set()
        self._lock = threading.Lock()  # the set is used by the request threads and by ``abort``
        self._closed = False

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        if self.scheme == "https":
            import truststore

            ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _post(self, path: str, body: dict[str, Any], timeout: float) -> Reply:
        raw = json.dumps(body, ensure_ascii=True).encode("ascii")
        conn = self._connection(timeout)
        with self._lock:
            self._open.add(conn)
        try:
            conn.request(
                "POST",
                self.prefix + path,
                body=raw,
                headers={
                    "Authorization": "Bearer " + self._key.reveal(),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "switchboard-meeting-relay",
                },
            )
            resp = conn.getresponse()
            data = resp.read(REPLY_MAX + 1)
            wait: float | None = None
            try:
                wait = float(resp.getheader("Retry-After") or "")
            except ValueError:
                wait = None
            try:
                parsed = json.loads(data[:REPLY_MAX]) if data else {}
            except ValueError:
                parsed = {}
            return Reply(resp.status, parsed if isinstance(parsed, dict) else {}, wait)
        except (OSError, http.client.HTTPException, ssl.SSLError) as e:
            # the class says what went wrong; the text of an HTTP library's error is not repeated
            raise Unreachable(type(e).__name__) from None
        finally:
            with self._lock:
                self._open.discard(conn)
            conn.close()

    async def post(self, path: str, body: dict[str, Any], timeout: float) -> Reply:
        if self._closed:
            raise Unreachable("closed")
        return await asyncio.to_thread(self._post, path, body, timeout)

    def abort(self) -> None:
        """Close every request that is under way (and refuse new ones). A thread blocked in a read wakes
        with an error, which ``post`` turns into ``Unreachable``."""
        self._closed = True
        with self._lock:
            pending = list(self._open)
        for conn in pending:
            sock = conn.sock
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(Exception):
                conn.close()


# --------------------------------------------------------------------------- joinly's side
class Joinly(Protocol):
    """What the relay asks of joinly. ``JoinlyClient`` is the real one."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def join(self, url: str, name: str, timeout: float) -> None: ...

    async def segments(self) -> list[Segment]: ...

    async def changed(self, stop: asyncio.Event, timeout: float) -> None: ...

    async def speak(self, text: str) -> str: ...

    async def leave(self) -> None: ...


class JoinlyClient:
    """A joinly reached over MCP, as the broker's own bot reaches one (``joinly.py`` is what they share)."""

    def __init__(self, url: str, header: str) -> None:
        self.url, self.header = url, header
        self._client: Any = None
        self._handler: joinly.Updates | None = None

    async def open(self) -> None:
        client, handler = joinly.open_client(self.url, self.header)
        await asyncio.wait_for(client.__aenter__(), JOINLY_OPEN_S)
        self._client, self._handler = client, handler
        await joinly.subscribe(client)  # without it the transcript is read on a timer, which is slower

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.__aexit__(None, None, None), joinly.LEAVE_TIMEOUT_S)

    async def join(self, url: str, name: str, timeout: float) -> None:
        await self._client.call_tool(
            "join_meeting", {"meeting_url": url, "participant_name": name}, timeout=timeout
        )

    def _updates(self) -> joinly.Updates:
        if self._handler is None:
            raise RuntimeError("joinly is not open")
        return self._handler

    async def segments(self) -> list[Segment]:
        self._updates().updated.clear()
        return joinly.segments(await self._client.read_resource(joinly.RESOURCE))

    async def changed(self, stop: asyncio.Event, timeout: float) -> None:
        await joinly.wait_for(self._updates(), stop, timeout)

    async def speak(self, text: str) -> str:
        result = await self._client.call_tool("speak_text", {"text": text}, timeout=joinly.SPEAK_TIMEOUT_S)
        return joinly.reply(result)

    async def leave(self) -> None:
        await asyncio.wait_for(
            self._client.call_tool("leave_meeting", {}, timeout=joinly.LEAVE_TIMEOUT_S),
            joinly.LEAVE_TIMEOUT_S + 3,
        )


# ---------------------------------------------------------------------------------- the relay
@dataclass(frozen=True)
class RelayConfig:
    # the speech model to ask joinly for; "" leaves it joinly's own (``joinly.settings_header``)
    model: str = ""
    voice: str = DEFAULT_VOICE
    participant: str = joinly.PARTICIPANT
    join: bool = True  # False: start everything and join no call (a check of the setup, ``--no-join``)
    burn: bool = False  # delete the link's file once the call is joined, and the key's file at the end
    key_path: str | None = None
    link_path: str | None = None


OpenJoinly = Callable[[Sequence[str], str], Joinly]


class Relay:
    """One hosted call's relay. ``run`` returns the exit code (``Exit``)."""

    def __init__(
        self,
        cfg: RelayConfig,
        key: Secret,
        link: Secret | None,
        broker: Broker,
        open_joinly: OpenJoinly,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        now: Callable[[], float] = time.monotonic,
        say: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg, self._key, self._link = cfg, key, link
        self.broker, self.open_joinly = broker, open_joinly
        self._sleep_fn = sleep
        self._now = now
        self._say = say or _print
        self.fingerprint = keys.fingerprint(keys.key_hash(key.reveal()))
        self.exit: Exit | None = None
        self.stop = asyncio.Event()
        self._speech: deque[str] = deque()
        self._speech_ready = asyncio.Event()
        self._down_since: float | None = None
        self._pause = BACKOFF_FIRST_S
        self._pending: deque[dict[str, Any]] = deque()
        self._warned_pending = False
        self._seen: set[tuple[Any, ...]] = set()
        self._joining = False

    # ------------------------------------------------------------------ small things
    def say(self, text: str) -> None:
        """One line on stderr. It never holds the key or the link."""
        self._say(f"relay: {text}")

    def finish(self, code: Exit, why: str | None = None) -> None:
        """Stop with ``code``. The first reason wins: a relay that was told the call ended and then loses
        joinly while leaving it still says it ended."""
        if self.exit is None:
            self.exit = code
            self.say(why or WHY[code])
        self.stop.set()

    async def sleep(self, seconds: float) -> None:
        """Wait ``seconds``, or until the relay is stopping. The only place that waits on the clock."""
        if self.stop.is_set():
            return
        if self._sleep_fn is not None:
            await self._sleep_fn(seconds)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), seconds)

    # ------------------------------------------------------------------ talking to the broker
    async def _call(self, path: str, body: dict[str, Any], timeout: float) -> Reply | None:
        """One request, and what the answer means for the relay as a whole. A usable answer is returned.
        The ones that end the call finish the relay (401: the key is not accepted; 410: the call has ended)
        and return None; so do the ones that were waited out (429, an error, a 5xx): the caller asks again."""
        try:
            reply = await self.broker.post(path, body, timeout)
        except Unreachable as e:
            if not self.stop.is_set():
                await self._lost(f"the broker could not be reached ({e})")
            return None
        if reply.status >= 500:
            await self._lost(f"the broker answered {reply.status}")
            return None
        self._down_since, self._pause = None, BACKOFF_FIRST_S
        if reply.status == 401:
            word = _word(reply.body.get("error"))
            self.finish(
                Exit.REFUSED, "the broker did not accept the call key" + (f" ({word})" if word else "")
            )
            return None
        if reply.status == 410:
            self.finish(Exit.ENDED)
            return None
        if reply.status == 429:
            wait = min(max(reply.retry_after or 1.0, 1.0), RETRY_AFTER_MAX_S)
            await self.sleep(wait)
            return None
        return reply

    async def _lost(self, what: str) -> None:
        """The broker is not answering: wait a growing pause, and give up once it has been so for as long as
        the broker itself would wait before it ended the call."""
        now = self._now()
        if self._down_since is None:
            self._down_since = now
            self.say(f"{what}; trying again")
        if now - self._down_since >= QUIET_S:
            self.finish(Exit.SERVER_LOST)
            return
        await self.sleep(self._pause)
        self._pause = min(self._pause * 2, BACKOFF_MAX_S)

    # ------------------------------------------------------------------ the run
    async def run(self) -> Exit:
        """Join, relay until the call ends, leave. Returns why it stopped."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.finish, Exit.STOPPED)
        self.say(f"key {self.fingerprint} (the room's call bar shows the same)")
        jy: Joinly | None = None
        tasks: list[asyncio.Task[None]] = []
        pump: asyncio.Task[None] | None = None
        try:
            hints, voice = await self._first_poll()
            if self.stop.is_set():
                return self._code()
            jy = self.open_joinly(hints, voice)
            # the voice loop runs from the start: a call ended from the room while the relay is still joining
            # is heard, and the join is dropped
            tasks.append(asyncio.create_task(self._speech_loop()))
            if await self._start(jy):
                pump = asyncio.create_task(self._pump(jy))
                tasks += [pump, asyncio.create_task(self._speaker(jy))]
            await self.stop.wait()
            if self.exit is Exit.STOPPED and pump is not None:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
                await self._final_push(jy)
        except asyncio.CancelledError:
            self.finish(Exit.STOPPED)
            raise
        except Exception as e:
            self.finish(Exit.JOINLY_LOST, f"stopped on an error ({type(e).__name__})")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._depart()
            self.broker.abort()
            if jy is not None:
                await self._leave(jy)
            self._burn(self.cfg.key_path)
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)
        return self._code()

    async def _depart(self) -> None:
        """Best effort once, including after the room ended it (410 is harmless). Never retry shutdown
        or replace the original exit reason; a broker out of reach still has its silence timeout."""
        reason = {
            Exit.STOPPED: "stopped",
            Exit.JOIN_FAILED: "join_failed",
            Exit.JOINLY_LOST: "joinly_lost",
            Exit.ENDED: "ended",
        }.get(self._code(), "left")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.broker.post(LEAVE_PATH, {"reason": reason}, DEPARTURE_S), DEPARTURE_S)

    def _code(self) -> Exit:
        return self.exit if self.exit is not None else Exit.ENDED

    async def _first_poll(self) -> tuple[list[str], str]:
        """The room's names and server voice, before joinly opens. Answered at once (``wait`` 0).
        An empty list and the fallback voice when the relay is stopping.
        """
        while not self.stop.is_set():
            reply = await self._call(SPEECH_PATH, {"wait": 0}, REQUEST_TIMEOUT_S)
            if reply is None:
                continue
            if reply.status != 200:  # a broker that does not have hosted calls answers 404 or 405
                self.finish(
                    Exit.USAGE,
                    f"the broker did not take the key's first request ({reply.status}):"
                    " is it a switchboard that hosts calls on a person's machine?",
                )
                return [], self.cfg.voice
            if reply.body.get("ended"):
                self.finish(Exit.ENDED)
            voice = reply.body.get("tts_voice")
            # Older brokers omit the field. Keep --voice as their fallback; never give joinly an
            # unchecked setting from a response (it would ignore the entire settings header).
            if not isinstance(voice, str) or not re.fullmatch(r"[a-z0-9_]{1,32}", voice):
                voice = self.cfg.voice
            hints = reply.body.get("hints")
            words = [w for w in hints if isinstance(w, str)] if isinstance(hints, list) else []
            return words, voice
        return [], self.cfg.voice

    async def _start(self, jy: Joinly) -> bool:
        """Connect to joinly (it may still be starting), join the call, and mark the broker's call live with
        an empty push. False when the relay is stopping, or joins no call on purpose."""
        began = self._now()
        while not self.stop.is_set():
            try:
                await jy.open()
                self.say("joinly is ready")
                break
            except Exception:
                if self._now() - began >= JOINLY_START_S:
                    self.finish(Exit.JOINLY_LOST, "joinly could not be reached")
                    return False
                await self.sleep(2.0)
        if self.stop.is_set():
            return False
        if not self.cfg.join or self._link is None:
            self.say("started; joining no call (--no-join)")
            return False
        self.say("joining the call")
        self._joining = True
        joining = asyncio.ensure_future(jy.join(self._link.reveal(), self.cfg.participant, JOIN_TIMEOUT_S))
        stopping = asyncio.ensure_future(self.stop.wait())
        try:
            await asyncio.wait({joining, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
        if not joining.done():  # the call was ended while the relay was still joining it
            joining.cancel()
            await asyncio.gather(joining, stopping, return_exceptions=True)
            return False
        if joining.exception() is not None:
            self.finish(Exit.JOIN_FAILED)
            return False
        self._link = None  # joined once: the link is not needed again, here or on disk
        self._burn(self.cfg.link_path)
        self.say("in the call")
        # the first push, even an empty one, marks the call live: asked again until the broker has it, because
        # a call nobody speaks in would otherwise stay "joining"
        while not self.stop.is_set() and not await self._push([]):
            pass
        return not self.stop.is_set()

    async def _leave(self, jy: Joinly) -> None:
        if self._joining:
            with contextlib.suppress(Exception):
                await jy.leave()
        with contextlib.suppress(Exception):
            await jy.close()

    def _burn(self, path: str | None) -> None:
        """Delete a file of the call's (the link, once joined; the key, at the end) when asked to. A call that
        has ended has no use for either, and the link's passcode should not outlive its need."""
        if self.cfg.burn and path:
            with contextlib.suppress(OSError):
                os.unlink(path)

    # ------------------------------------------------------------------ the transcript, pushed
    async def _push(self, segments: list[dict[str, Any]]) -> bool:
        """One push. True when the broker took it (or refused it whole, which the same batch would be again).
        False when it must be asked again: the caller keeps the segments, and the broker skips any it had
        already taken."""
        reply = await self._call(PUSH_PATH, {"segments": segments}, REQUEST_TIMEOUT_S)
        if reply is None:
            return False
        if reply.status == 200:
            if reply.body.get("ended"):
                self.finish(Exit.ENDED)
            return True
        self.say(f"the broker refused {len(segments)} segments ({reply.status}); skipping them")
        return True

    def _batch(self) -> list[dict[str, Any]]:
        return [self._pending[i] for i in range(min(BATCH_MAX, len(self._pending)))]

    async def _flush(self) -> None:
        """Push what is waiting, a batch at a time, until it is all taken or the relay is stopping."""
        while self._pending and not self.stop.is_set():
            batch = self._batch()
            if await self._push(batch):
                for _ in batch:
                    self._pending.popleft()

    def _take(self, segs: list[Segment]) -> None:
        for seg in segs:
            key = (seg.start, seg.end, seg.text, seg.speaker, seg.role)
            if key in self._seen:
                continue
            self._seen.add(key)
            if seg.role != "participant" or not seg.text.strip():
                continue  # the bot's own speech is not what the call said
            self._pending.append(
                {"speaker": seg.speaker, "text": seg.text, "start": seg.start, "end": seg.end}
            )
            if len(self._pending) > PENDING_MAX:
                self._pending.popleft()
                if not self._warned_pending:
                    self._warned_pending = True
                    self.say("the broker takes segments slower than the call makes them; dropping the oldest")

    async def _pump(self, jy: Joinly) -> None:
        """Read the transcript when joinly says it changed (or every ``POLL_S``) and push what is new."""
        while not self.stop.is_set():
            try:
                self._take(await jy.segments())
            except Exception:
                self.finish(Exit.JOINLY_LOST, "joinly dropped the call")
                return
            await self._flush()
            await jy.changed(self.stop, joinly.POLL_S)

    async def _final_push(self, jy: Joinly) -> None:
        """A relay told to stop (not one the call ended on) reads the transcript once more, so the end of
        what was said is not lost, and pushes it if the broker answers within ``FINAL_PUSH_S``."""
        try:
            await asyncio.wait_for(self._last(jy), FINAL_PUSH_S)
        except Exception:
            self.say("the last words were not pushed")

    async def _last(self, jy: Joinly) -> None:
        self._take(await jy.segments())
        while self._pending:
            batch = self._batch()
            reply = await self.broker.post(PUSH_PATH, {"segments": batch}, FINAL_PUSH_S)
            if reply.status != 200:
                return
            for _ in batch:
                self._pending.popleft()

    # ------------------------------------------------------------------ the voice
    async def _speech_loop(self) -> None:
        """Long-poll the broker for what to say, the voice switch and the end."""
        while not self.stop.is_set():
            began = self._now()
            reply = await self._call(SPEECH_PATH, {"wait": SPEECH_WAIT_S}, SPEECH_WAIT_S + REQUEST_TIMEOUT_S)
            if reply is not None:
                self._heard(reply.body)
            gap = SPEECH_MIN_GAP_S - (self._now() - began)
            if gap > 0 and not self.stop.is_set():
                await self.sleep(gap)

    def _heard(self, body: dict[str, Any]) -> None:
        if body.get("ended"):
            self.finish(Exit.ENDED)
        if not body.get("voice"):
            self._speech.clear()  # voice is off: what was waiting is dropped, the line being said finishes
        lines = body.get("lines")
        for line in lines if isinstance(lines, list) else []:
            if isinstance(line, str) and line.strip() and body.get("voice"):
                self._speech.append(line)
        if self._speech:
            self._speech_ready.set()

    async def _speaker(self, jy: Joinly) -> None:
        """Say the waiting lines one after another. ``speak_text`` returns when the speech has ended."""
        while not self.stop.is_set():
            if not self._speech:
                self._speech_ready.clear()
                await self._wait_event(self._speech_ready)
                continue
            line = self._speech.popleft()
            try:
                said = await jy.speak(line)
            except Exception:
                self.say("could not speak in the call")
                self._speech.clear()
                continue
            if said.startswith(joinly.INTERRUPTED):
                self.say("someone spoke over a line, so it stopped early")

    async def _wait_event(self, ev: asyncio.Event) -> None:
        waiting = asyncio.ensure_future(ev.wait())
        stopping = asyncio.ensure_future(self.stop.wait())
        try:
            await asyncio.wait({waiting, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (waiting, stopping):
                t.cancel()
            await asyncio.gather(waiting, stopping, return_exceptions=True)


def _word(value: Any) -> str:
    """A server's error word (``unknown_key``) kept to letters, digits, ``_`` and ``-``, and 32 of them."""
    return "".join(c for c in value if c.isalnum() or c in "_-")[:32] if isinstance(value, str) else ""


def _print(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def real_joinly(url: str, cfg: RelayConfig) -> OpenJoinly:
    """The factory a real run gives ``Relay``: a ``JoinlyClient`` whose settings carry the room's names
    and selected voice from the broker's first answer."""

    def make(hints: Sequence[str], voice: str) -> Joinly:
        header = joinly.settings_header(
            hotwords=hints, model=cfg.model, voice=voice, participant=cfg.participant
        )
        return JoinlyClient(url, header)

    return make


def main(
    *,
    server: str,
    joinly_url: str,
    key_file: str,
    link_file: str | None,
    model: str = "",
    voice: str = DEFAULT_VOICE,
    participant: str = joinly.PARTICIPANT,
    join: bool = True,
    allow_http: bool = False,
    burn: bool = False,
    no_mode_check: bool = False,
) -> int:
    """``switchboard meeting relay``: read the key and the link, run the relay, return its exit code. The
    arguments are the command line's (``cli.py``); a key or link that cannot be used is ``Exit.USAGE``."""
    joinly.install_redact()
    try:
        if join and link_file is None:
            raise UsageError("a call to join needs --link-file (or --no-join, to join none)")
        key, link = read_secrets(key_file, link_file if join else None, mode_check=not no_mode_check)
        broker = BrokerClient(server, key, allow_http=allow_http)
    except UsageError as e:
        print(f"switchboard: meeting relay: {e}", file=sys.stderr)
        return int(Exit.USAGE)
    cfg = RelayConfig(
        model=model,
        voice=voice,
        participant=participant,
        join=join,
        burn=burn,
        key_path=None if key_file == "-" else key_file,
        link_path=None if link_file in (None, "-") else link_file,
    )
    relay = Relay(cfg, key, link, broker, real_joinly(joinly_url, cfg))
    return int(asyncio.run(relay.run()))
