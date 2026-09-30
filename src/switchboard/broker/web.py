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

The owner of a hosted broker (§31): ``GET /setup`` is the claim page while there is no
owner; ``POST /api/setup/begin`` (the claim token) and ``/finish`` (the new passkey)
claim it; ``POST /api/passkey/begin`` and ``/finish`` sign in with a passkey (or, for a
signed-in browser, pass a fresh passkey check); ``POST /api/passkeys/begin`` and
``/api/passkeys`` add a passkey to a session that passed one; ``GET /api/auth/state``
tells the sign-in page what applies. None of these need a session but the last two;
all of them are Origin-checked like every write, and rate-limited on failure.

Machines that dial in (§31.7): ``POST /link/pair`` (a pairing code, sent by ``switchboard
remote join``: no session, and an Origin is refused), the ``/link`` WebSocket (no Origin, a
signed handshake), and the owner's ``GET /api/machines``, ``POST /api/machines/pair``,
``/api/machines/{name}/approve``, ``/remove`` and ``/cancel`` (a code). Making a code and
approving need a passkey check in the last five minutes, as adding a passkey does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect, WebSocketState

from switchboard import __version__, db
from switchboard.broker.auth import COOKIE_NAME, SESSION_TTL_S, sha256_hex
from switchboard.broker.commands import Actor
from switchboard.broker.passkeys import CEREMONY_TTL_S, CLAIM_GRACE_S, clean_name, sign_count_ok
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
# the ceremony cookies (§31.3, §31.4): the claim's id, a sign-in's sealed state, an add's
SETUP_COOKIE = "switchboard_setup"
PASSKEY_COOKIE = "switchboard_passkey"
ADD_COOKIE = "switchboard_passkey_add"
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


def _set_cookie(resp: Response, sid: str, *, secure: bool) -> None:
    """The session cookie. ``secure`` is the web origin's (``state.web_origin.secure``), on every
    call: a refresh without it would replace an https page's Secure cookie with one a plain-http
    request to the same name would carry."""
    resp.set_cookie(
        COOKIE_NAME,
        sid,
        max_age=SESSION_TTL_S,
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure,  # behind a public https:// URL (DESIGN.md §30), never sent over plain http
    )


def _set_ceremony(resp: Response, name: str, value: str, *, secure: bool) -> None:
    """A ceremony cookie: HttpOnly, SameSite=Strict, Secure behind https, gone with the ceremony."""
    resp.set_cookie(name, value, max_age=int(CEREMONY_TTL_S), path="/", httponly=True, samesite="strict",
                    secure=secure)


