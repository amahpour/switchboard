"""REST routes, static files and the read-only WebSocket (DESIGN.md §5.4, §5.5).

Every ``/api/*`` request needs the session cookie. Unsafe methods also pass
``HostOriginGuard`` (exact Origin + ``X-Switchboard: 1``). The WebSocket checks
Origin and the cookie before ``accept()`` and only understands ``hello`` and
``ping`` from the client.

Remote links (§27.11): ``GET /api/remotes`` is every remote's state;
``POST /api/remotes/{name}/enable`` and ``/disable`` are the web UI's Enable /
reconnect and Disable buttons, the web session's equivalents of the human-only
``switchboard remote enable|disable`` (consent for exactly the current config,
recorded as ``via web``). A ``remotes`` WebSocket event carries every change.

The Inspector (§29): ``GET /api/rooms/{slug}/members/{name}`` is one member's detail
(times, session id, queued ids, delivery counts, a short delivery timeline) for the human's
web session. Read-only, and never message text.

Closed rooms (§28): ``GET /api/rooms`` lists open rooms (each with its ``id``) and
the ``closed`` count; ``GET /api/closed-rooms`` lists closed ones; ``POST
/api/closed-rooms/{id}/reopen`` is the web UI's Reopen button.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect, WebSocketState

from switchboard import __version__
from switchboard.broker.auth import COOKIE_NAME, SESSION_TTL_S
from switchboard.broker.commands import Actor
from switchboard.broker.hub import WsSubscriber
from switchboard.broker.service import ServiceError, message_dict
from switchboard.models import InvalidName, normalize_room, valid_host

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState

log = logging.getLogger("switchboard.web")

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
NO_STORE = {"Cache-Control": "no-store"}
MAX_BODY = 64 * 1024
WS_MAX_ROOMS = 64
WS_BACKLOG = 500
BAD_TOKEN_EVENT_S = 60.0
HASH_RE = re.compile(r"[0-9a-f]{64}")  # a remote's config_hash (sha256 hex)
ROOM_ID_RE = re.compile(r"[1-9][0-9]{0,18}")  # a room id in /api/closed-rooms/{rid}/reopen
MAX_ROOM_ID = 2**63 - 1  # SQLite's INTEGER: 19 digits can be more (OverflowError, not 400)


class _StaticFiles(StaticFiles):
    """Static assets revalidate on every load (ETag), so an upgrade never runs stale JS."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status, headers=NO_STORE)


def _svc_err(e: ServiceError) -> JSONResponse:
    return _err(e.http_status, e.code, e.message)


def _ok(data: Any) -> JSONResponse:
    return JSONResponse(data, headers=NO_STORE)


def _set_cookie(resp: Response, sid: str) -> None:
    resp.set_cookie(
        COOKIE_NAME,
        sid,
        max_age=SESSION_TTL_S,
        path="/",
        httponly=True,
        samesite="strict",
    )


async def _json_body(request: Request) -> dict[str, Any]:
    body = await request.body()
    if len(body) > MAX_BODY:
        raise ServiceError("bad_request", "request body too large")
    if not body:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        raise ServiceError("bad_request", "invalid JSON") from None
    if not isinstance(data, dict):
        raise ServiceError("bad_request", "body must be a JSON object")
    return data


