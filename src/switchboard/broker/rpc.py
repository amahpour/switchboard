"""UDS JSON-lines RPC server (DESIGN.md §5.1, §5.2).

- One JSON object per line, at most 1 MiB. Requests ``{"id", "method", "params"}``;
  responses ``{"id", "result"}`` or ``{"id", "error": {"code", "message"}}``;
  pushes ``{"push": kind, "data": {...}}``.
- Peers with another uid are dropped at accept. Pids come from the kernel.
- Roles: anon < human_cli < human (see peer.PeerPolicy). ``mcp``: the connection
  sent ``mcp.hello`` and passed ``verify_mcp_peer``. ``member``: an mcp connection
  whose ``params.cred`` matches a membership issued to this very MCP process
  (checked by AgentService). ``hook``: any same-uid peer; AgentService decides
  which session, if any, the event may affect.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import socket
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from switchboard import __version__
from switchboard.broker.commands import Actor
from switchboard.broker.hub import Subscriber
from switchboard.broker.peer import Peer, PeerPolicy
from switchboard.broker.service import ServiceError, message_dict

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState

log = logging.getLogger("switchboard.rpc")

MAX_LINE = 1 << 20
ERROR_CODES = frozenset(
    {
        "bad_request",
        "unauthorized",
        "forbidden",
        "not_found",
        "not_member",
        "name_taken",
        "name_reserved",
        "kicked",
        "paused",
        "internal",
        "conflict",
    }
)


class RpcError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code if code in ERROR_CODES else "internal"
        self.message = message


Handler = Callable[["Conn", dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class MethodSpec:
    role: str  # anon | human_cli | human | login (human_cli + TTY)
    handler: Handler
    long_poll: bool = False


class Conn:
    _ids = itertools.count(1)

    def __init__(self, server: "RpcServer", writer: asyncio.StreamWriter, peer: Peer):
        self.id = next(self._ids)
        self.server = server
        self.writer = writer
        self.peer = peer
        self.out: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=5000)
        self.closed = False
        self.tails: list[TailSubscriber] = []
        self.mcp: Any = None  # AgentService.McpConn once mcp.hello succeeded

    def send(self, obj: dict[str, Any]) -> None:
        if self.closed:
            return
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        try:
            self.out.put_nowait(line)
        except asyncio.QueueFull:
            log.warning("conn %d: output queue full; closing", self.id)
            self.close()

    def push(self, kind: str, data: dict[str, Any]) -> None:
        self.send({"push": kind, "data": data})

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.out.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def run_writer(self) -> None:
        try:
            while True:
                line = await self.out.get()
                if line is None:
                    break
                self.writer.write(line)
                await self.writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.closed = True
            try:
                self.writer.close()
            except Exception:
                pass


class TailSubscriber(Subscriber):
    """A ``room.tail`` subscription living on an RPC connection."""

    kinds = frozenset({"msg", "notice"})

    def __init__(self, conn: Conn, room: str):
        super().__init__()
        self.conn = conn
        self.rooms = {room}

    def format(self, kind: str, room: str | None, data: dict[str, Any]) -> Any:
        return (kind, room, data)

    def offer(self, item: Any) -> bool:  # write straight into the connection queue
        if self.closed or self.conn.closed:
            self.closed = True
            return False
        kind, room, data = item
        push = "message" if kind == "msg" else kind
        self.conn.push(push, {"room": room, **data})
        return True


class RpcServer:
    def __init__(self, path: Path, state: "BrokerState"):
        self.path = Path(path)
        self.state = state
        self.policy: PeerPolicy = state.peer_policy
        self.methods: dict[str, MethodSpec] = build_methods(state)
        self._server: asyncio.base_events.Server | None = None
        self.conns: set[Conn] = set()
        self._tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        p = self.path
        if p.exists() or p.is_symlink():
            st = os.lstat(p)
            if not stat.S_ISSOCK(st.st_mode):
                raise RuntimeError(f"{p} exists and is not a socket")
            if _socket_alive(p):
                raise RuntimeError(f"another broker is listening on {p}")
            p.unlink()
        old = os.umask(0o077)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle, path=str(p), limit=MAX_LINE + 1
            )
        finally:
            os.umask(old)
        os.chmod(p, 0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for c in list(self.conns):
            c.close()
        for t in list(self._tasks):
            t.cancel()
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            except (asyncio.TimeoutError, Exception):
                pass
        try:
            if self.path.exists() and stat.S_ISSOCK(os.lstat(self.path).st_mode):
                self.path.unlink()
        except OSError:
            pass

    # ----------------------------------------------------------- connection
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock: socket.socket | None = writer.get_extra_info("socket")
        peer = Peer.from_socket(sock)
        if peer.uid != os.getuid():
            log.warning("refused UDS peer with uid %s", peer.uid)
            writer.close()
            return
        conn = Conn(self, writer, peer)
        self.conns.add(conn)
        wtask = asyncio.create_task(conn.run_writer())
        try:
            while not conn.closed:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    conn.send({"id": None, "error": {"code": "bad_request", "message": "line too long"}})
                    break
                except ConnectionError:
                    break
                if not line:
                    break
                if not line.strip():
                    continue
                await self._dispatch(conn, line)
        finally:
            for t in conn.tails:
                self.state.hub.remove(t)
            if getattr(self.state, "agents", None) is not None:
                try:
                    # MCP sessions go offline; a parked hook's park ends
                    self.state.agents.conn_closed(conn)
                except Exception:
                    log.exception("conn %d: cleanup failed", conn.id)
            conn.close()
            self.conns.discard(conn)
            try:
                await asyncio.wait_for(wtask, 2.0)
            except (asyncio.TimeoutError, Exception):
                wtask.cancel()

    async def _dispatch(self, conn: Conn, line: bytes) -> None:
        rid: Any = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise RpcError("bad_request", "request must be an object")
            rid = req.get("id")
            if not isinstance(rid, int) or isinstance(rid, bool):
                raise RpcError("bad_request", "id must be an integer")
            method = req.get("method")
            params = req.get("params", {})
            params = {} if params is None else params
            if not isinstance(method, str) or not isinstance(params, dict):
                raise RpcError("bad_request", "method must be a string and params an object")
            spec = self.methods.get(method)
            if spec is None:
                raise RpcError("not_found", f"unknown method {method}")
            self._authorize(conn, spec.role, method)
        except RpcError as e:
            conn.send({"id": rid, "error": {"code": e.code, "message": e.message}})
            return
        except (ValueError, UnicodeDecodeError):
            conn.send({"id": None, "error": {"code": "bad_request", "message": "invalid JSON"}})
            return
        if spec.long_poll:
            t = asyncio.create_task(self._run(conn, rid, method, spec, params))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        else:
            await self._run(conn, rid, method, spec, params)

    async def _run(self, conn: Conn, rid: int, method: str, spec: MethodSpec, params: dict[str, Any]) -> None:
        try:
            result = await spec.handler(conn, params)
            conn.send({"id": rid, "result": result})
        except RpcError as e:
            conn.send({"id": rid, "error": {"code": e.code, "message": e.message}})
        except ServiceError as e:
            code = e.code if e.code in ERROR_CODES else "bad_request"
            conn.send({"id": rid, "error": {"code": code, "message": e.message}})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("rpc %s failed", method)
            conn.send({"id": rid, "error": {"code": "internal", "message": "internal error"}})

    def _authorize(self, conn: Conn, role: str, method: str) -> None:
        if role == "anon":
            return
        pol, peer = self.policy, conn.peer
        if role == "human_cli":
            if pol.human_allowed(peer) or pol.human_cli_allowed(peer):
                return
            why = pol.refusal(peer)  # the SSH rules name their reason (§27.5.7)
            if why:
                raise RpcError("forbidden", f"{method} {why}")
            raise RpcError(
                "forbidden",
                f"{method} must come from your own terminal (not from an agent's shell;"
                " every process above the caller must be visible and none may be an agent)",
            )
        if role == "human":
            if pol.human_allowed(peer):
                return
            raise RpcError("forbidden", f"{method} needs your web session: use the switchboard web UI")
        if role in ("mcp", "member"):
            if conn.mcp is not None:
                return
            raise RpcError("unauthorized", f"{method}: send mcp.hello first")
        if role == "hook":
            return
        if role == "login":
            if pol.login_allowed(peer):
                return
            why = pol.refusal(peer)
            if why:
                raise RpcError("forbidden", f"{method} {why}")
            raise RpcError(
                "forbidden",
                "login links are only issued to a terminal you typed in: run `switchboard login` there",
            )
        raise RpcError("forbidden", f"{method}: role {role} not available")


def _socket_alive(p: Path) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(str(p))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------------------
# METHODS (§5.2). M1: sys.*, room.*, human.*. M2 adds mcp.*, agent.*, hook.*.
# ---------------------------------------------------------------------------
def _str(params: dict[str, Any], key: str) -> str:
    v = params.get(key)
    if not isinstance(v, str) or not v:
        raise RpcError("bad_request", f"{key} is required")
    return v


def _opt_int(params: dict[str, Any], key: str, default: int | None = None) -> int | None:
    v = params.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise RpcError("bad_request", f"{key} must be an integer")
    return v


def build_methods(state: "BrokerState") -> dict[str, MethodSpec]:
    svc = lambda: state.service  # noqa: E731  (service exists once the lifespan runs)

    def actor_for(conn: Conn) -> Actor:
        pol = state.peer_policy
        role = "human" if pol.human_allowed(conn.peer) else "human_cli"
        return Actor(role=role, via="cli", chain=pol.describe(conn.peer))

    async def sys_ping(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": __version__,
            "pid": state.info.pid,
            "port": state.info.port,
            "test_mode": state.test_mode,
        }

    async def sys_status(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        return svc().status()

    async def sys_stop(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        log.warning("stop requested over the UDS by pid %s", conn.peer.pid)
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, state.request_shutdown)
        return {}

    async def room_list(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        return {"rooms": svc().rooms()}

    async def room_who(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        room = svc().room(_str(p, "room"))
        members = svc().members(room.name)
        # agentsview ids (§26) only for a caller that passes the human_cli check (`switchboard who`
        # in the human's terminal), never for an agent's shell, and only when agentsview is found
        ids = svc().transcript_ids(room)
        pol = state.peer_policy
        if ids and (pol.human_allowed(conn.peer) or pol.human_cli_allowed(conn.peer)):
            for m in members:
                if m["name"] in ids:
                    m["transcript"] = ids[m["name"]]
        return {
            "room": room.name,
            "human": state.cfg.human_name,
            "members": members,
            "settings": svc().settings(room),
        }

    async def room_history(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        room = svc().room(_str(p, "room"))
        after = _opt_int(p, "after")
        limit = _opt_int(p, "limit", 100) or 100
        return {"room": room.name, "messages": svc().history(room.name, after, limit)}

    async def room_tail(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        room = svc().room(_str(p, "room"))
        after = _opt_int(p, "after")
        limit = _opt_int(p, "limit", 20)
        limit = 20 if limit is None else max(0, limit)
        follow = p.get("follow", True) is not False
        # Backlog and subscription in the same loop step: no gap, no duplicate.
        limit = min(limit, 999)
        more = False
        if not limit:
            rows = []
        elif after is None:
            rows = state.store.history(room.id, None, limit)  # the newest ``limit``
        else:
            rows = state.store.history(room.id, after, limit + 1)  # the oldest after ``after``
            more = len(rows) > limit
            rows = rows[:limit]
        backlog = [message_dict(m) for m in rows]
        if follow:
            sub = TailSubscriber(conn, room.name)
            conn.tails.append(sub)
            state.hub.add(sub)
        # ``more``: page on with room.history(after=last id); pushes that arrive
        # meanwhile overlap by id and are de-duplicated by the client.
        return {"room": room.name, "messages": backlog, "following": follow, "more": more}

    async def room_create(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        room = svc().create_room(_str(p, "name"))
        return {"room": svc().room_dict(room)}

    async def human_say(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        msg = svc().human_say(_str(p, "room"), _str(p, "text"), via="cli")
        return {"id": msg.id}

    async def human_command(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        return svc().command(_str(p, "room"), _str(p, "text"), actor_for(conn))

    async def human_login_link(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        token = state.login_tokens.mint()
        chain = state.peer_policy.describe(conn.peer)
        state.store.add_event("login", data={"what": "link", "via": "cli"})
        state.hub.notice(None, "warn", f"a login link was issued via cli ({chain})")
        return {"url": f"{state.base_url}/login?t={token}", "expires_in_s": state.login_tokens.ttl_s}

    async def human_logout_all(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        n = state.sessions.revoke_all()
        state.hub.close_sessions(None)
        state.store.add_event("login", data={"what": "logout_all", "via": "cli", "revoked": n})
        return {"revoked": n}

    agents = lambda: state.agents  # noqa: E731

    async def mcp_hello(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        return agents().hello(conn, p)

    async def mcp_attach(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        return agents().attach(conn, p)

    async def mcp_posted(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        return agents().posted(conn, p)

    async def mcp_bye(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        agents().conn_closed(conn)
        return {}

    def member_op(fn_name: str) -> Handler:
        async def h(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
            return getattr(agents(), fn_name)(conn, p)

        return h

    async def agent_wait(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        return await agents().wait(conn, p)

    async def hook_event(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        return await agents().hook_event(conn, p)

    async def hook_ack(conn: Conn, p: dict[str, Any]) -> dict[str, Any]:
        return agents().hook_ack(conn, p)

    return {
        "mcp.hello": MethodSpec("anon", mcp_hello),
        "mcp.attach": MethodSpec("mcp", mcp_attach),
        "mcp.posted": MethodSpec("mcp", mcp_posted),
        "mcp.bye": MethodSpec("mcp", mcp_bye),
        "agent.join": MethodSpec("mcp", member_op("join")),
        "agent.leave": MethodSpec("member", member_op("leave")),
        "agent.who": MethodSpec("member", member_op("who")),
        "agent.say": MethodSpec("member", member_op("say")),
        "agent.read": MethodSpec("member", member_op("read")),
        "agent.wait": MethodSpec("member", agent_wait, long_poll=True),
        "agent.unwait": MethodSpec("member", member_op("unwait")),
        "agent.pass": MethodSpec("member", member_op("pass_")),
        "agent.away": MethodSpec("member", member_op("away")),
        # a long poll: a Cursor stop hook parks here until a follow-up (or its end)
        "hook.event": MethodSpec("hook", hook_event, long_poll=True),
        "hook.ack": MethodSpec("hook", hook_ack),
        "sys.ping": MethodSpec("anon", sys_ping),
        "sys.status": MethodSpec("anon", sys_status),
        "sys.stop": MethodSpec("human_cli", sys_stop),
        "room.list": MethodSpec("anon", room_list),
        "room.who": MethodSpec("anon", room_who),
        "room.history": MethodSpec("anon", room_history),
        "room.tail": MethodSpec("anon", room_tail),
        "room.create": MethodSpec("human", room_create),
        "human.say": MethodSpec("human_cli", human_say),
        "human.command": MethodSpec("human_cli", human_command),
        "human.login_link": MethodSpec("login", human_login_link),
        "human.logout_all": MethodSpec("human_cli", human_logout_all),
    }