def _drop_cookie(resp: Response, name: str, *, secure: bool) -> None:
    resp.delete_cookie(name, path="/", httponly=True, samesite="strict", secure=secure)


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

    # A platform's health check (DESIGN.md §30): "ok" and nothing else, with no session and, in
    # HostOriginGuard, no Host check (the checker probes the container's own address).
    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    async def healthz() -> Response:
        return Response("ok\n", media_type="text/plain", headers=NO_STORE)

    # the tab icon for a browser that asks the old way (a JSON response opened in a tab, or a
    # browser that ignores <link rel="icon">): the 32 px PNG, which browsers accept at .ico
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return FileResponse(STATIC_DIR / "favicon-32.png", media_type="image/png",
                            headers={"Cache-Control": "no-cache"})

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
        sid = state.sessions.create("login-link")
        state.store.add_event("login", data={"what": "session", "via": "web"})
        state.hub.notice(None, "warn", "new web login")
        resp = RedirectResponse("/", status_code=303, headers=NO_STORE)
        _set_cookie(resp, sid, secure=state.web_origin.secure)
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
        resp.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict", secure=state.web_origin.secure)
        return resp

    # ----------------------------------------------------- the owner (§31)
    def claim_open() -> bool:
        return state.claim is not None and state.claim.active

    def passkeys_on() -> bool:
        return state.webauthn is not None and state.store.passkey_count() > 0

    # failures need no session, so they are rate-limited like bad login tokens: one event per
    # BAD_TOKEN_EVENT_S with a count, and the same log line at most that often
    failures: dict[str, dict[str, float]] = {}

    def failed(what: str) -> None:
        f = failures.setdefault(what, {"n": 0, "last": float("-inf")})
        f["n"] += 1
        now = state.clock.now()
        if now - f["last"] >= BAD_TOKEN_EVENT_S:
            state.store.add_event("login", data={"what": what, "count": int(f["n"])})
            log.warning("%s (%d since the last note)", what.replace("_", " "), int(f["n"]))
            f["n"], f["last"] = 0, now

    def secure() -> bool:
        return state.web_origin.secure

    @app.get("/setup", include_in_schema=False)
    async def setup_page() -> Response:
        # the claim page while there is no owner; once claimed, or on a desktop, just the app
        if not claim_open():
            return RedirectResponse("/", status_code=303, headers=NO_STORE)
        return FileResponse(STATIC_DIR / "setup.html", media_type="text/html", headers=NO_STORE)

    @app.post("/api/setup/begin")
    async def setup_begin(request: Request) -> Response:
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        claim, wa = state.claim, state.webauthn
        if claim is None or wa is None or not claim.active or not claim.check(body.get("token")):
            failed("bad_claim")
            return _err(403, "bad_claim", "This claim link is invalid, expired or already used. Open the newest one"
                                          " in the broker's log.")
        cid = request.cookies.get(SETUP_COOKIE)
        if claim.ceremony_busy(cid):
            return _err(409, "busy", "Someone is claiming this switchboard from another browser right now. If that"
                                     " isn't you, wait five minutes and try again; if it was, use that browser.")
        # the owner's WebAuthn user id, made now and kept only in the ceremony until the claim
        handle = secrets.token_bytes(16)
        options, fstate = wa.register_options(handle, state.cfg.human_name, [])
        cid = secrets.token_urlsafe(32)
        claim.bind(cid, {"fido": fstate, "handle": handle.hex()})
        resp = _ok({"options": options, "human": state.cfg.human_name})
        _set_ceremony(resp, SETUP_COOKIE, cid, secure=secure())
        return resp

    @app.post("/api/setup/finish")
    async def setup_finish(request: Request) -> Response:
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        claim, wa = state.claim, state.webauthn
        cer = claim.ceremony(request.cookies.get(SETUP_COOKIE)) if claim is not None else None
        if cer is None or wa is None:
            failed("bad_claim")
            return _err(403, "no_ceremony", "No claim is in progress in this browser (it takes five minutes at"
                                            " most): open the claim link again.")
        try:
            reg = wa.register_finish(cer["fido"], body.get("credential"))
        except ValueError as e:
            claim.drop_ceremony()  # the token stays usable until it expires
            failed("bad_claim")
            return _err(400, "bad_credential", f"the passkey could not be verified: {e}")
        name = clean_name(body.get("name"))
        with db.tx(state.store.con):  # the owner and the first passkey, or neither
            state.store.claim_owner(bytes.fromhex(cer["handle"]))
            state.store.passkey_add(reg.credential_id, reg.public_key, name, reg.aaguid, reg.sign_count)
        claim.spend()
        state.claimed()
        now = state.clock.now()
        sid = state.sessions.create("claim")
        h = sha256_hex(sid)
        state.passkey_checks[h] = now  # creating the passkey is a passkey check
        state.claim_grace[h] = now + CLAIM_GRACE_S  # and the backup step needs no second one
        state.store.add_event("login", data={"what": "claim", "via": "web", "passkey": name})
        state.hub.notice(None, "warn", f'switchboard claimed (passkey "{name}")')
        log.warning("switchboard claimed with a passkey named %r", name)
        resp = _ok({"ok": True, "name": name, "human": state.cfg.human_name})
        _set_cookie(resp, sid, secure=secure())
        _drop_cookie(resp, SETUP_COOKIE, secure=secure())
        return resp

    @app.post("/api/passkey/begin")
    async def passkey_begin(request: Request) -> Response:
        # no session, and nothing kept on the server: the ceremony's state rides in a sealed cookie
        wa = state.webauthn
        if wa is None or not passkeys_on():
            return _err(403, "no_passkeys", "no passkeys are set up here")
        options, fstate = wa.auth_options()
        resp = _ok({"options": options})
        _set_ceremony(resp, PASSKEY_COOKIE, state.sealer.seal(fstate, CEREMONY_TTL_S), secure=secure())
        return resp

    @app.post("/api/passkey/finish")
    async def passkey_finish(request: Request) -> Response:
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        wa = state.webauthn
        if wa is None or not passkeys_on():
            return _err(403, "no_passkeys", "no passkeys are set up here")
        st = state.sealer.unseal(request.cookies.get(PASSKEY_COOKIE))
        if st is None:
            failed("bad_passkey")
            return _err(403, "no_ceremony", "the sign-in took too long, or its cookie is missing: try again")
        try:
            cred_id, count = wa.auth_finish(st, state.store.passkeys(), body.get("credential"))
        except ValueError as e:
            failed("bad_passkey")
            return _err(403, "bad_credential", f"the passkey could not be verified: {e}")
        row = state.store.passkey(cred_id)
        if row is None:  # pragma: no cover - auth_finish matched one of the rows just read
            return _err(403, "bad_credential", "unknown passkey")
        if not sign_count_ok(row.sign_count, count):
            # WebAuthn §7.2: a counter that didn't grow may mean a cloned authenticator
            failed("bad_passkey")
            log.warning("passkey %r: signature counter %d after %d, refused", row.name, count, row.sign_count)
            return _err(403, "sign_count", f'the passkey "{row.name}" sent a signature counter that did not grow;'
                                           " if you did not just use it elsewhere, it may have been copied")
        if not state.used_challenges.add(st["challenge"]):
            failed("bad_passkey")
            return _err(403, "replay", "this sign-in was already used")
        state.store.passkey_used(cred_id, count)
        now = state.clock.now()
        h = session(request)
        resp = _ok({"ok": True, "name": row.name, "reauth": h is not None})
        if h is not None:
            # a signed-in browser confirming it's the owner: a fresh check for this session, no new session
            state.passkey_checks[h] = now
            state.store.add_event("login", data={"what": "passkey_check", "passkey": row.name})
        else:
            sid = state.sessions.create(f"passkey:{row.name}")
            state.passkey_checks[sha256_hex(sid)] = now
            state.store.add_event("login", data={"what": "session", "via": "passkey", "passkey": row.name})
            state.hub.notice(None, "warn", f'new web login (passkey "{row.name}")')
            _set_cookie(resp, sid, secure=secure())
        _drop_cookie(resp, PASSKEY_COOKIE, secure=secure())
        return resp

    def add_refused(request: Request) -> tuple[str | None, JSONResponse | None]:
        """The session that may add a passkey now, or why not: signed in, passkeys possible
        here, the broker claimed, and a passkey check in the last five minutes (or the
        claim's own grace)."""
        h = session(request)
        if h is None:
            return None, unauthorized()
        if state.webauthn is None:
            return None, _err(403, "no_passkeys", "passkeys need a public https:// URL (docs/DEPLOY.md)")
        if state.store.owner_handle() is None:
            return None, _err(409, "unclaimed", "claim this switchboard from the link in its log first")
        if not state.fresh_check(h):
            return None, _err(403, "reauth", "confirm it's you with a passkey first")
        return h, None

    @app.post("/api/passkeys/begin")
    async def passkeys_begin(request: Request) -> Response:
        h, no = add_refused(request)
        if no is not None:
            return no
        wa = state.webauthn
        assert wa is not None
        handle = state.store.owner_handle()
        assert handle is not None
        options, fstate = wa.register_options(handle, state.cfg.human_name,
                                              [pk.credential_id for pk in state.store.passkeys()])
        resp = _ok({"options": options})
        _set_ceremony(resp, ADD_COOKIE, state.sealer.seal(fstate, CEREMONY_TTL_S), secure=secure())
        return resp

    @app.post("/api/passkeys")
    async def passkeys_add(request: Request) -> Response:
        h, no = add_refused(request)
        if no is not None:
            return no
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        wa = state.webauthn
        assert wa is not None
        st = state.sealer.unseal(request.cookies.get(ADD_COOKIE))
        if st is None:
            return _err(403, "no_ceremony", "adding the passkey took too long, or its cookie is missing: try again")
        try:
            reg = wa.register_finish(st, body.get("credential"))
        except ValueError as e:
            return _err(400, "bad_credential", f"the passkey could not be verified: {e}")
        if state.store.passkey(reg.credential_id) is not None:
            return _err(409, "conflict", "that passkey is registered already")
        name = clean_name(body.get("name"))
        state.store.passkey_add(reg.credential_id, reg.public_key, name, reg.aaguid, reg.sign_count)
        state.store.add_event("login", data={"what": "passkey_added", "passkey": name})
        state.hub.notice(None, "info", f'passkey added ("{name}")')
        resp = _ok({"ok": True, "name": name, "passkeys": state.store.passkey_count()})
        _drop_cookie(resp, ADD_COOKIE, secure=secure())
        return resp

    @app.get("/api/auth/state")
    async def auth_state() -> Response:
        # for the sign-in page (no session): what applies here, and nothing about the owner
        return _ok({"hosted": state.web_origin.public, "claimed": state.store.owner_handle() is not None,
                    "passkeys": passkeys_on(), "claim": claim_open()})

    # ---------------------------------------------- machines that dial in (§31.7)
    @app.post("/link/pair", include_in_schema=False)
    async def link_pair(request: Request) -> Response:
        # sent by `switchboard remote join`, never by a browser: an Origin is refused outright
        if request.headers.get("origin") is not None:
            return _err(403, "forbidden", "this route takes no browser requests")
        m = state.machines
        if m is None:
            return _err(404, "not_found", "this broker takes no machines that dial in (a hosted broker with"
                                          " passkeys does)")
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        status, data = m.pair(body)
        return JSONResponse(data, status_code=status, headers=NO_STORE)

    @app.websocket("/link")
    async def link_ws(ws: WebSocket) -> None:
        m = state.machines
        if m is None or ws.headers.get("origin") is not None:  # never a browser
            await ws.close(code=1008)
            return
        await m.serve(ws)

    def owner(request: Request, fresh: bool) -> JSONResponse | None:
        """Why the owner's machine routes refuse this request, or None: a session, a broker that
        takes machines, and (to make a code or approve) a passkey check in the last 5 minutes."""
        h = session(request)
        if h is None:
            return unauthorized()
        if state.machines is None:
            return _err(404, "not_found", "machines dial in only to a hosted broker with passkeys")
        if fresh and not state.fresh_check(h):
            return _err(403, "reauth", "confirm it's you with a passkey first")
        return None

    @app.get("/api/machines")
    async def machines_list(request: Request) -> Response:
        if session(request) is None:
            return unauthorized()
        m = state.machines
        if m is None:
            return _ok({"hosted": False, "machines": []})
        return _ok({"hosted": True, "machines": m.summary(), "codes": m.codes.unused(),
                    "broker_fingerprint": m.fingerprint})

    @app.post("/api/machines/pair")
    async def machines_pair(request: Request) -> Response:
        no = owner(request, True)
        if no is not None:
            return no
        try:
            body = await _json_body(request)
            return _ok(state.machines.mint(body.get("name")))
        except ServiceError as e:
            return _svc_err(e)

    @app.post("/api/machines/{name}/approve")
    async def machines_approve(request: Request, name: str) -> Response:
        no = owner(request, True)
        if no is not None:
            return no
        try:
            await _json_body(request)
            if not valid_host(name):
                raise ServiceError("bad_request", "machine names look like work-laptop")
            return _ok(state.machines.approve(name, "web"))
        except ServiceError as e:
            return _svc_err(e)

    @app.post("/api/machines/{name}/cancel")
    async def machines_cancel(request: Request, name: str) -> Response:
        no = owner(request, False)  # forgetting a code only takes access away
        if no is not None:
            return no
        try:
            await _json_body(request)
            if not valid_host(name):
                raise ServiceError("bad_request", "machine names look like work-laptop")
            return _ok(state.machines.cancel(name))
        except ServiceError as e:
            return _svc_err(e)

    @app.post("/api/machines/{name}/remove")
    async def machines_remove(request: Request, name: str) -> Response:
        no = owner(request, False)  # removing only takes access away
        if no is not None:
            return no
        try:
            await _json_body(request)
            if not valid_host(name):
                raise ServiceError("bad_request", "machine names look like work-laptop")
            return _ok(await state.machines.remove(name, "web"))
        except ServiceError as e:
            return _svc_err(e)

    # --------------------------------------------------------------- api
    @app.get("/api/me")
    async def me(request: Request) -> Response:
        h = session(request)
        if h is None:
            return unauthorized()
        resp = _ok(
            {
                "human": state.cfg.human_name,
                "version": __version__,
                "test_mode": state.test_mode,
                "port": state.info.port,
                # the passkeys sheet (§31.4): shown behind a public URL where passkeys work
                "hosted": state.webauthn is not None,
                "passkeys": state.store.passkey_count(),
                "fresh": state.fresh_check(h),
            }
        )
        # Slide the browser cookie along with the server-side session.
        _set_cookie(resp, request.cookies.get(COOKIE_NAME) or "", secure=state.web_origin.secure)
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