def install(app: FastAPI, state: "BrokerState") -> None:
    def session(request: Request) -> str | None:
        return state.sessions.check(request.cookies.get(COOKIE_NAME))

    def unauthorized() -> JSONResponse:
        return _err(401, "unauthorized", "not signed in: run `switchboard login` in your terminal")

    # ------------------------------------------------------------- pages
    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> Response:
        page = "index.html" if session(request) else "login.html"
        return FileResponse(STATIC_DIR / page, media_type="text/html", headers=NO_STORE)

    app.mount("/static", _StaticFiles(directory=STATIC_DIR, html=False), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    # Bad-token hits need no auth, so they are rate-limited before they touch
    # the database: at most one event per BAD_TOKEN_EVENT_S, with a count.
    bad_tokens = {"n": 0, "last": float("-inf")}

    @app.get("/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        token = request.query_params.get("t")
        if not state.login_tokens.consume(token):
            bad_tokens["n"] += 1
            now = state.clock.now()
            if now - bad_tokens["last"] >= BAD_TOKEN_EVENT_S:
                state.store.add_event("login", data={"what": "bad_token", "count": bad_tokens["n"]})
                bad_tokens["n"], bad_tokens["last"] = 0, now
            return PlainTextResponse(
                "This login link is invalid, expired or already used.\n"
                "Run `switchboard login` in your terminal for a new one.\n",
                status_code=403,
                headers=NO_STORE,
            )
        sid = state.sessions.create()
        state.store.add_event("login", data={"what": "session", "via": "web"})
        state.hub.notice(None, "warn", "new web login")
        resp = RedirectResponse("/", status_code=303, headers=NO_STORE)
        _set_cookie(resp, sid)
        return resp

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:
        h = session(request)
        if h is None:
            return unauthorized()
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        if body.get("all") is True:
            n = state.sessions.revoke_all()
            state.hub.close_sessions(None)
            state.store.add_event("login", data={"what": "logout_all", "via": "web", "revoked": n})
        else:
            n = state.sessions.revoke(request.cookies.get(COOKIE_NAME))
            state.hub.close_sessions(h)
        resp = _ok({"ok": True, "revoked": n})
        resp.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
        return resp

    # --------------------------------------------------------------- api
    @app.get("/api/me")
    async def me(request: Request) -> Response:
        if session(request) is None:
            return unauthorized()
        resp = _ok(
            {
                "human": state.cfg.human_name,
                "version": __version__,
                "test_mode": state.test_mode,
                "port": state.info.port,
            }
        )
        # Slide the browser cookie along with the server-side session.
        _set_cookie(resp, request.cookies.get(COOKIE_NAME) or "")
        return resp

    @app.get("/api/rooms")
    async def list_rooms(request: Request) -> Response:
        if session(request) is None:
            return unauthorized()
        return _ok({"rooms": state.service.rooms(), "closed": state.store.count_closed_rooms()})

    @app.post("/api/rooms")
    async def create_room(request: Request) -> Response:
        if session(request) is None:
            return unauthorized()
        try:
            body = await _json_body(request)
            name = body.get("name")
            if not isinstance(name, str):
                raise ServiceError("bad_request", "name is required")
            room = state.service.create_room(name)
            return _ok({"room": state.service.room_dict(room)})
        except ServiceError as e:
            return _svc_err(e)

    @app.get("/api/rooms/{slug}/messages")
    async def messages(request: Request, slug: str) -> Response:
        if session(request) is None:
            return unauthorized()
        try:
            after = _qint(request, "after")
            limit = _qint(request, "limit") or 200
            return _ok({"messages": state.service.history(slug, after, min(limit, 1000))})
        except ServiceError as e:
            return _svc_err(e)

    @app.get("/api/rooms/{slug}/members")
    async def members(request: Request, slug: str) -> Response:
        if session(request) is None:
            return unauthorized()
        try:
            room = state.service.room(slug)
            return _ok(
                {
                    "room": room.name,
                    "human": state.cfg.human_name,
                    "members": state.service.members(room.name),
                    "settings": state.service.settings(room),
                }
            )
        except ServiceError as e:
            return _svc_err(e)

    # The Inspector (§29): one member's detail for the human's web session. A GET, not a
    # WebSocket frame: it is read on demand for the one agent the human is looking at, so the
    # broadcast ``members`` frame (and ``switchboard tail``) stays as small as it was. It
    # writes nothing, publishes nothing and carries no message text (ids and times only).
    @app.get("/api/rooms/{slug}/members/{name}")
    async def member_detail(request: Request, slug: str, name: str) -> Response:
        if session(request) is None:
            return unauthorized()
        try:
            return _ok(state.service.member_detail(slug, name))
        except ServiceError as e:
            return _svc_err(e)

    @app.post("/api/rooms/{slug}/say")
    async def say(request: Request, slug: str) -> Response:
        if session(request) is None:
            return unauthorized()
        try:
            body = await _json_body(request)
            text = body.get("text")
            if not isinstance(text, str):
                raise ServiceError("bad_request", "text is required")
            msg = state.service.human_say(slug, text, via="web")
            return _ok({"id": msg.id})
        except ServiceError as e:
            return _svc_err(e)

    @app.post("/api/rooms/{slug}/command")
    async def command(request: Request, slug: str) -> Response:
        if session(request) is None:
            return unauthorized()
        try:
            body = await _json_body(request)
            text = body.get("text")
            if not isinstance(text, str):
                raise ServiceError("bad_request", "text is required")
            res = state.service.command(slug, text, Actor(role="human", via="web"))
            return _ok(res)
        except ServiceError as e:
            return _svc_err(e)

    # ------------------------------------------------------- closed rooms
    # Not under /api/rooms/{slug}: a room named #closed must not clash (§28).
    @app.get("/api/closed-rooms")
    async def closed_rooms(request: Request) -> Response:
        if session(request) is None:
            return unauthorized()
        return _ok({"rooms": state.service.closed_room_dicts()})

    @app.post("/api/closed-rooms/{rid}/reopen")
    async def reopen_room(request: Request, rid: str) -> Response:
        if session(request) is None:
            return unauthorized()
        try:
            await _json_body(request)
            if not ROOM_ID_RE.fullmatch(rid) or int(rid) > MAX_ROOM_ID:
                raise ServiceError("bad_request", "bad room id")
            room = state.service.reopen_room(int(rid), via="web")
            return _ok({"room": state.service.room_dict(room)})
        except ServiceError as e:
            return _svc_err(e)

    # ------------------------------------------------------------ remotes
    # The web session is the human (§5.4): enabling a remote from here is the same consent
    # as `switchboard remote enable` (§27.5.8), for exactly the entry's current config.
    @app.get("/api/remotes")
    async def remotes(request: Request) -> Response:
        if session(request) is None:
            return unauthorized()
        mgr = state.remotes
        if mgr is None:  # the broker is still starting its links
            return _ok({"remotes": [], "config_error": None, "version": __version__})
        try:
            return _ok({**(await mgr.status()), "version": __version__})
        except ServiceError as e:
            return _svc_err(e)

    async def remote_op(request: Request, name: str, op: str) -> Response:
        if session(request) is None:
            return unauthorized()
        try:
            body = await _json_body(request)
            if not valid_host(name):
                raise ServiceError("bad_request", "remote names look like fpga-pi")
            mgr = state.remotes
            if mgr is None:
                raise ServiceError("conflict", "the broker is still starting its remote links; try again")
            if op == "enable":
                # the consent is for the config the page showed (its `config_hash`): an edit of
                # remotes.toml or the key files since then is a 409, never a silent consent
                want = body.get("config_hash")
                if not isinstance(want, str) or not HASH_RE.fullmatch(want):
                    raise ServiceError("bad_request", "config_hash missing: reload the remotes panel and enable again")
                # a long poll, as the CLI's: it dials and waits up to 15 s for the outcome
                return _ok(await mgr.enable(name, via="web", actor="web session", expect_hash=want))
            return _ok(await mgr.disable(name, via="web"))
        except ServiceError as e:
            return _svc_err(e)

    @app.post("/api/remotes/{name}/enable")
    async def remote_enable(request: Request, name: str) -> Response:
        return await remote_op(request, name, "enable")

    @app.post("/api/remotes/{name}/disable")
    async def remote_disable(request: Request, name: str) -> Response:
        return await remote_op(request, name, "disable")

    # ---------------------------------------------------------- websocket
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        origin_ok = ws.headers.get("origin") == state.origin
        sid_hash = state.sessions.check(ws.cookies.get(COOKIE_NAME)) if origin_ok else None
        if not origin_ok or sid_hash is None:
            await ws.close(code=1008)
            return
        await ws.accept()
        sub = WsSubscriber()
        sub.sid_hash = sid_hash
        state.hub.add(sub)

        async def close(code: int) -> None:
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.close(code=code)

        sender = asyncio.create_task(sub.run_sender(ws.send_text, close))
        try:
            while not sub.closed:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                text = msg.get("text")
                frame = None
                if isinstance(text, str) and len(text) <= 16384:
                    try:
                        frame = json.loads(text)
                    except ValueError:
                        frame = None
                t = frame.get("t") if isinstance(frame, dict) else None
                if t == "ping":
                    sub.offer({"t": "pong"})
                elif t == "hello":
                    if not _ws_hello(state, sub, frame):
                        break
                else:
                    break  # read-only socket: anything else closes it
        except WebSocketDisconnect:
            pass
        finally:
            state.hub.remove(sub)  # queues the close sentinel
            try:
                await asyncio.wait_for(sender, 1.0)
            except (asyncio.TimeoutError, Exception):
                sender.cancel()
            if ws.client_state == WebSocketState.CONNECTED:
                try:
                    await ws.close(code=1008)
                except Exception:
                    pass


def _qint(request: Request, key: str) -> int | None:
    v = request.query_params.get(key)
    if v is None or v == "":
        return None
    try:
        n = int(v)
    except ValueError:
        raise ServiceError("bad_request", f"{key} must be an integer") from None
    if n < 0:
        raise ServiceError("bad_request", f"{key} must be >= 0")
    return n


def _ws_hello(state: "BrokerState", sub: WsSubscriber, frame: dict[str, Any]) -> bool:
    rooms = frame.get("rooms", [])
    after = frame.get("after", {}) or {}
    if not isinstance(rooms, list) or not isinstance(after, dict) or len(rooms) > WS_MAX_ROOMS:
        return False
    for raw in rooms:
        if not isinstance(raw, str):
            return False
        try:
            name = normalize_room(raw)
        except InvalidName:
            continue
        room = state.store.get_room(name)
        if room is None:
            continue
        a = after.get(raw, after.get(name))
        a = a if isinstance(a, int) and not isinstance(a, bool) and a >= 0 else None
        # Subscribe and read the backlog in one loop step: no gaps, no dups.
        sub.rooms.add(name)
        backlog = state.store.history(room.id, a, WS_BACKLOG + 1)
        if len(backlog) > WS_BACKLOG:
            # Too much to replay: send the newest WS_BACKLOG so the live stream
            # continues without a hole, and say that older lines were skipped.
            backlog = state.store.history(room.id, None, WS_BACKLOG)
            # Message ids are global across rooms: count this room's rows.
            skipped = state.store.count_messages(room.id, a or 0, backlog[0].id)
            sub.offer({"t": "notice", "room": name, "level": "info",
                       "text": f"{skipped} earlier message(s) not shown here; `switchboard tail` has them all"})
        for m in backlog:
            sub.offer({"t": "msg", "room": name, "msg": message_dict(m)})
        sub.offer({"t": "room", "room": name, "settings": state.service.settings(room)})
        sub.offer(
            {
                "t": "members",
                "room": name,
                "members": state.service.members(name),
            }
        )
    return True
