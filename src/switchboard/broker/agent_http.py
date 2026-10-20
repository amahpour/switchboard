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
  ``clientInfo`` (Codex, Claude Code, or Cursor). The session is the
  harness's own. Codex names its thread on every call (``_meta.threadId``). Claude Code names
  the call (``_meta["claudecode/toolUseId"]``), and its PreToolUse hook, a call of the hidden
  ``hook`` tool just before, reports which session made that call (``HookUses``). Cursor's
  command hook injects a checked conversation ID into its call's arguments. Any other client
  is told it isn't supported yet, since its calls couldn't be told apart.
- **The hook tool** is left out of ``tools/list``, so the model never sees it, and a hook
  calls it anyway. Each event reports the session's status (§49.4); its answer is written here
  (#380's decision 5), only in the hook script's own shapes (§7.3) plus Codex's Stop
  ``decision: block``, and is ``{}`` when there is nothing to say.
- **The listener** (``/mcp/listen``): Claude Code's Stop hook posts its session id there and waits,
  round after round, and the answer is the message that wakes it (§49.4).
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
from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent, ToolAnnotations
from starlette.routing import Route

from switchboard import __version__
from switchboard.broker import agent_tokens
from switchboard.broker.agents import McpConn
from switchboard.broker.peer import McpIdentity
from switchboard.broker.service import ServiceError
from switchboard.hook import switchboard_hook as hook_script
from switchboard.mcp.server import (
    CLIENT_INFO_META_KEY,
    TOOL_NAMES,
    WAIT_CAPS,
    McpState,
    build_server,
    err,
)
from switchboard.models import AgentTokenRow, InvalidName, canonical_event, normalize_room

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from switchboard.broker.app import BrokerState

log = logging.getLogger("switchboard.agent_http")

PATH = "/mcp"
# the clients whose sessions a call over HTTPS can name, by their clientInfo name (§49.3)
CLIENTS = {
    "codex-mcp-client": "codex",
    "claude-code": "claude",
    "Cursor": "cursor",
    "rmcp": "devin",  # Devin's Rust transport; a matching input hook is still required
}
UNSUPPORTED = (
    "switchboard over HTTPS works with Codex, Claude Code, Cursor and Devin for now."
    " Connect this agent with `switchboard install` meanwhile"
)
HOOK_TOOL = "hook"  # called by a harness's hooks, never listed for the model
# what a hook's input template may name: the hook tool's parameters (the plugins' hooks use these)
HOOK_FIELDS = (
    "event",
    "session_id",
    "tool_use_id",
    "tool_name",
    "turn_id",
    "prompt_id",
    "permission_mode",
    "model",
    "source",
    "reason",
    "stop_hook_active",
    "tool_response",
    "wait_s",
)
LISTEN_PATH = "/mcp/listen"  # Claude Code's listener waits here (§49.4)
SESSION_PATH = "/mcp/session"  # Claude Code's SessionStart and SessionEnd, as command hooks (§49.4)
SESSION_EVENTS = frozenset({"SessionStart", "SessionEnd"})  # Claude Code runs no mcp_tool hook at these
SESSION_FIELDS = ("event", "session_id", "source", "reason", "permission_mode")  # all /mcp/session reads
SESSION_BODY_MAX = 2048
INPUT_SESSION = "switchboard_session_id"
INPUT_USE = "switchboard_tool_use_id"
PRETOOL_PATH = "/mcp/pretool"
EVENT_PATH = "/mcp/event"
SESSION_HEADER = b"x-switchboard-session"  # the listener's session id, the only thing it sends
WAIT_HEADER = b"x-switchboard-wait"  # how long one round of the listener waits
AGAIN_HEADER = b"x-switchboard-again"  # "1" on the listener's later rounds: only an idle session waits on
WAIT_MAX_S = 86400.0  # the longest any one wait is held (a Codex Stop hook may ask for a day)
LISTEN_AGAIN = frozenset({"timeout", "paused"})  # a round that ended so: the listener waits again (408)
TOOL_USE_KEY = "claudecode/toolUseId"  # the call's id in Claude Code's _meta
USES_MAX = 4096  # tool calls whose session a hook reported, remembered at once
USES_TTL_S = 120.0  # how long such a report is kept
HOOK_WAIT_S = 2.0  # how long a Claude Code call waits for its PreToolUse report
ID_MAX = 128  # a session id or a tool call's id, as a harness sends it
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


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= ID_MAX else None


def _flag(value: Any) -> bool | None:
    """A boolean a hook's input template filled in: ``true``/``false`` as text, or a JSON bool."""
    if isinstance(value, bool):
        return value
    return {"true": True, "false": False}.get(value) if isinstance(value, str) else None


def hook_params(event: str, fields: dict[str, Any], now: float) -> dict[str, Any]:
    """What a hook over HTTPS relays, as the hook script's own params (§7.2 step 4): the fields
    its input template names, cut and checked, and the batch tokens in a switchboard tool's own
    result (its hook matches switchboard's tools only, so nothing else of the session is sent).
    ``wait_s``, a number the hook config itself sets, lets a Stop hook wait that long."""
    ev = event.lower()
    ok = False if ev == "posttoolusefailure" else (True if ev == "posttooluse" else None)
    toks: list[list[Any]] = []
    raw = fields.get("tool_response")
    result = raw if isinstance(raw, str) else json.dumps(raw, default=str) if raw else ""
    for m in hook_script.TOKEN_RE.finditer(result[: hook_script.FIELD_SCAN_MAX]):
        if [int(m.group(1)), m.group(2)] not in toks and len(toks) < hook_script.MAX_TOKENS:
            toks.append([int(m.group(1)), m.group(2)])
    wait = fields.get("wait_s")
    try:
        wait_s = float(wait) if wait not in (None, "") and not isinstance(wait, bool) else None
    except (TypeError, ValueError):
        wait_s = None
    p: dict[str, Any] = {
        "sid": _id(fields.get("session_id")),
        "gen": _id(fields.get("turn_id")) or _id(fields.get("prompt_id")),
        "tool": _id(fields.get("tool_name")),
        "tool_use_id": _id(fields.get("tool_use_id")),
        "ok": ok,
        "stop_hook_active": _flag(fields.get("stop_hook_active")),
        "source": hook_script._str(fields.get("source"), 32),
        "reason": hook_script._str(fields.get("reason"), 64),
        "permission_mode": hook_script._str(fields.get("permission_mode"), 32),
        "model": hook_script._model(fields.get("model")),
        "tokens": toks,
        "t": now,
        "max_wait_s": min(wait_s, WAIT_MAX_S) if wait_s is not None and wait_s > 0 else None,
    }
    return {k: v for k, v in p.items() if v is not None and v != []}


def reply_for(harness: str, event: str, out: Any) -> dict[str, Any] | None:
    """A hook's answer, written by the broker (#380's decision 5): the hook script's own shapes
    for context (``render``, §7.3), and Codex's Stop ``decision: block`` for a follow-up. Every
    answer is checked against those exact shapes; anything else answers nothing."""
    if not isinstance(out, dict):
        return None
    text = out.get("text")
    if not isinstance(text, str) or not text:
        return None
    if out.get("kind") == "continue" and harness == "codex" and event == "Stop":
        reply: Any = {"decision": "block", "reason": text}
    else:
        reply = hook_script.render(harness, event, out)
    if harness == "cursor" and reply in ({"additional_context": text}, {"followup_message": text}):
        return reply
    return reply if _shape_ok(reply, event, text) else None


def _shape_ok(reply: Any, event: str, text: str) -> bool:
    """Exactly one of the shapes a hook over HTTPS may answer with, carrying the batch's text."""
    if reply == {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}:
        return True
    return reply == {"decision": "block", "reason": text} and event == "Stop"


class HookUses:
    """Which Claude Code session made each call (§49.3): its PreToolUse hook reports the call's id
    with the session's, just before the call itself arrives (5 ms before, in #380's spike). Kept
    per token, so one device's reports never name another's sessions, for a couple of minutes,
    and at most ``USES_MAX`` of them."""

    def __init__(self, clock: Any) -> None:
        self.clock = clock
        self.uses: OrderedDict[tuple[int, str], tuple[str, float]] = OrderedDict()
        self.arrived = asyncio.Event()

    def report(self, token_id: int, tool_use_id: str, session_id: str) -> None:
        now = self.clock.now()
        self.uses[(token_id, tool_use_id)] = (session_id, now)
        self.uses.move_to_end((token_id, tool_use_id))
        while self.uses and (
            len(self.uses) > USES_MAX or next(iter(self.uses.values()))[1] < now - USES_TTL_S
        ):
            self.uses.popitem(last=False)
        arrived, self.arrived = self.arrived, asyncio.Event()
        arrived.set()

    def get(self, token_id: int, tool_use_id: str) -> str | None:
        got = self.uses.get((token_id, tool_use_id))
        return got[0] if got is not None and got[1] >= self.clock.now() - USES_TTL_S else None

    async def session(self, token_id: int, tool_use_id: str) -> str | None:
        """The session that made this call, waiting a moment (``HOOK_WAIT_S``) for its hook's
        report if it hasn't come yet: the two requests can arrive in either order."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HOOK_WAIT_S
        while True:
            got = self.get(token_id, tool_use_id)
            if got is not None:
                return got
            left = deadline - loop.time()
            if left <= 0:
                return None
            try:
                await asyncio.wait_for(self.arrived.wait(), left)
            except TimeoutError:
                return None


class InputUses:
    """A command hook's report, paired with IDs injected into that same tool call's input."""

    def __init__(self, clock: Any) -> None:
        self.clock = clock
        self.uses: OrderedDict[tuple[int, str], tuple[str, str, str, float]] = OrderedDict()

    def report(self, token_id: int, harness: str, sid: str, use: str, tool: str) -> None:
        now = self.clock.now()
        self.uses[(token_id, use)] = (harness, sid, tool, now)
        self.uses.move_to_end((token_id, use))
        while self.uses and (
            len(self.uses) > USES_MAX or next(iter(self.uses.values()))[3] < now - USES_TTL_S
        ):
            self.uses.popitem(last=False)

    def take(self, token_id: int, harness: str, sid: str, use: str, tool: str) -> bool:
        got = self.uses.pop((token_id, use), None)
        return bool(
            got is not None and got[:3] == (harness, sid, tool) and got[3] >= self.clock.now() - USES_TTL_S
        )


class HttpTools:
    """``build_server``'s backend inside the broker (``mcp.server.ToolBackend``), one for every
    agent over HTTPS. Each tool call's caller is worked out by ``prime``, from the request the call
    came on, and kept in a context variable for the rest of that call."""

    def __init__(self, state: "BrokerState") -> None:
        self.state = state
        self._caller: ContextVar[Caller | None] = ContextVar("switchboard_http_caller", default=None)
        self._ids = itertools.count(1 << 48)  # connection ids apart from the socket's
        self.uses = HookUses(state.clock)
        self.input_uses = InputUses(state.clock)
        self.input_session: ContextVar[str | None] = ContextVar("switchboard_input_session", default=None)

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
        conn = self.conn_for(token, harness, info)
        if harness in ("cursor", "devin"):
            thread = self.input_session.get()
        elif harness == "claude":
            use = _id(McpState.meta(ctx).get(TOOL_USE_KEY))
            thread = await self.uses.session(token.id, use) if use is not None else None
        else:
            thread = self._thread_of(ctx)
        self._caller.set(Caller(token, harness, thread, conn))
        self.state.agents.http_claims(token.device, TOOL_NAMES)

    def conn_for(self, token: AgentTokenRow, harness: str, info: dict[str, str] | None = None) -> HttpConn:
        """A connection for one call of ``harness`` on the token's device (§49.3)."""
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
            client_info=info or {},
            evidence={"via": "agent token"},
            version=__version__,
            tools=TOOL_NAMES,
        )
        return HttpConn(next(self._ids), token.device, mc)

    async def on_hook(self, fields: dict[str, Any]) -> str:
        """A harness's hook, by its token (``prime`` ran): Claude Code's PreToolUse names the
        session of the call it precedes; every other event goes to the session it names (§49.4),
        and the answer is the hook's reply, checked (``reply_for``)."""
        c = self._served()
        if c is None:
            return "{}"
        event, sid = str(fields.get("event") or ""), _id(fields.get("session_id"))
        if event == "PreToolUse":
            use = _id(fields.get("tool_use_id"))
            if c.harness == "claude" and sid and use:
                self.uses.report(c.token.id, use, sid)
            return "{}"
        params = hook_params(event, fields, self.state.clock.now())
        agents: Any = self.state.agents  # an HttpConn stands in for the socket's Conn
        try:
            res = await agents.http_hook(c.conn, c.harness, event, params)
        except asyncio.CancelledError:
            # the hook went away (Codex: Esc while its Stop hook waited): so does its park
            agents.run(self.state.engine.close_conn_sinks(c.conn.id))
            raise
        reply = reply_for(c.harness, event, res.get("out"))
        if reply is None:
            return "{}"
        agents.hook_ack(c.conn, {"batch_id": res.get("batch_id"), "ack": res.get("ack")})
        return json.dumps(reply)

    def thread_id(self, ctx: Context | None) -> str | None:
        """The caller's session within its MCP connection: a Codex thread, or the Claude Code
        session its PreToolUse hook reported for this call (``prime`` worked it out)."""
        c = self._caller.get()
        return c.thread if c is not None else self._thread_of(ctx)

    @staticmethod
    def _thread_of(ctx: Context | None) -> str | None:
        """Codex: ``_meta.threadId`` of this call (one MCP session serves many threads)."""
        return _id(McpState.meta(ctx).get("threadId"))

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


