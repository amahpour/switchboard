"""Agents over HTTPS (DESIGN.md §49.2, §49.3, #382): switchboard's agent tools at ``/mcp``.

The broker serves the stdio server's own tools (``mcp/server.py``'s ``build_server``) over
Streamable HTTP, to an agent that presents an agent token (§49.1), so nothing of switchboard's
runs on the agent's machine.

- **In front of /mcp** stands ``AgentAuth``. A request with an ``Origin`` is refused: agents send
  none, and a browser always does. One without a live agent token gets 401. An MCP session
  (``Mcp-Session-Id``, Codex's 2025-06-18 handshake) belongs to the token that opened it. A
  token's last use is written at most once a minute.
- **Who is calling** (``HttpTools.prime``): the token's person and device, the device being the
  host of its agents' sessions (§27.6). The harness is the client's own claim, from its
  ``clientInfo`` (``codex-mcp-client`` is Codex). The session is the harness's own: Codex names
  its thread on every call (``_meta.threadId``). Any other client is told it isn't supported yet,
  since its calls couldn't be told apart (Claude Code comes next, through its hooks).
- **No credentials.** A session's memberships are read from the store on every call, so a broker
  restart drops nobody. A call names its membership (``m:<id>``), and ``AgentService`` checks it
  against the caller's own session.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastmcp import Context
from fastmcp.server.dependencies import get_http_request
from starlette.routing import Route

from switchboard import __version__
from switchboard.broker import agent_tokens
from switchboard.broker.agents import McpConn
from switchboard.broker.peer import McpIdentity
from switchboard.broker.service import ServiceError
from switchboard.mcp.server import (
    CLIENT_INFO_META_KEY,
    TOOL_NAMES,
    WAIT_CAPS,
    McpState,
    build_server,
    err,
)
from switchboard.models import AgentTokenRow, InvalidName, normalize_room

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from switchboard.broker.app import BrokerState

log = logging.getLogger("switchboard.agent_http")

PATH = "/mcp"
# the clients whose sessions a call over HTTPS can name, by their clientInfo name (§49.3)
CLIENTS = {"codex-mcp-client": "codex"}
UNSUPPORTED = (
    "switchboard over HTTPS works with Codex for now; Claude Code comes next (#382)."
    " Connect this agent with `switchboard install` meanwhile"
)
SESSIONS_MAX = 4096  # MCP sessions remembered, each with the token that opened it
TOUCH_S = 60.0  # a token's last use is written at most this often
# the AgentService method behind each of the tools' calls (the socket's method table, §5.2)
METHODS = {
    "agent.join": "join",
    "agent.leave": "leave",
    "agent.who": "who",
    "agent.say": "say",
    "agent.review": "review",
    "agent.claim": "claim",
    "agent.read": "read",
    "agent.wait": "wait",
    "agent.pass": "pass_",
    "agent.away": "away",
    "agent.status": "status",
}

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(eq=False)
class HttpConn:
    """One call over HTTPS, as ``AgentService`` sees a connection (§49.3): the token's device is
    its host, and nothing about it is a process. A fresh one per call, so its id keys only that
    call's own ``wait()``."""

    id: int
    host: str
    mcp: McpConn
    http: bool = True
    remote: bool = False
    closed: bool = False
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Caller:
    token: AgentTokenRow
    harness: str
    thread: str | None
    conn: HttpConn


def client_info(ctx: Context | None) -> dict[str, str]:
    """The client's name and version, as it says (a claim, cut to 80 characters): from this
    request's ``_meta`` (2026-07-28), else from its session's ``initialize`` (2025-06-18)."""
    raw: Any = McpState.meta(ctx).get(CLIENT_INFO_META_KEY)
    if not isinstance(raw, dict) and ctx is not None:
        try:
            params = ctx.session.client_params
        except Exception:  # no session behind this context
            params = None
        info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
        if info is not None and hasattr(info, "model_dump"):
            raw = info.model_dump(by_alias=True, exclude_none=True)
    if not isinstance(raw, dict):
        return {}
    return {k: str(v)[:80] for k, v in raw.items() if k in ("name", "version")}


