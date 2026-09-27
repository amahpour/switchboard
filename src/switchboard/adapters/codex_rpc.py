"""A minimal JSON-RPC client for the Codex app-server (DESIGN.md §9.3).

Transport: WebSocket over a Unix socket (the daemon control socket, or a
``codex app-server --listen unix://PATH``), one JSON message per text frame,
JSON-RPC 2.0 without the ``jsonrpc`` field (FINDINGS §4a). Derived from the
M0 experiment clients.

What makes it safe to point at a user's daemon (FINDINGS §11):
- **Method allowlist.** Only ``initialize``, ``initialized``, ``thread/read``,
  ``thread/loaded/list``, ``turn/start`` and ``turn/steer`` can be sent; any
  other method raises before anything is written.
- **Parameter allowlists.** ``turn/start`` carries exactly ``threadId``,
  ``input`` and ``clientUserMessageId``; ``turn/steer`` exactly those plus
  ``expectedTurnId``. No override field (model, approval or sandbox policy,
  cwd, ...) can be sent: they would persist on the user's session. The keys
  are also checked against ``guardrails.CODEX_OVERRIDE_FIELDS``.
- **Never answers a server request** (an approval, a user-input request):
  such messages are counted by method name and dropped. The client never
  subscribes to a thread (no resume), so an unsubscribed connection is not
  even sent approval requests.
- **Never logs thread contents.** Responses are parsed in memory; only method
  names, error codes and counts reach the log.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import stat
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from switchboard import __version__, guardrails

log = logging.getLogger("switchboard.codex.rpc")

ALLOWED_METHODS = frozenset(
    {"initialize", "initialized", "thread/read", "thread/loaded/list", "turn/start", "turn/steer"}
)
# Keys each method may carry. turn/start and turn/steer must carry exactly these.
PARAM_KEYS: dict[str, frozenset[str]] = {
    "initialize": frozenset({"clientInfo", "capabilities"}),
    "initialized": frozenset(),
    "thread/read": frozenset({"threadId", "includeTurns"}),
    "thread/loaded/list": frozenset({"cursor"}),
    "turn/start": frozenset({"threadId", "input", "clientUserMessageId"}),
    "turn/steer": frozenset({"threadId", "expectedTurnId", "input", "clientUserMessageId"}),
}
EXACT_KEYS = frozenset({"turn/start", "turn/steer"})
MAX_FRAME = 64 * 1024 * 1024  # thread/read with turns can be large
CONNECT_TIMEOUT_S = 5.0
REQUEST_TIMEOUT_S = 10.0
INVALID_REQUEST = -32600  # e.g. "no active turn to steer", "expected active turn id ..."


class ForbiddenRpc(Exception):
    """A method or parameter outside the allowlists: nothing was sent."""


class RpcError(Exception):
    """An error response. Only the numeric code is safe to log."""

    def __init__(self, code: Any, message: Any = ""):
        super().__init__(f"codex rpc error {code}")
        self.code = code if isinstance(code, int) and not isinstance(code, bool) else None
        self.message = message if isinstance(message, str) else ""


class SocketRefused(Exception):
    """The control socket is missing or not safely ours."""


# ------------------------------------------------------------------ params
def text_input(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text, "text_elements": []}]


def initialize_params() -> dict[str, Any]:
    return {
        "clientInfo": {"name": "switchboard", "title": "switchboard", "version": __version__},
        "capabilities": {"experimentalApi": False},
    }


def thread_read_params(thread_id: str, include_turns: bool = False) -> dict[str, Any]:
    return {"threadId": thread_id, "includeTurns": bool(include_turns)}


def loaded_list_params(cursor: str | None = None) -> dict[str, Any]:
    return {"cursor": cursor} if cursor else {}


def turn_start_params(thread_id: str, text: str, client_id: str) -> dict[str, Any]:
    """Exactly threadId, input, clientUserMessageId: never an override field."""
    return {"threadId": thread_id, "input": text_input(text), "clientUserMessageId": client_id}


def turn_steer_params(thread_id: str, turn_id: str, text: str, client_id: str) -> dict[str, Any]:
    return {"threadId": thread_id, "expectedTurnId": turn_id, "input": text_input(text),
            "clientUserMessageId": client_id}


def check_request(method: str, params: Any) -> None:
    """Raise ForbiddenRpc unless (method, params) is inside the allowlists."""
    if method not in ALLOWED_METHODS:
        raise ForbiddenRpc(f"method not allowed: {method}")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ForbiddenRpc("params must be an object")
    keys = set(params)
    allowed = PARAM_KEYS[method]
    if keys - allowed:
        raise ForbiddenRpc(f"{method}: keys not allowed: {sorted(keys - allowed)}")
    if method in EXACT_KEYS and keys != allowed:
        raise ForbiddenRpc(f"{method}: must carry exactly {sorted(allowed)}")
    if keys & set(guardrails.CODEX_OVERRIDE_FIELDS):
        raise ForbiddenRpc(f"{method}: override field")
    if method in EXACT_KEYS:
        inp = params.get("input")
        if (not isinstance(inp, list) or len(inp) != 1 or not isinstance(inp[0], dict)
                or set(inp[0]) != {"type", "text", "text_elements"} or inp[0]["type"] != "text"
                or not isinstance(inp[0]["text"], str) or inp[0]["text_elements"] != []):
            raise ForbiddenRpc(f"{method}: input must be one plain text item")
        for k in keys - {"input"}:
            if not isinstance(params[k], str) or not params[k] or len(params[k]) > 200:
                raise ForbiddenRpc(f"{method}: {k} must be a short string")
    if method == "initialize":
        caps = params.get("capabilities") or {}
        if not isinstance(caps, dict) or caps.get("experimentalApi") is not False or set(caps) - {"experimentalApi"}:
            raise ForbiddenRpc("initialize: only experimentalApi=false")
    if method == "thread/read":
        if not isinstance(params.get("threadId"), str) or not isinstance(params.get("includeTurns", False), bool):
            raise ForbiddenRpc("thread/read: bad params")


# ------------------------------------------------------------------ socket
def check_socket(path: str | os.PathLike[str]) -> str:
    """The resolved socket path, if it is a socket we own in a directory only we
    can write (the daemon's control socket is a symlink into a 0700 dir).
    Raises SocketRefused otherwise."""
    p = os.path.expanduser(str(path))
    if not os.path.isabs(p):
        raise SocketRefused("control socket path must be absolute")
    uid = os.getuid()
    try:
        lst = os.lstat(p)
        real = os.path.realpath(p)
        st = os.stat(real)
        parent = os.stat(os.path.dirname(real))
    except OSError:
        raise SocketRefused("control socket not found") from None
    if lst.st_uid != uid or st.st_uid != uid:
        raise SocketRefused("control socket is not owned by this user")
    if not stat.S_ISSOCK(st.st_mode):
        raise SocketRefused("control socket path is not a socket")
    if parent.st_uid not in (uid, 0) or parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX:
        raise SocketRefused("control socket directory is writable by others")
    return real


# ------------------------------------------------------------------ client
NoteFn = Callable[[str, dict[str, Any], float], None]


class CodexRpc:
    """One connection. ``on_notification(method, params, t)`` sees notifications."""

    def __init__(self, path: str | os.PathLike[str], *, on_notification: NoteFn | None = None,
                 clock: Callable[[], float] = time.time):
        self.path = str(path)
        self.on_notification = on_notification
        self.clock = clock
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self.closed_event = asyncio.Event()
        self.notes: Counter[str] = Counter()  # notification method -> count
        self.server_requests: Counter[str] = Counter()  # never answered
        self.init_result: dict[str, Any] | None = None

    @property
    def closed(self) -> bool:
        return self.closed_event.is_set()

    async def connect(self, timeout: float = CONNECT_TIMEOUT_S) -> dict[str, Any]:
        from websockets.asyncio.client import unix_connect

        check_socket(self.path)
        self._ws = await asyncio.wait_for(
            unix_connect(self.path, uri="ws://localhost/", max_size=MAX_FRAME, ping_interval=None,
                         compression=None, proxy=None, open_timeout=timeout, close_timeout=1),
            timeout)
        self._reader = asyncio.get_running_loop().create_task(self._read())
        try:
            res = await self.request("initialize", initialize_params(), timeout=timeout)
            await self.notify("initialized")
        except BaseException:
            await self.close()
            raise
        self.init_result = res if isinstance(res, dict) else {}
        return self.init_result

    async def _read(self) -> None:
        try:
            async for frame in self._ws:
                t = self.clock()
                try:
                    msg = json.loads(frame)
                except (ValueError, TypeError):
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg, t)
        except Exception as e:  # connection closed or broken
            log.debug("codex rpc reader ended: %s", type(e).__name__)
        finally:
            self.closed_event.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("codex app-server connection closed"))
            self._pending.clear()

    def _dispatch(self, msg: dict[str, Any], t: float) -> None:
        method = msg.get("method")
        if "id" in msg and method is None:
            fut = self._pending.pop(msg["id"], None) if isinstance(msg["id"], int) else None
            if fut is not None and not fut.done():
                err = msg.get("error")
                if isinstance(err, dict):
                    fut.set_exception(RpcError(err.get("code"), err.get("message")))
                else:
                    fut.set_result(msg.get("result"))
            return
        if not isinstance(method, str):
            return
        name = method[:80]
        if "id" in msg:
            # A server -> client request (an approval, a user-input question):
            # never answered, by design. Only its method name is recorded.
            self.server_requests[name] += 1
            log.info("codex app-server request %s left unanswered", name)
            return
        self.notes[name] += 1
        if self.on_notification is not None:
            params = msg.get("params")
            try:
                self.on_notification(name, params if isinstance(params, dict) else {}, t)
            except Exception:
                log.exception("codex notification handler failed (%s)", name)

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._ws is None or self.closed:
            raise ConnectionError("codex app-server connection closed")
        await self._ws.send(json.dumps(msg, separators=(",", ":")))

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      timeout: float = REQUEST_TIMEOUT_S) -> Any:
        check_request(method, params)
        rid = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg: dict[str, Any] = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            await self._send(msg)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        check_request(method, params)
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), 2)
            except Exception:
                pass
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        self.closed_event.set()

    # ----------------------------------------------------------- helpers
    async def loaded_threads(self, timeout: float = REQUEST_TIMEOUT_S) -> set[str]:
        """Every thread id the server has loaded (follows ``nextCursor``)."""
        out: set[str] = set()
        cursor: str | None = None
        for _ in range(50):
            res = await self.request("thread/loaded/list", loaded_list_params(cursor), timeout=timeout)
            data = res.get("data") if isinstance(res, dict) else None
            for tid in data or []:
                if isinstance(tid, str):
                    out.add(tid)
            cursor = res.get("nextCursor") if isinstance(res, dict) else None
            if not isinstance(cursor, str) or not cursor:
                break
        return out

    async def read_thread(self, thread_id: str, include_turns: bool = False,
                          timeout: float = REQUEST_TIMEOUT_S) -> dict[str, Any]:
        res = await self.request("thread/read", thread_read_params(thread_id, include_turns), timeout=timeout)
        th = res.get("thread") if isinstance(res, dict) else None
        return th if isinstance(th, dict) else {}


async def one_shot(path: str, fn: Callable[[CodexRpc], Any], *, timeout: float = REQUEST_TIMEOUT_S) -> Any:
    """Open a fresh, never-subscribed connection, run ``await fn(rpc)``, close it."""
    rpc = CodexRpc(path)
    await rpc.connect(timeout=min(timeout, CONNECT_TIMEOUT_S))
    try:
        return await fn(rpc)
    finally:
        await rpc.close()


# ------------------------------------------------------------ thread views
KNOWN_STATUS_TYPES = frozenset({"idle", "active", "notLoaded", "systemError"})


def thread_status(status: Any) -> str | None:
    """Map an app-server ThreadStatus to a switchboard status (DESIGN §9.3).

    Fails closed: ``active`` with **any** flag (0.156.1 knows only
    ``waitingOnApproval`` and ``waitingOnUserInput``; a later version may add
    another wait) and a status type this version doesn't know are both a hold
    (``waiting-approval``). None only when there is no status object at all."""
    if not isinstance(status, dict):
        return None
    t = status.get("type")
    if t == "idle":
        return "idle"
    if t == "active":
        flags = status.get("activeFlags")
        if flags is None or flags == []:
            return "busy"
        return "waiting-approval"
    if t in ("notLoaded", "systemError"):
        return "offline"
    return "waiting-approval"


def active_turn_id(thread: dict[str, Any]) -> str | None:
    """The id of the thread's in-progress turn (the last one), if any."""
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for t in reversed(turns):
        if isinstance(t, dict) and t.get("status") == "inProgress" and isinstance(t.get("id"), str):
            return t["id"]
    return None


def contains(thread: dict[str, Any], needle: str) -> bool:
    """In-memory search of a thread's serialized contents (never logged)."""
    try:
        return needle in json.dumps(thread, ensure_ascii=False)
    except (TypeError, ValueError):
        return False


PROOF_SERVER = "switchboard"  # the MCP server name `switchboard install codex` writes
PROOF_TOOL = "join"


def join_proven(thread: dict[str, Any], needle: str) -> bool:
    """The thread proof (DESIGN §9.3): ``needle`` (``yk:j<nonce>``) must be in
    the **result** of a completed ``mcpToolCall`` item of switchboard's own ``join``
    tool, not merely anywhere in the thread (a file the model printed, a
    command's output or a queued prompt could carry it). In memory only."""
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return False
    for turn in turns:
        items = turn.get("items") if isinstance(turn, dict) else None
        if not isinstance(items, list):
            continue
        for it in items:
            if (isinstance(it, dict) and it.get("type") == "mcpToolCall" and it.get("server") == PROOF_SERVER
                    and it.get("tool") == PROOF_TOOL and it.get("status") == "completed"
                    and isinstance(it.get("result"), dict) and contains(it["result"], needle)):
                return True
    return False


_VERSION_RE = re.compile(r"/(\d+\.\d+\.\d+)")


def server_version(init_result: dict[str, Any] | None) -> str | None:
    """The app-server version from ``initialize``'s ``userAgent`` (e.g. ``…/0.156.1 …``)."""
    ua = (init_result or {}).get("userAgent")
    if not isinstance(ua, str):
        return None
    m = _VERSION_RE.search(ua)
    return m.group(1) if m else None