class SessionHook:
    """``/mcp/session`` (§49.4): Claude Code's SessionStart and SessionEnd, which it won't run as
    ``mcp_tool`` hooks (it has no MCP connection then), sent by the plugin's ``bin/session``, a
    command hook. It posts a small JSON object with only ``SESSION_FIELDS`` and is handled exactly
    as the hook tool handles the same event: a SessionEnd takes the session offline, and a
    SessionStart after ``/clear`` or a compaction may answer with context, in the checked shape.
    Anything else is refused. The answer is always 200 with a JSON object (``{}`` for nothing),
    which the script prints for Claude Code."""

    def __init__(self, auth: AgentAuth, tools: HttpTools) -> None:
        self.auth = auth
        self.tools = tools

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await _reply(send, 405, {"error": "method", "message": "POST only"})
            return
        if _header(scope, b"origin") is not None:
            await _reply(send, 403, {"error": "forbidden", "message": "agents only"})
            return
        row = self.auth.token(scope)
        if row is None:
            await _reply(send, 401, {"error": "unauthorized", "message": "an agent token is needed"})
            return
        raw = b""
        while True:
            msg = await receive()
            raw += msg.get("body", b"")
            if len(raw) > SESSION_BODY_MAX:
                await _reply(send, 413, {"error": "too_large", "message": "too large"})
                return
            if not msg.get("more_body"):
                break
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("event") not in SESSION_EVENTS:
            await _reply(send, 400, {"error": "bad_request", "message": "SessionStart or SessionEnd"})
            return
        self.auth.touch(row)
        fields = {k: body[k] for k in SESSION_FIELDS if isinstance(body.get(k), str)}
        event = fields["event"]
        state = self.tools.state
        agents: Any = state.agents  # an HttpConn stands in for the socket's Conn
        conn = self.tools.conn_for(row, "claude")
        res = await agents.http_hook(conn, "claude", event, hook_params(event, fields, state.clock.now()))
        reply = reply_for("claude", event, res.get("out"))
        if reply is None:
            await _reply(send, 200, {})
            return
        await _reply(send, 200, reply)  # sent first: a batch is acknowledged once its answer went out
        agents.hook_ack(conn, {"batch_id": res.get("batch_id"), "ack": res.get("ack")})