class HttpTools:
    """``build_server``'s backend inside the broker (``mcp.server.ToolBackend``), one for every
    agent over HTTPS. Each tool call's caller is worked out by ``prime``, from the request the call
    came on, and kept in a context variable for the rest of that call."""

    def __init__(self, state: "BrokerState") -> None:
        self.state = state
        self._caller: ContextVar[Caller | None] = ContextVar("switchboard_http_caller", default=None)
        self._ids = itertools.count(1 << 48)  # connection ids apart from the socket's

    @property
    def harness(self) -> str | None:
        c = self._caller.get()
        return c.harness if c is not None else None

    @property
    def creds(self) -> dict[tuple[str | None, str], str]:
        """The caller's memberships, from the store: ``(thread id, room) -> "m:<id>"``. A fresh
        dict each time, so what a tool writes into it goes nowhere, and nothing needs to."""
        c = self._served()
        if c is None:
            return {}
        try:
            rooms = self.state.agents.http_rooms(c.conn, c.thread)
        except ServiceError:
            return {}
        return {(c.thread, room.name): f"m:{m.id}" for room, m in rooms}

    @property
    def codex_threads(self) -> dict[str, str]:
        """Nothing kept: a thread's join code is for its proof before a wake on its own machine
        (§27.7), and nothing here wakes a thread."""
        return {}

    def _served(self) -> Caller | None:
        c = self._caller.get()
        return c if c is not None and c.harness in CLIENTS.values() else None

    async def prime(self, ctx: Context | None) -> None:
        try:
            token = get_http_request().scope.get("state", {}).get("agent_token")
        except RuntimeError:  # not over HTTP at all
            token = None
        if not isinstance(token, AgentTokenRow):
            self._caller.set(None)
            return
        info = client_info(ctx)
        harness = CLIENTS.get(info.get("name", ""), "unknown")
        ident = McpIdentity(
            harness=harness,
            mcp_pid=0,
            mcp_start=0.0,
            agent_pid=None,
            agent_start=None,
            evidence=f"agent token for {token.device}",
            host=token.device,
        )
        mc = McpConn(
            ident=ident,
            client_info=info,
            evidence={"via": "agent token"},
            version=__version__,
            tools=TOOL_NAMES,
        )
        conn = HttpConn(next(self._ids), token.device, mc)
        self._caller.set(Caller(token, harness, self.thread_id(ctx), conn))
        self.state.agents.http_claims(token.device, TOOL_NAMES)

    def thread_id(self, ctx: Context | None) -> str | None:
        """Codex: ``_meta.threadId`` of this call (one MCP session serves many threads)."""
        v = McpState.meta(ctx).get("threadId")
        return v if isinstance(v, str) and 0 < len(v) <= 128 else None

    def wait_cap(self) -> int:
        return WAIT_CAPS.get(self.harness or "unknown", 50)

    async def call(self, method: str, params: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        c = self._caller.get()
        if c is None:
            return {"ok": False, "error": "no agent token on this call", "code": "unauthorized"}
        if self._served() is None:
            return {"ok": False, "error": UNSUPPORTED, "code": "unsupported"}
        name = METHODS.get(method)
        if name is None:
            return {"ok": False, "error": f"unknown method {method}", "code": "bad_request"}
        try:
            res = getattr(self.state.agents, name)(c.conn, params)
            if asyncio.iscoroutine(res):
                res = await asyncio.wait_for(res, timeout)
        except ServiceError as e:
            return {"ok": False, "error": e.message, "code": e.code}
        except TimeoutError:
            return {"ok": False, "error": "switchboard took too long to answer", "code": "timeout"}
        return {"ok": True, **res}

    async def find_cred(self, room: str, tid: str | None) -> tuple[str, str] | str:
        """The caller's membership in ``room``, as ``(canonical room, "m:<id>")``, or an error
        for the tool's result."""
        try:
            name = normalize_room(room)
        except InvalidName as e:
            return err(str(e), "bad_request")
        c = self._served()
        if c is None:
            return err(UNSUPPORTED if self._caller.get() is not None else "no agent token", "unsupported")
        try:
            got = self.state.agents.http_room(c.conn, name, tid)
        except ServiceError as e:
            return err(e.message, e.code)
        if got is None:
            return err(f'you are not in {name}: call join("{name}", your_screen_name) first', "not_member")
        r, m = got
        return r.name, f"m:{m.id}"

    def base(self, cred: str, tid: str | None) -> dict[str, Any]:
        p: dict[str, Any] = {"cred": cred}
        if tid:
            p["thread_id"] = tid
        return p

    def forget(self, res: dict[str, Any], key: tuple[str | None, str]) -> None:
        """Nothing to forget: memberships are read from the store on every call."""

    def unwait(self, params: dict[str, Any]) -> None:
        c = self._served()
        if c is None:
            return
        agents: Any = self.state.agents  # an HttpConn stands in for the socket's Conn
        try:
            agents.unwait(c.conn, params)
        except ServiceError:
            pass  # the membership or the wait ended meanwhile


async def _reply(send: Send, status: int, data: dict[str, Any], *extra: tuple[bytes, bytes]) -> None:
    body = json.dumps(data).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                *extra,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k.lower() == name:
            return str(v.decode("latin-1"))
    return None


class AgentAuth:
    """In front of ``/mcp`` (§49.2): no browser, a live agent token, and an MCP session used only
    with the token that opened it."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]], state: "BrokerState"):
        self.app = app
        self.state = state
        self.sessions: OrderedDict[str, int] = OrderedDict()  # Mcp-Session-Id -> token id
        self.touched: dict[int, float] = {}  # token id -> when its last use was written

    def token(self, scope: Scope) -> AgentTokenRow | None:
        scheme, _, value = (_header(scope, b"authorization") or "").partition(" ")
        value = value.strip()
        if scheme.lower() != "bearer" or not agent_tokens.TOKEN_RE.fullmatch(value):
            return None
        return self.state.store.agent_token_by_hash(agent_tokens.token_hash(value))

    def touch(self, row: AgentTokenRow) -> None:
        now = self.state.clock.now()
        if now - self.touched.get(row.id, float("-inf")) >= TOUCH_S:
            self.touched[row.id] = now
            self.state.store.agent_token_used(row.id)

    def bind(self, session_id: str, token_id: int) -> None:
        self.sessions[session_id] = token_id
        self.sessions.move_to_end(session_id)
        while len(self.sessions) > SESSIONS_MAX:
            self.sessions.popitem(last=False)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if _header(scope, b"origin") is not None:
            await _reply(
                send, 403, {"error": "forbidden", "message": "agents only: a browser can't call /mcp"}
            )
            return
        row = self.token(scope)
        if row is None:
            await _reply(
                send,
                401,
                {
                    "error": "unauthorized",
                    "message": "an agent token is needed: make one in Settings → Agents",
                },
                (b"www-authenticate", b'Bearer realm="switchboard"'),
            )
            return
        sid = _header(scope, b"mcp-session-id")
        if sid is not None and self.sessions.get(sid, row.id) != row.id:
            # another token's session: as if it didn't exist (the client starts a new one)
            await _reply(send, 404, {"error": "not_found", "message": "no such MCP session"})
            return
        self.touch(row)
        scope.setdefault("state", {})["agent_token"] = row

        async def send_binding(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                for k, v in message.get("headers", []):
                    if k.lower() == b"mcp-session-id":
                        self.bind(v.decode("latin-1"), row.id)
            await send(message)

        await self.app(scope, receive, send_binding)
        if sid is not None and scope.get("method") == "DELETE":
            self.sessions.pop(sid, None)


def install(app: "FastAPI", state: "BrokerState") -> Any:
    """Serve the tools at ``PATH`` behind ``AgentAuth``. Returns the MCP app, whose lifespan runs
    its session manager: the broker's own lifespan enters it."""
    server = build_server(HttpTools(state))
    mcp_app = server.http_app(path=PATH)
    inner = next(r.endpoint for r in mcp_app.routes if isinstance(r, Route) and r.path == PATH)
    app.router.routes.append(Route(PATH, endpoint=AgentAuth(inner, state)))
    return mcp_app