class CommandHook:
    """A command hook reports its own session; PreToolUse also binds its call's injected IDs."""

    def __init__(self, auth: AgentAuth, tools: HttpTools) -> None:
        self.auth, self.tools = auth, tools

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await _reply(send, 405, {"error": "method"})
            return
        if _header(scope, b"origin") is not None:
            await _reply(send, 403, {"error": "forbidden"})
            return
        row = self.auth.token(scope)
        if row is None:
            await _reply(send, 401, {"error": "unauthorized"})
            return
        raw = b""
        while True:
            msg = await receive()
            raw += msg.get("body", b"")
            if len(raw) > 4096:
                await _reply(send, 413, {"error": "too_large"})
                return
            if not msg.get("more_body"):
                break
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("harness") not in ("cursor", "devin"):
            await _reply(send, 400, {"error": "bad_request"})
            return
        harness = body["harness"]
        sid = _id(body.get("session_id"))
        event = body.get("event")
        if sid is None or not isinstance(event, str):
            await _reply(send, 400, {"error": "bad_request"})
            return
        self.auth.touch(row)
        if scope.get("path") == PRETOOL_PATH:
            use, tool = _id(body.get("tool_use_id")), _id(body.get("tool_name"))
            names = {f"switchboard_{name}" for name in TOOL_NAMES} if harness == "cursor" else set(TOOL_NAMES)
            expected = "preToolUse" if harness == "cursor" else "PreToolUse"
            if event != expected or use is None or tool not in names:
                await _reply(send, 400, {"error": "bad_request"})
                return
            self.tools.input_uses.report(row.id, harness, sid, use, tool)
            if harness == "devin":
                conn = self.tools.conn_for(row, harness)
                await self.tools.state.agents.http_hook(
                    conn,
                    harness,
                    event,
                    hook_params(
                        event,
                        {"session_id": sid, "tool_use_id": use, "tool_name": tool},
                        self.tools.state.clock.now(),
                    ),
                )
            await _reply(send, 200, {})
            return
        ev = canonical_event(event)
        allowed = (
            ("SessionStart", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd")
            if harness == "cursor"
            else ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd")
        )
        if ev not in allowed:
            await _reply(send, 400, {"error": "bad_request"})
            return
        fields = {k: v for k, v in body.items() if k in HOOK_FIELDS}
        fields["session_id"] = sid
        agents: Any = self.tools.state.agents
        conn = self.tools.conn_for(row, harness)
        res = await agents.http_hook(
            conn, harness, ev, hook_params(event, fields, self.tools.state.clock.now())
        )
        reply = reply_for(harness, event, res.get("out")) or {}
        await _reply(send, 200, reply)
        if reply:
            agents.hook_ack(conn, {"batch_id": res.get("batch_id"), "ack": res.get("ack")})


class Listener:
    """``/mcp/listen`` (§49.4): Claude Code's Stop hook, a ``curl`` loop with ``asyncRewake``, posts
    here with nothing but its session id and waits, round after round. The answer is the message
    that wakes it (200, its text); 408 when a round ran out with nothing, so the listener waits
    again; or another refusal (409 and the rest), which ends the listener and wakes nothing. A later
    listener of the session, or any other hook of it, ends this one, and so does ``curl`` going away.
    A later round (``X-Switchboard-Again: 1``) waits only while the session is still idle, so one
    that slips in after the session got busy never reports it idle."""

    def __init__(self, auth: AgentAuth, tools: HttpTools) -> None:
        self.auth = auth
        self.tools = tools

    @staticmethod
    async def _ended(send: Send, agents: Any, conn: HttpConn, sid: str) -> bool:
        """410 with the line that tells a session it was ended by someone else (``http_ended``),
        as plain text the listener prints once; False when there is no such line."""
        try:
            why = agents.http_ended(conn, sid)
        except ServiceError:
            why = None
        if not why:
            return False
        body = why.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 410,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
        return True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await _reply(send, 405, {"error": "method", "message": "POST only"})
            return
        if _header(scope, b"origin") is not None:
            await _reply(send, 403, {"error": "forbidden", "message": "agents only: a browser can't listen"})
            return
        row = self.auth.token(scope)
        if row is None:
            await _reply(send, 401, {"error": "unauthorized", "message": "an agent token is needed"})
            return
        sid = _id(_header(scope, SESSION_HEADER))
        if sid is None:
            await _reply(send, 400, {"error": "bad_request", "message": "X-Switchboard-Session is needed"})
            return
        try:
            wait = float(_header(scope, WAIT_HEADER) or 900)
        except ValueError:
            wait = 900.0
        self.auth.touch(row)
        state = self.tools.state
        agents: Any = state.agents  # an HttpConn stands in for the socket's Conn
        conn = self.tools.conn_for(row, "claude")
        if _header(scope, AGAIN_HEADER) == "1":
            try:
                p = agents.http_session(conn, sid)
            except ServiceError:
                p = None
            if p is None and await self._ended(send, agents, conn, sid):
                return
            if p is None or p.status != "idle":
                await _reply(send, 409, {"error": "not_idle", "message": "not idle: its next Stop listens"})
                return

        async def gone() -> None:
            # curl went away (its session ended, or a turn began): so does the park
            while (await receive())["type"] != "http.disconnect":
                pass
            agents.run(state.engine.close_conn_sinks(conn.id))

        watch = asyncio.create_task(gone())
        try:
            params = {"sid": sid, "max_wait_s": min(max(wait, 0.0), WAIT_MAX_S), "t": state.clock.now()}
            res = await agents.http_hook(conn, "claude", "Stop", params)
        finally:
            watch.cancel()
        out = res.get("out")
        text = out.get("text") if isinstance(out, dict) and out.get("kind") == "continue" else None
        if not isinstance(text, str) or not text:
            if res.get("ended") in LISTEN_AGAIN:
                await _reply(send, 408, {"error": "timeout", "message": "nothing yet: listen again"})
            elif not await self._ended(send, agents, conn, sid):
                await _reply(send, 409, {"error": "nothing", "message": "nothing to wake for"})
            return
        agents.hook_ack(conn, {"batch_id": res.get("batch_id"), "ack": res.get("ack")})
        body = text.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class HideHook(Middleware):
    """The hook tool is for hooks, so ``tools/list`` leaves it out: the model never sees it."""

    async def on_list_tools(self, context: Any, call_next: Any) -> Any:
        return [t for t in await call_next(context) if t.name != HOOK_TOOL]


class InputIdentity(Middleware):
    """Remove injected arguments before tool validation; accept only a matching hook report."""

    def __init__(self, tools: HttpTools) -> None:
        self.tools = tools

    async def on_list_tools(self, context: Any, call_next: Any) -> Any:
        listed = await call_next(context)
        if client_info(context.fastmcp_context).get("name") == "Cursor":
            return [t for t in listed if t.name.startswith("switchboard_")]
        return [t for t in listed if not t.name.startswith("switchboard_")]

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        ctx = context.fastmcp_context
        info = client_info(ctx)
        if context.message.name == HOOK_TOOL or info.get("name") not in ("Cursor", "rmcp"):
            return await call_next(context)
        harness = "cursor" if info.get("name") == "Cursor" else "devin"
        if harness == "cursor" and not context.message.name.startswith("switchboard_"):
            return ToolResult(
                content=[TextContent(text=err("use the Cursor switchboard tools", "bad_request"))]
            )
        args = context.message.arguments or {}
        sid, use = _id(args.get(INPUT_SESSION)), _id(args.get(INPUT_USE))
        try:
            token = get_http_request().scope.get("state", {}).get("agent_token")
        except RuntimeError:
            token = None
        if (
            not isinstance(token, AgentTokenRow)
            or sid is None
            or use is None
            or not self.tools.input_uses.take(token.id, harness, sid, use, context.message.name)
        ):
            return ToolResult(
                content=[TextContent(text=err("this call has no matching session hook", "bad_request"))]
            )
        clean = {k: v for k, v in args.items() if k not in (INPUT_SESSION, INPUT_USE)}
        marker = self.tools.input_session.set(sid)
        try:
            return await call_next(
                context.copy(message=context.message.model_copy(update={"arguments": clean}))
            )
        finally:
            self.tools.input_session.reset(marker)


def install(app: "FastAPI", state: "BrokerState") -> Any:
    """Serve the tools at ``PATH`` behind ``AgentAuth``, with the hidden hook tool. Returns the
    MCP app, whose lifespan runs its session manager: the broker's own lifespan enters it."""
    tools = HttpTools(state)
    server = build_server(tools)

    @server.tool(name=HOOK_TOOL, annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False))
    async def hook(
        ctx: Context,
        event: Any = "",
        session_id: Any = "",
        tool_use_id: Any = "",
        tool_name: Any = "",
        turn_id: Any = "",
        prompt_id: Any = "",
        permission_mode: Any = "",
        model: Any = "",
        source: Any = "",
        reason: Any = "",
        stop_hook_active: Any = "",
        tool_response: Any = "",
        wait_s: Any = "",
    ) -> str:
        """For switchboard's hooks, not for the model."""
        await tools.prime(ctx)
        given = locals()
        return await tools.on_hook({k: given[k] for k in HOOK_FIELDS})

    server.add_middleware(HideHook())
    server.add_middleware(InputIdentity(tools))
    mcp_app = server.http_app(path=PATH)
    inner = next(r.endpoint for r in mcp_app.routes if isinstance(r, Route) and r.path == PATH)
    auth = AgentAuth(inner, state)
    app.router.routes.append(Route(PATH, endpoint=auth))
    app.router.routes.append(Route(LISTEN_PATH, endpoint=Listener(auth, tools)))
    app.router.routes.append(Route(SESSION_PATH, endpoint=SessionHook(auth, tools)))
    command_hook = CommandHook(auth, tools)
    app.router.routes.append(Route(PRETOOL_PATH, endpoint=command_hook))
    app.router.routes.append(Route(EVENT_PATH, endpoint=command_hook))
    return mcp_app
