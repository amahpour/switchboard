"""REST routes, static files and the WebSocket (DESIGN.md §5.4, §5.5, §56).

Every ``/api/*`` request needs the session cookie. Unsafe methods also pass
``HostOriginGuard`` (exact Origin + ``X-Switchboard: 1``). The WebSocket checks
Origin and the cookie before ``accept()``. From the client it understands ``hello``,
``ping``, and ``board`` (a signed-in person who can see the room, sending shapes).
Anything else closes it.

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

People (§32): everyone but the owner signs in with a name and a password, or a passkey of
their own. ``POST /api/signin/password`` signs in (the owner's one-time password from the
log starts the claim instead; a person's one-time password gives a session that may only
choose its own password or passkey, ``POST /api/me/password``); ``POST /api/signin/check``
is a password check for a signed-in browser. The owner's admin section: ``GET /api/people``,
``POST /api/people`` (a name: a one-time password to hand over), ``/api/people/{id}/password``
(a new one) and ``/remove``. Everyone signed in posts under their own name, and every
person is a human to every agent.

The agents' avatars (§29.11): ``GET /api/agent-icons`` (what is set and the limits),
``/api/agent-icons.css`` (the page's stylesheet for them) and ``/api/agent-icons/{harness}/{scheme}``
(one image) answer any signed-in person; ``PUT`` the same path (the body is the file) and
``DELETE /api/agent-icons/{harness}`` are the owner's. The icon of each platform a call can be on
(``zoom``, ``meet``, ``teams``, #623) is one more name in the same routes.

A room's call (§54): ``GET /api/rooms/{slug}/call`` is its details for the one person looking, with
whether they may end it. Read-only, and never a link, a key or its hash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect, WebSocketState

from switchboard import __version__, board, build_info, db
from switchboard.broker import agent_icons, agent_tokens, home, oauth, oidc, people
from switchboard.broker import search as search_api
from switchboard.broker.auth import BOARD_CSP, COOKIE_NAME, SESSION_TTL_S, app_csp, sha256_hex, upload_path
from switchboard.broker.commands import MAX_BUDGET, MAX_HOPS, Actor
from switchboard.broker.hub import WsSubscriber
from switchboard.broker.passkeys import CEREMONY_TTL_S, CLAIM_GRACE_S, clean_name, sign_count_ok
from switchboard.broker.passwords import (
    DUMMY_HASH,
    hash_password,
    normalize_one_time,
    one_time_password,
    password_problem,
    verify_password,
)
from switchboard.broker.service import ServiceError
from switchboard.files import (
    FILE_ID_RE,
    Spool,
)
from switchboard.files import (
    clean_name as file_name,
)
from switchboard.files import (
    content_disposition as file_disposition,
)
from switchboard.files import (
    new_file_id as new_part_id,
)
from switchboard.models import InvalidName, normalize_room
from switchboard.reviews import ReviewError
from switchboard.store import Conflict

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState

log = logging.getLogger("switchboard.web")

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
NO_STORE = {"Cache-Control": "no-store"}
MAX_BODY = 64 * 1024
WS_MAX_ROOMS = 64
WS_BACKLOG = 500
WS_FRAME_MAX = 16384  # hello and ping; a board frame may be as large as the board itself
BAD_TOKEN_EVENT_S = 60.0
# the ceremony cookies (§31.3, §31.4): the claim's id, a sign-in's sealed state, an add's
SETUP_COOKIE = "switchboard_setup"
PASSKEY_COOKIE = "switchboard_passkey"
ADD_COOKIE = "switchboard_passkey_add"
DEVICE_COOKIE = "switchboard_device"
# a signed-out browser's authorize request, sealed, so signing in brings it back to the consent page (§49.9)
OAUTH_NEXT_COOKIE = "switchboard_oauth_next"
# a wrong password sign-in, whoever it was (#240): the ask-the-admin half is for someone with no
# email, who can't sign in at all since name sign-in ended, without saying whether that's who it is
WRONG_SIGNIN = "wrong email or password (no email set for you yet? ask the admin to set one)"
DEVICE_TTL_S = 30 * 24 * 3600
OPEN_CHECKS_MAX = 32  # strangers' password checks running or queued; past this, refused before scrypt
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


OIDC_COOKIE = "sb_oidc"  # binds a Google sign-in's callback to the browser that started it
# The callback's answer: a same-site navigation to the app, so the new Strict cookie is sent.
SSO_DONE_PAGE = (
    '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/">'
    '<title>switchboard</title></head><body><p>Signed in. <a href="/">Continue</a></p></body></html>'
)


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
    resp.set_cookie(
        name, value, max_age=int(CEREMONY_TTL_S), path="/", httponly=True, samesite="strict", secure=secure
    )


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


async def _spool_upload(request: Request, dest: Path, limit: int) -> tuple[int, str] | None:
    """Write the body to ``dest``, hashing it. None when it passes ``limit`` (nothing is kept)."""
    spool = Spool(dest, limit)
    try:
        async for chunk in request.stream():
            if not spool.write(chunk):
                return None
        return spool.finish()
    except Exception:
        spool.abort()
        raise


async def _capped_body(request: Request, limit: int) -> bytes | None:
    """The request's body, or None when it is longer than ``limit`` (checked before and while it is
    read, for the routes anyone can call)."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _oauth_err(status: int, code: str, description: str) -> JSONResponse:
    """RFC 6749's error answer. ``no-store`` and ``no-cache`` as §5.1 says of anything with a token."""
    return JSONResponse(
        {"error": code, "error_description": description},
        status_code=status,
        headers={**NO_STORE, "Pragma": "no-cache"},
    )


def install(app: FastAPI, state: "BrokerState") -> None:
    # scrypt work (#182). Strangers share two workers and a bounded queue: a flood past it is
    # refused before any scrypt runs, so it can't grow without end. A browser the broker already
    # trusts (its device cookie, or a session's own check) has a worker of its own, so a flood
    # under made-up names doesn't hold it up. One check at a time per guessing budget: every
    # failure is counted before that budget's next try, so parallel guesses can't outrun it.
    open_work, trusted_work = asyncio.Semaphore(2), asyncio.Semaphore(1)
    open_checks = [0]
    checking: set[str] = set()

    def refuse_check(budget: str, trusted: bool) -> Response | None:
        """A 429 when this budget has a check in flight, or strangers' checks are full."""
        if budget in checking:
            return _err(429, "slow_down", "this sign-in is being checked: try again in a moment")
        if not trusted and open_checks[0] >= OPEN_CHECKS_MAX:
            return _err(429, "busy", "too many sign-ins right now: try again in a few seconds")
        return None

    async def check_password(typed: Any, stored: str | None, budget: str, trusted: bool) -> bool:
        """One scrypt check, the dummy hash when there's no real one (#179). The caller has
        ``refuse_check``ed and records the outcome before its next ``await``."""
        checking.add(budget)
        open_checks[0] += 0 if trusted else 1
        try:
            async with trusted_work if trusted else open_work:
                matched = await asyncio.to_thread(verify_password, typed, stored or DUMMY_HASH)
        finally:
            checking.discard(budget)
            open_checks[0] -= 0 if trusted else 1
        return stored is not None and matched

    def device_id(request: Request, account: str) -> str | None:
        sealed = state.sealer.unseal(request.cookies.get(DEVICE_COOKIE))
        if sealed is None or sealed.get("kind") != "signin_device" or sealed.get("account") != account:
            return None
        value = sealed.get("id")
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{22}", value) else None

    def trust_device(resp: Response, account: str, current: str | None = None) -> None:
        value = current or secrets.token_urlsafe(16)
        resp.set_cookie(
            DEVICE_COOKIE,
            state.sealer.seal({"kind": "signin_device", "account": account, "id": value}, DEVICE_TTL_S),
            max_age=DEVICE_TTL_S,
            path="/",
            httponly=True,
            samesite="strict",
            secure=secure(),
        )

    def who(request: Request) -> people.Who | None:
        """Whoever this request's session is, if it's a live one (a removed person's is not),
        even one that must choose its own password first."""
        return people.who_of(state, state.sessions.check(request.cookies.get(COOKIE_NAME)))

    def session(request: Request) -> str | None:
        """The session's id hash, for a session that may use the app: signed in, and not
        still on a one-time password."""
        w = who(request)
        return w.h if w is not None and not w.must_reset else None

    def viewer(request: Request) -> int:
        """Who's asking, for private rooms (§44): their person id, 0 for the owner. No live
        person is -1, which no private room lists: it fails closed, to the public rooms."""
        w = who(request)
        return state.service.person_key(w.person_id) if w is not None else -1

    def unauthorized() -> JSONResponse:
        if state.hosted:
            return _err(401, "unauthorized", "not signed in: sign in again")
        return _err(401, "unauthorized", "not signed in: run `switchboard login` in your terminal")

    # ------------------------------------------------------------- pages
    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> Response:
        w = who(request)
        if w is not None and not w.must_reset:
            # an agent's sign-in (§49.9) sent this browser to sign in first: back to its consent page
            pending = state.sealer.unseal(request.cookies.get(OAUTH_NEXT_COOKIE))
            query = pending.get("q") if pending is not None and pending.get("kind") == "oauth_next" else None
            if request.cookies.get(OAUTH_NEXT_COOKIE) is not None:
                back = RedirectResponse(
                    oauth.AUTHORIZE_PATH + "?" + query
                    if isinstance(query, str) and oauth.NEXT_QUERY_RE.fullmatch(query)
                    else "/",
                    status_code=303,
                    headers=NO_STORE,
                )
                _drop_cookie(back, OAUTH_NEXT_COOKIE, secure=state.web_origin.secure)
                return back
        page = "login.html" if w is None else ("setup.html" if w.must_reset else "index.html")
        # the app page alone allows inline styles, for Mermaid's drawings (§33); SecurityHeaders
        # adds the strict CSP to everything else, this page's sign-in forms included
        headers = (
            NO_STORE
            if page != "index.html"
            else {**NO_STORE, "content-security-policy": app_csp(state.web_origin)}
        )
        if page == "index.html" and w is not None:
            # The preference reaches <html> in the response, before CSS or app.js can paint.
            # It is an enum checked on write and by the schema, never text from a request.
            prefs = state.store.preferences(w.person_id)
            html = (STATIC_DIR / page).read_text(encoding="utf-8")
            html = html.replace('data-theme="system"', f'data-theme="{prefs["theme"]}"', 1)
            html = html.replace('data-text-size="default"', f'data-text-size="{prefs["text_size"]}"', 1)
            return Response(html, media_type="text/html", headers=headers)
        if page == "setup.html" and w is not None:
            size = state.store.preferences(w.person_id)["text_size"]
            html = (STATIC_DIR / page).read_text(encoding="utf-8")
            return Response(
                html.replace('data-text-size="default"', f'data-text-size="{size}"', 1),
                media_type="text/html",
                headers=headers,
            )
        return FileResponse(STATIC_DIR / page, media_type="text/html", headers=headers)

    @app.get("/board", include_in_schema=False)
    async def board_document() -> Response:
        # Public shell only: no session, room, credentials or data goes into this document.
        return FileResponse(
            STATIC_DIR / "board.html",
            media_type="text/html",
            headers={**NO_STORE, "content-security-policy": BOARD_CSP, "x-frame-options": "SAMEORIGIN"},
        )

    @app.get("/board-assets/{asset:path}", include_in_schema=False)
    async def board_asset(asset: str) -> Response:
        # Only the pinned build's files, never room files or a prefix-wide CORS middleware.
        if asset == "mermaid.min.js":
            return FileResponse(
                STATIC_DIR / "vendor" / "mermaid" / "mermaid.min.js",
                headers={"cache-control": "no-cache", "access-control-allow-origin": "*"},
            )
        root = STATIC_DIR / "vendor" / "board"
        target = (root / asset).resolve()
        if (
            root not in target.parents
            or not target.is_file()
            or target.suffix not in {".js", ".css", ".woff2", ".woff"}
        ):
            return _err(404, "not_found", "not found")
        return FileResponse(target, headers={"cache-control": "no-cache", "access-control-allow-origin": "*"})

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
        return FileResponse(
            STATIC_DIR / "favicon-32.png", media_type="image/png", headers={"Cache-Control": "no-cache"}
        )

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
            # everywhere this person is signed in, and nobody else (§32)
            w = who(request)
            gone = state.store.web_session_delete_person(w.person_id if w is not None else None)
            for g in gone:
                state.hub.close_sessions(g)
            n = len(gone)
            state.store.add_event("login", data={"what": "logout_all", "via": "web", "revoked": n})
        else:
            n = state.sessions.revoke(request.cookies.get(COOKIE_NAME))
            state.hub.close_sessions(h)
        resp = _ok({"ok": True, "revoked": n})
        resp.delete_cookie(
            COOKIE_NAME, path="/", httponly=True, samesite="strict", secure=state.web_origin.secure
        )
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
    async def setup_page(request: Request) -> Response:
        # choosing how you'll sign in (§32.4): the admin while the broker isn't set up, or anyone
        # on a one-time password; otherwise, and on a desktop, just the app
        w = who(request)
        if not claim_open() and not (w is not None and w.must_reset):
            return RedirectResponse("/", status_code=303, headers=NO_STORE)
        if w is not None:
            size = state.store.preferences(w.person_id)["text_size"]
            html = (STATIC_DIR / "setup.html").read_text(encoding="utf-8")
            return Response(
                html.replace('data-text-size="default"', f'data-text-size="{size}"', 1),
                media_type="text/html",
                headers=NO_STORE,
            )
        return FileResponse(STATIC_DIR / "setup.html", media_type="text/html", headers=NO_STORE)

    @app.get("/api/setup/state")
    async def setup_state(request: Request) -> Response:
        """For setup.html (no session needed): ``reset`` for a session on a one-time password,
        ``claim`` for a browser in the claim's ceremony, ``link`` while the broker isn't set up
        (the page has the log's link, and signs in with its one-time password), else ``none``."""
        pk = state.webauthn is not None
        w = who(request)
        if w is not None and w.must_reset:
            email = people.email_of(state, w.person_id)
            first = people.names_of(state, w.person_id)[0]
            return _ok(
                {"mode": "reset", "human": w.name, "email": email, "first_name": first, "passkeys_work": pk}
            )
        if claim_ceremony(request) is not None:
            return _ok({"mode": "claim", "human": state.cfg.human_name, "passkeys_work": pk})
        return _ok({"mode": "link" if claim_open() else "none", "passkeys_work": pk})

    def start_claim(resp: JSONResponse) -> JSONResponse:
        """Bind a claim ceremony to this browser: the one-time password (or the log's link)
        was right, and the owner now chooses a password or a passkey (§32.4). The owner's
        WebAuthn user id is made now and kept only in the ceremony until the claim."""
        claim = state.claim
        assert claim is not None
        cid = secrets.token_urlsafe(32)
        claim.bind(cid, {"fido": None, "handle": secrets.token_bytes(16).hex()})
        _set_ceremony(resp, SETUP_COOKIE, cid, secure=secure())
        return resp

    def claim_ceremony(request: Request) -> dict[str, Any] | None:
        claim = state.claim
        return (
            claim.ceremony(request.cookies.get(SETUP_COOKIE)) if claim is not None and claim.active else None
        )

    @app.post("/api/setup/begin")
    async def setup_begin(request: Request) -> Response:
        """A passkey for the claim: with the log's link (its token), or in the ceremony a
        sign-in with the one-time password started."""
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        claim, wa = state.claim, state.webauthn
        if wa is None:
            return _err(403, "no_passkeys", "passkeys don't work at this address; choose a password")
        cid = request.cookies.get(SETUP_COOKIE)
        cer = claim_ceremony(request)
        if cer is None:
            if claim is None or not claim.active or not claim.check(body.get("token")):
                failed("bad_claim")
                return _err(
                    403,
                    "bad_claim",
                    "This one-time password is wrong, expired or already used. Use the"
                    " newest one in the broker's log.",
                )
            if claim.ceremony_busy(cid):
                return _err(
                    409,
                    "busy",
                    "Someone is setting up this switchboard from another browser right now."
                    " If that isn't you, wait five minutes and try again; if it was, use that"
                    " browser.",
                )
            cid = secrets.token_urlsafe(32)
            cer = {"fido": None, "handle": secrets.token_bytes(16).hex()}
        assert claim is not None and cid is not None
        options, fstate = wa.register_options(bytes.fromhex(cer["handle"]), state.cfg.human_name, [])
        claim.bind(cid, {**cer, "fido": fstate})
        resp = _ok({"options": options, "human": state.cfg.human_name})
        _set_ceremony(resp, SETUP_COOKIE, cid, secure=secure())
        return resp

    def claim_names(body: dict[str, Any]) -> tuple[str | None, str | None, JSONResponse | None]:
        """The admin's first and last name, which setup asks for with their email (§39.6)."""
        first, why = people.clean_name_part(body.get("first_name"))
        if why is None:
            last, why = people.clean_name_part(body.get("last_name"))
        if why is not None:
            return None, None, _err(400, "bad_name", why)
        return first, last, None

    def claim_email(body: dict[str, Any]) -> tuple[str | None, JSONResponse | None]:
        """The admin's email, which setup asks for (#192, §39): who they sign in as from then on.
        Optional here, so the API and older pages still set up; nobody else may have it."""
        email, why = people.clean_email(body.get("email"))
        if why is not None:
            return None, _err(400, "bad_email", why)
        if email is not None and state.store.person_by_email(email) is not None:
            return None, _err(409, "taken", "someone here already has that email")
        return email, None

    @app.post("/api/setup/password")
    async def setup_password(request: Request) -> Response:
        """The claim with a password: the owner's own, chosen in the ceremony a sign-in with
        the one-time password started."""
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        claim, cer = state.claim, claim_ceremony(request)
        if claim is None or cer is None:
            failed("bad_claim")
            return _err(
                403,
                "no_ceremony",
                "No setup is in progress in this browser (it takes five minutes at"
                " most): sign in with the one-time password again.",
            )
        email, no = claim_email(body)
        if no is None:
            first, last, no = claim_names(body)
        if no is not None:
            return no
        why = password_problem(body.get("password"), state.cfg.human_name)
        if why is not None:
            return _err(400, "bad_password", why)
        with db.tx(state.store.con):  # the owner, their email and their password, or none of them
            state.store.claim_owner(bytes.fromhex(cer["handle"]))
            state.store.set_owner_password(hash_password(body["password"]))
            if email is not None:
                state.store.set_email(None, email)
            if first is not None or last is not None:
                state.store.set_names(None, first, last)
        claim.spend()
        state.claimed()
        sid = state.sessions.create("claim")
        state.passkey_checks[sha256_hex(sid)] = state.clock.now()  # choosing it is a check
        state.store.add_event("login", data={"what": "claim", "via": "web", "with": "password"})
        state.hub.notice(None, "warn", "switchboard set up (with a password)")
        log.warning("switchboard set up: the admin chose a password")
        resp = _ok({"ok": True, "human": state.cfg.human_name})
        _set_cookie(resp, sid, secure=secure())
        trust_device(resp, "owner")
        _drop_cookie(resp, SETUP_COOKIE, secure=secure())
        return resp

    @app.post("/api/setup/finish")
    async def setup_finish(request: Request) -> Response:
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        claim, wa = state.claim, state.webauthn
        cer = claim.ceremony(request.cookies.get(SETUP_COOKIE)) if claim is not None else None
        if claim is None or cer is None or wa is None or cer.get("fido") is None:
            failed("bad_claim")
            return _err(
                403,
                "no_ceremony",
                "No setup is in progress in this browser (it takes five minutes at"
                " most): sign in with the one-time password again.",
            )
        email, no = claim_email(body)
        if no is None:
            first, last, no = claim_names(body)
        if no is not None:
            return no
        try:
            reg = wa.register_finish(cer["fido"], body.get("credential"))
        except ValueError as e:
            claim.drop_ceremony()  # the token stays usable until it expires
            failed("bad_claim")
            return _err(400, "bad_credential", f"the passkey could not be verified: {e}")
        name = clean_name(body.get("name"))
        with db.tx(state.store.con):  # the owner, their email and the first passkey, or none of them
            state.store.claim_owner(bytes.fromhex(cer["handle"]))
            state.store.passkey_add(reg.credential_id, reg.public_key, name, reg.aaguid, reg.sign_count)
            if email is not None:
                state.store.set_email(None, email)
            if first is not None or last is not None:
                state.store.set_names(None, first, last)
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
            return _err(
                403,
                "sign_count",
                f'the passkey "{row.name}" sent a signature counter that did not grow;'
                " if you did not just use it elsewhere, it may have been copied",
            )
        if not state.used_challenges.add(st["challenge"]):
            failed("bad_passkey")
            return _err(403, "replay", "this sign-in was already used")
        person = state.store.person(row.person_id) if row.person_id is not None else None
        if row.person_id is not None and (
            person is None or not person.active
        ):  # pragma: no cover - deleted with them
            return _err(403, "bad_credential", "unknown passkey")
        state.store.passkey_used(cred_id, count)
        now = state.clock.now()
        w = who(request)
        if person is not None and person.must_reset:
            # their passkey proves it's them: a one-time password the admin gave them since is not needed
            state.store.person_set_password(person.id, None)
        name = person.name if person is not None else state.cfg.human_name
        resp = _ok({"ok": True, "name": row.name, "reauth": w is not None, "human": name})
        if w is not None and w.person_id == row.person_id:
            # a signed-in browser confirming it's them: a fresh check for this session, no new session
            state.passkey_checks[w.h] = now
            state.store.add_event("login", data={"what": "passkey_check", "passkey": row.name})
        else:
            sid = state.sessions.create(f"passkey:{row.name}", person_id=row.person_id)
            state.passkey_checks[sha256_hex(sid)] = now
            state.store.add_event(
                "login", data={"what": "session", "via": "passkey", "passkey": row.name, "person": name}
            )
            state.hub.notice(None, "warn", f'new web login: {name} (passkey "{row.name}")')
            _set_cookie(resp, sid, secure=secure())
        trust_device(resp, password_bucket(row.person_id), device_id(request, password_bucket(row.person_id)))
        _drop_cookie(resp, PASSKEY_COOKIE, secure=secure())
        return resp

    def add_refused(request: Request) -> tuple[people.Who | None, JSONResponse | None]:
        """The session that may add a passkey now, or why not: signed in (a session still on a
        one-time password may: a passkey instead of a password), passkeys possible here, the
        broker set up, and a passkey or password check in the last five minutes (or the
        claim's own grace)."""
        w = who(request)
        if w is None:
            return None, unauthorized()
        if state.webauthn is None:
            return None, _err(
                403, "no_passkeys", "passkeys need a public https:// URL with a DNS name (docs/DEPLOY.md)"
            )
        if state.store.owner_handle() is None:
            return None, _err(
                409, "unclaimed", "set this switchboard up with the one-time password in its log first"
            )
        if not state.fresh_check(w.h):
            return None, _err(403, "reauth", "confirm it's you first")
        return w, None

    def handle_of(w: people.Who) -> bytes:
        if w.person_id is None:
            handle = state.store.owner_handle()
            assert handle is not None
            return handle
        p = state.store.person(w.person_id)
        assert p is not None
        return p.handle

    @app.post("/api/passkeys/begin")
    async def passkeys_begin(request: Request) -> Response:
        w, no = add_refused(request)
        if no is not None:
            return no
        assert w is not None
        wa = state.webauthn
        assert wa is not None
        options, fstate = wa.register_options(
            handle_of(w), w.name, [pk.credential_id for pk in state.store.passkeys_of(w.person_id)]
        )
        resp = _ok({"options": options})
        _set_ceremony(resp, ADD_COOKIE, state.sealer.seal(fstate, CEREMONY_TTL_S), secure=secure())
        return resp

    @app.post("/api/passkeys")
    async def passkeys_add(request: Request) -> Response:
        w, no = add_refused(request)
        if no is not None:
            return no
        assert w is not None
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        wa = state.webauthn
        assert wa is not None
        st = state.sealer.unseal(request.cookies.get(ADD_COOKIE))
        if st is None:
            return _err(
                403, "no_ceremony", "adding the passkey took too long, or its cookie is missing: try again"
            )
        try:
            reg = wa.register_finish(st, body.get("credential"))
        except ValueError as e:
            return _err(400, "bad_credential", f"the passkey could not be verified: {e}")
        if state.store.passkey(reg.credential_id) is not None:
            return _err(409, "conflict", "that passkey is registered already")
        name = clean_name(body.get("name"))
        with db.tx(state.store.con):
            state.store.passkey_add(
                reg.credential_id, reg.public_key, name, reg.aaguid, reg.sign_count, person_id=w.person_id
            )
            if w.must_reset and w.person_id is not None:
                state.store.person_set_password(w.person_id, None)  # a passkey instead of a password
        state.store.add_event("login", data={"what": "passkey_added", "passkey": name, "person": w.name})
        state.hub.notice(None, "info", f'passkey added by {w.name} ("{name}")')
        resp = _ok({"ok": True, "name": name, "passkeys": len(state.store.passkeys_of(w.person_id))})
        _drop_cookie(resp, ADD_COOKIE, secure=secure())
        return resp

    # ---------------------------------------------------------- passwords (§32.4)
    def password_bucket(person_id: int | None) -> str:
        return "owner" if person_id is None else f"person:{person_id}"

    @app.post("/api/signin/password")
    async def signin_password(request: Request) -> Response:
        """An email (or a name) and a password. The owner's one-time password from the log starts
        the claim (``next: setup``); a person's one-time password gives a session that must
        choose its own first (``next: setup``); anything else that's right is a session
        (``next: app``). Who an email or a name means (#192, §39): ``people.account``. Exactly
        one scrypt check runs either way (#179): the real hash when there's an account that has
        one to check, the dummy hash otherwise — never both, never neither — so a wrong password
        costs the same whether it's a real person's, the admin's with no password yet, one typed
        during an open claim, or nobody's."""
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        if not state.hosted:
            return _err(403, "no_passwords", "this switchboard signs in with `switchboard login`")
        raw, pw = body.get("name"), body.get("password")
        name = raw.strip().lower()[:254] if isinstance(raw, str) else ""
        kind, who_p = people.account(state, name)
        key = (
            password_bucket(who_p.id if who_p is not None else None)
            if kind != "none"
            else f"name:{name or '?'}"
        )
        trusted = device_id(request, key)
        budget = f"device:{trusted}" if trusted is not None else key
        wait = state.signin.wait_s(budget)
        if wait > 0:
            return _err(429, "slow_down", f"too many wrong tries: wait {int(wait) + 1} s and try again")
        refused = refuse_check(budget, trusted is not None)
        if refused is not None:
            return refused
        if kind == "owner":
            stored, typed = state.store.owner_password_hash(), pw
        elif kind == "person":
            assert who_p is not None
            stored = who_p.password_hash
            typed = (normalize_one_time(pw) or pw) if who_p.must_reset else pw
        else:
            stored, typed = None, pw
        password_ok = await check_password(typed, stored, budget, trusted is not None)
        # nothing below awaits until the outcome is recorded (``check_password``); a password
        # changed while this one was checked doesn't count
        if kind == "owner":
            password_ok = password_ok and state.store.owner_password_hash() == stored
        elif kind == "person":
            assert who_p is not None
            current = state.store.person(who_p.id)
            password_ok = password_ok and current is not None and current.password_hash == stored
        now = state.clock.now()
        resp: JSONResponse | None = None
        if kind == "owner":
            if state.claim is not None and state.claim.active and state.claim.check(pw):
                if state.claim.ceremony_busy(request.cookies.get(SETUP_COOKIE)):
                    return _err(
                        409, "busy", "Someone is setting up this switchboard from another browser right now."
                    )
                state.signin.ok(budget)
                return start_claim(
                    _ok(
                        {
                            "ok": True,
                            "next": "setup",
                            "human": state.cfg.human_name,
                            "passkeys": state.webauthn is not None,
                        }
                    )
                )
            if password_ok:
                resp = signed_in(None, state.cfg.human_name, "password", now)
        elif kind == "person":
            person = who_p
            assert person is not None
            if person.must_reset:
                if password_ok:
                    if person.password_expires_at is not None and now >= person.password_expires_at:
                        state.signin.failed(budget)
                        return _err(
                            403, "expired", "this one-time password has expired: ask the admin for a new one"
                        )
                    resp = signed_in(person.id, person.name, "one-time", now, reset=True)
            elif password_ok:
                resp = signed_in(person.id, person.name, "password", now)
        if resp is None:
            state.signin.failed(budget)
            failed("bad_password")
            # the same words whoever it was, so a sign-in never says which emails are someone's
            return _err(403, "bad_password", WRONG_SIGNIN)
        state.signin.ok(budget)
        trust_device(resp, key, trusted)
        return resp

    def signed_in(
        person_id: int | None, name: str, via: str, now: float, *, reset: bool = False
    ) -> JSONResponse:
        sid = state.sessions.create(via, person_id=person_id)
        state.passkey_checks[sha256_hex(sid)] = now  # a password just typed is a fresh check
        state.store.add_event("login", data={"what": "session", "via": via, "person": name})
        state.hub.notice(None, "warn", f"new web login: {name}" + (" (one-time password)" if reset else ""))
        resp = _ok(
            {
                "ok": True,
                "next": "setup" if reset else "app",
                "human": name,
                "passkeys": state.webauthn is not None,
            }
        )
        _set_cookie(resp, sid, secure=secure())
        return resp

    @app.post("/api/signin/check")
    async def signin_check(request: Request) -> Response:
        """A signed-in browser confirms it's them with their password: a fresh check, as a
        passkey gives one (to add a passkey, add a person or make an agent token)."""
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        key = f"check:{w.h}"
        wait = state.signin.wait_s(key)
        if wait > 0:
            return _err(429, "slow_down", f"too many wrong tries: wait {int(wait) + 1} s and try again")
        refused = refuse_check(key, True)
        if refused is not None:
            return refused
        stored = (
            state.store.owner_password_hash()
            if w.person_id is None
            else getattr(state.store.person(w.person_id), "password_hash", None)
        )
        if not await check_password(body.get("password"), stored, key, True):
            state.signin.failed(key)
            failed("bad_password")
            return _err(403, "bad_password", "wrong password")
        state.signin.ok(key)
        state.passkey_checks[w.h] = state.clock.now()
        state.store.add_event("login", data={"what": "password_check", "person": w.name})
        return _ok({"ok": True})

    @app.post("/api/me/password")
    async def my_password(request: Request) -> Response:
        """Choose your own password: right after a one-time password, or later to change it
        (a password or passkey check in the last five minutes)."""
        w = who(request)
        if w is None or not state.hosted:
            return unauthorized()
        if not w.must_reset and not state.fresh_check(w.h):
            return _err(403, "reauth", "confirm it's you first")
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        why = password_problem(body.get("password"), w.name)
        if why is not None:
            return _err(400, "bad_password", why)
        hashed = hash_password(body["password"])
        if w.person_id is None:
            state.store.set_owner_password(hashed)
        else:
            state.store.person_set_password(w.person_id, hashed)
        state.store.add_event("login", data={"what": "password_set", "person": w.name})
        return _ok({"ok": True})

    # ---------------------------------------------------------- the admin section (§32.3)
    def admin(request: Request, fresh: bool) -> tuple[people.Who | None, JSONResponse | None]:
        w = who(request)
        if w is None or w.must_reset:
            return None, unauthorized()
        if not state.hosted:
            return None, _err(404, "not_found", "people sign in to a hosted switchboard only")
        if not w.is_admin:
            return None, _err(403, "forbidden", "only an admin manages people")
        if fresh and not state.fresh_check(w.h):
            return None, _err(403, "reauth", "confirm it's you first")
        return w, None

    @app.post("/api/people/{pid}/email")
    async def people_email(request: Request, pid: str) -> Response:
        """The email a person (or the admin, ``owner``) is, and signs in with; or none (#192)."""
        w, no = admin(request, True)
        if no is not None:
            return no
        assert w is not None
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        email, why = people.clean_email(body.get("email"))
        if why is not None:
            return _err(400, "bad_request", why)
        if pid == "owner":
            person_id, name = None, state.cfg.human_name
        else:
            p = (
                state.store.person(int(pid))
                if ROOM_ID_RE.fullmatch(pid) and int(pid) <= MAX_ROOM_ID
                else None
            )
            if p is None or not p.active:
                return _err(404, "not_found", "no such person")
            person_id, name = p.id, p.name
        try:
            state.store.set_email(person_id, email)
        except Conflict as e:
            return _err(409, "taken", str(e))
        state.store.add_event(
            "people", data={"what": "email", "person": name, "by": w.name, "set": bool(email)}
        )
        state.hub.notice(
            None,
            "info",
            f"{w.name} " + (f"set {name}'s email" if email else f"removed {name}'s email"),
        )
        return _ok({"person": next(x for x in people.summary(state) if x["id"] == person_id)})

    @app.get("/api/people")
    async def people_list(request: Request) -> Response:
        _w, no = admin(request, False)
        if no is not None:
            return no
        return _ok({"people": people.summary(state), "origin": state.web_origin.origin})

    @app.post("/api/people")
    async def people_add(request: Request) -> Response:
        w, no = admin(request, True)  # a new way in: a fresh check, as adding a passkey
        if no is not None:
            return no
        assert w is not None
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        # an email's local part wins over a first name (#563); with no email, the first name
        email, why = people.clean_email(body.get("email"))
        if why is not None:
            return _err(400, "bad_email", why)
        first, why = people.clean_name_part(body.get("first_name"))
        if why is None:
            last, why = people.clean_name_part(body.get("last_name"))
        if why is not None:
            return _err(400, "bad_name", why)
        raw = body.get("name")
        derived = (first is not None or email is not None) and (
            raw is None or (isinstance(raw, str) and not raw.strip())
        )
        if derived:
            raw = people.name_for(state, first, last, email)
        why = people.name_problem(state, raw)
        if why is not None:
            if derived:
                why = f"{raw or 'that email'} can't be their name ({why}): choose one for them"
            return _err(400, "bad_name", why)
        assert isinstance(raw, str)
        name = raw.strip().lower()
        one_time = one_time_password()
        try:
            p = state.store.person_add(
                name,
                secrets.token_bytes(16),
                hash_password(normalize_one_time(one_time) or ""),
                people.ONE_TIME_TTL_S,
                email=email,
                first_name=first,
                last_name=last,
            )
        except Conflict as e:
            return _err(409, "taken", str(e))
        except ValueError as e:
            return _err(409, "conflict", str(e))
        state.store.add_event("people", data={"what": "add", "person": name, "by": w.name})
        state.hub.notice(None, "info", f"{name} added by {w.name}")
        people_changed(state)
        return _ok(
            {
                "person": next(x for x in people.summary(state) if x["id"] == p.id),
                "password": one_time,
                "invite": people.invite_text(state.web_origin.origin, name, one_time, email, first),
            }
        )

    @app.post("/api/people/{pid}/password")
    async def people_password(request: Request, pid: str) -> Response:
        w, no = admin(request, True)
        if no is not None:
            return no
        assert w is not None
        try:
            await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        p = state.store.person(int(pid)) if ROOM_ID_RE.fullmatch(pid) and int(pid) <= MAX_ROOM_ID else None
        if p is None or not p.active:
            return _err(404, "not_found", "no such person")
        one_time = one_time_password()
        gone = state.store.person_one_time_password(
            p.id, hash_password(normalize_one_time(one_time) or ""), people.ONE_TIME_TTL_S
        )
        for g in gone or []:
            state.hub.close_sessions(g)
        state.store.add_event("people", data={"what": "one_time_password", "person": p.name, "by": w.name})
        state.hub.notice(None, "info", f"{w.name} gave {p.name} a new one-time password")
        return _ok(
            {
                "person": next(x for x in people.summary(state) if x["id"] == p.id),
                "password": one_time,
                "invite": people.invite_text(
                    state.web_origin.origin, p.name, one_time, p.email, p.first_name
                ),
            }
        )

    @app.post("/api/people/{pid}/remove")
    async def people_remove(request: Request, pid: str) -> Response:
        w, no = admin(request, False)  # removing only takes access away
        if no is not None:
            return no
        assert w is not None
        try:
            await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        p = state.store.person(int(pid)) if ROOM_ID_RE.fullmatch(pid) and int(pid) <= MAX_ROOM_ID else None
        if p is None or not p.active:
            return _err(404, "not_found", "no such person")
        devices = [t.device for t in state.store.agent_tokens_of(p.id)]  # revoked with them (§49.1)
        gone = state.store.person_remove(p.id) or []
        # their agents over HTTPS leave, as on a revoke (§49.3)
        ended = sum(state.agents.end_device(d, "token revoked") for d in devices)
        state.service.person_gone(p.id)  # out of every private room, with their agents (§44.3)
        for g in gone:
            state.hub.close_sessions(g)
        state.store.add_event(
            "people",
            data={
                "what": "remove",
                "person": p.name,
                "by": w.name,
                "sessions": len(gone),
                "agent_devices": devices,
                "agents_ended": ended,
            },
        )
        state.hub.notice(None, "warn", f"{p.name} removed by {w.name}: signed out everywhere")
        people_changed(state)
        return _ok({"ok": True, "sessions": len(gone)})

    @app.post("/api/people/{pid}/admin")
    async def people_admin(request: Request, pid: str) -> Response:
        """Make someone an admin, or take it away (#569, §51). Granting access needs a fresh
        check. Taking it away needs none: it only takes access away (§32.3). The owner stays
        an admin, and nobody can remove them."""
        w, no = admin(request, False)
        if no is not None:
            return no
        assert w is not None
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        if set(body) != {"admin"} or not isinstance(body.get("admin"), bool):
            return _err(400, "bad_request", "admin must be true or false")
        grant = body["admin"]
        if pid in ("owner", "0"):
            return _err(403, "forbidden", "nobody removes the owner or the owner's admin")
        p = state.store.person(int(pid)) if ROOM_ID_RE.fullmatch(pid) and int(pid) <= MAX_ROOM_ID else None
        if p is None or not p.active:
            return _err(404, "not_found", "no such person")
        if grant and not state.fresh_check(w.h):
            return _err(403, "reauth", "confirm it's you first")
        if p.admin != grant:
            if not state.store.person_set_admin(p.id, grant):
                return _err(404, "not_found", "no such person")
            state.store.add_event(
                "people", data={"what": "admin", "person": p.name, "by": w.name, "admin": grant}
            )
            verb = f"made {p.name} an admin" if grant else f"took {p.name}'s admin away"
            state.hub.notice(None, "info", f"{w.name} {verb}")
            people_changed(state)
        return _ok({"person": next(x for x in people.summary(state) if x["id"] == p.id)})

    @app.get("/api/auth/state")
    async def auth_state() -> Response:
        # for the sign-in page (no session): what applies here, and nothing about anyone
        return _ok(
            {
                "hosted": state.web_origin.public,
                "claimed": state.store.owner_handle() is not None,
                "passkeys": passkeys_on(),
                "passkeys_work": state.webauthn is not None,
                "claim": claim_open(),
                "password": state.hosted,
                "sso": state.oidc.cfg.provider if state.oidc is not None else "coming soon",
            }
        )

    # -------------------------------------------- Sign in with Google (#70, §38)
    # Only a person the admin added, matched by the Google email the admin set for them.
    # A failure goes back to the sign-in page with a code it explains; details go to the log.
    def sso_fail(code: str) -> Response:
        return RedirectResponse("/?sso=" + code, status_code=303, headers=NO_STORE)

    def sso_redirect_uri() -> str:
        return state.web_origin.origin + oidc.CALLBACK_PATH

    @app.get("/auth/oidc/start", include_in_schema=False)
    async def oidc_start() -> Response:
        if state.oidc is None:
            return _err(404, "not_found", "single sign-on isn't set up here")
        bind = secrets.token_urlsafe(32)
        try:
            url, _state = await asyncio.to_thread(state.oidc.start, sso_redirect_uri(), sha256_hex(bind))
        except oidc.OidcError as e:
            log.warning("sign-in with %s: %s", state.oidc.cfg.provider, e)
            return sso_fail(e.code)
        resp = RedirectResponse(url, status_code=303, headers=NO_STORE)
        # Lax, not Strict: the callback is a top-level navigation back from the provider
        resp.set_cookie(
            OIDC_COOKIE,
            bind,
            max_age=int(oidc.FLOW_TTL_S),
            path=oidc.CALLBACK_PATH,
            httponly=True,
            samesite="lax",
            secure=secure(),
        )
        return resp

    @app.get(oidc.CALLBACK_PATH, include_in_schema=False)
    async def oidc_callback(request: Request) -> Response:
        if state.oidc is None:
            return _err(404, "not_found", "single sign-on isn't set up here")
        q = request.query_params
        if q.get("error"):
            return sso_fail("denied")
        bind = request.cookies.get(OIDC_COOKIE) or ""
        try:
            email = await asyncio.to_thread(
                state.oidc.finish,
                q.get("state") or "",
                q.get("code") or "",
                sso_redirect_uri(),
                sha256_hex(bind),
            )
        except oidc.OidcError as e:
            # rate-limited like a bad login token: anyone can call this with a made-up state
            log.debug("sign-in with %s: %s", state.oidc.cfg.provider, e)
            failed("sso_" + e.code)
            return sso_fail(e.code)
        person = state.store.person_by_email(email)
        if email == state.store.owner_email() and state.store.owner_handle() is not None:
            person_id, name = None, state.cfg.human_name
        elif person is not None:
            person_id, name = person.id, person.name
            if person.must_reset:  # the invite's one-time password: Google is their way in now
                state.store.retire_one_time(person.id)
        else:
            failed("sso_not_allowed")
            return sso_fail("not_allowed")
        sid = state.sessions.create(state.oidc.cfg.provider, person_id=person_id)
        state.passkey_checks[sha256_hex(sid)] = state.clock.now()  # just confirmed with the provider
        state.store.add_event(
            "login", data={"what": "session", "via": state.oidc.cfg.provider, "person": name}
        )
        state.hub.notice(None, "warn", f"new web login: {name} ({state.oidc.cfg.provider})")
        # A page, not a redirect: the session cookie is SameSite=Strict, and a browser doesn't
        # send it on a redirect that a cross-site navigation (back from the provider) started.
        resp = HTMLResponse(SSO_DONE_PAGE, headers=NO_STORE)
        _set_cookie(resp, sid, secure=secure())
        resp.delete_cookie(OIDC_COOKIE, path=oidc.CALLBACK_PATH)
        return resp

    # -------------------------------------------------- agent tokens (#382, §49.1)
    # Each person's own: a desktop's owner, or anyone signed in to a hosted broker. Making one
    # grants access, so a hosted broker wants a check in the last five minutes, as adding a person does.
    @app.get("/api/agent-tokens")
    async def agent_tokens_list(request: Request) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        return _ok(agent_tokens.listing(state, w.person_id))

    @app.post("/api/agent-tokens")
    async def agent_tokens_make(request: Request) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        if state.hosted and not state.fresh_check(w.h):
            return _err(403, "reauth", "confirm it's you first")
        try:
            body = await _json_body(request)
            return _ok(agent_tokens.make(state, w.person_id, body.get("device"), w.name))
        except ServiceError as e:
            return _svc_err(e)

    @app.post("/api/agent-tokens/{tid}/revoke")
    async def agent_tokens_revoke(request: Request, tid: str) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            await _json_body(request)
            n = int(tid) if ROOM_ID_RE.fullmatch(tid) and int(tid) <= MAX_ROOM_ID else 0
            return _ok(agent_tokens.revoke(state, w.person_id, n, w.name))
        except ServiceError as e:
            return _svc_err(e)

    # ------------------------------------- browser sign-in for agents (#386, DESIGN.md §49.9)
    # The two documents an agent reads first (RFC 9728, RFC 8414): public, the same for everyone.
    @app.get(oauth.PRM_PATH, include_in_schema=False)
    @app.get(oauth.PRM_PATH + "/mcp", include_in_schema=False)
    async def oauth_protected_resource() -> Response:
        return _ok(oauth.protected_resource(state))

    @app.get(oauth.AS_PATH, include_in_schema=False)
    async def oauth_authorization_server() -> Response:
        return _ok(oauth.authorization_server(state))

    def oauth_items(request: Request) -> list[tuple[str, str]]:
        """The authorize request's parameters, in order, with repeats kept: ``parse_authorize`` refuses
        those. Too many or malformed ones are an error the person is shown."""
        try:
            return urllib.parse.parse_qsl(request.url.query, keep_blank_values=True, max_num_fields=40)
        except ValueError:
            raise oauth.OAuthError("invalid_request", "the request is malformed") from None

    @app.post(oauth.REGISTER_PATH, include_in_schema=False)
    async def oauth_register(request: Request) -> Response:
        """RFC 7591, for a program: a client says its name and where its answer goes (loopback only)
        and gets an id. No cookie is read, and a request with an Origin (a browser's script) is refused."""
        if request.headers.get("origin") is not None:
            return _oauth_err(403, "access_denied", "agents only: a browser can't register a client")
        raw = await _capped_body(request, oauth.BODY_MAX)
        if raw is None:
            return _oauth_err(413, "invalid_client_metadata", "the registration is too long")
        if not request.headers.get("content-type", "").lower().startswith("application/json"):
            return _oauth_err(400, "invalid_client_metadata", "send application/json")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("not an object")
            made = oauth.register(state, body)
        except ValueError:
            return _oauth_err(400, "invalid_client_metadata", "the registration is not a JSON object")
        except oauth.OAuthError as e:
            return _oauth_err(e.status, e.code, e.description)
        return JSONResponse(made, status_code=201, headers={**NO_STORE, "Pragma": "no-cache"})

    @app.post(oauth.TOKEN_PATH, include_in_schema=False)
    async def oauth_token(request: Request) -> Response:
        """RFC 6749's token endpoint for the one grant: a code and its PKCE verifier for the device's
        agent token. No cookie is read, and a request with an Origin is refused."""
        if request.headers.get("origin") is not None:
            return _oauth_err(403, "access_denied", "agents only: a browser can't ask for a token")
        raw = await _capped_body(request, oauth.BODY_MAX)
        if raw is None:
            return _oauth_err(413, "invalid_request", "the request is too long")
        kind = request.headers.get("content-type", "").lower()
        if not kind.startswith("application/x-www-form-urlencoded"):
            return _oauth_err(400, "invalid_request", "send application/x-www-form-urlencoded")
        try:
            items = urllib.parse.parse_qsl(
                raw.decode("ascii"), keep_blank_values=True, strict_parsing=bool(raw), max_num_fields=20
            )
        except (ValueError, UnicodeDecodeError):
            return _oauth_err(400, "invalid_request", "the request is malformed")
        form = dict(items)
        if len(form) != len(items):
            return _oauth_err(400, "invalid_request", "a parameter was sent twice")
        try:
            token = oauth.exchange(state, form)
        except oauth.OAuthError as e:
            if e.code == "invalid_grant":
                failed("bad_oauth_code")
            return _oauth_err(e.status, e.code, e.description)
        return JSONResponse(token, headers={**NO_STORE, "Pragma": "no-cache"})

    oauth_page = STATIC_DIR / "oauth.html"

    @app.get(oauth.AUTHORIZE_PATH, include_in_schema=False)
    async def oauth_authorize(request: Request) -> Response:
        """Where an agent sends its person's browser. A request that can't be answered to the agent is
        shown to the person (400); one the agent can be told about is sent back to it; a good one is
        the consent page, after signing in if need be."""
        try:
            oauth.parse_authorize(state, oauth_items(request))
        except oauth.OAuthError as e:
            if e.redirect_uri is not None:
                return RedirectResponse(oauth.error_redirect(e), status_code=303, headers=NO_STORE)
            return FileResponse(oauth_page, status_code=400, media_type="text/html", headers=NO_STORE)
        resp = FileResponse(oauth_page, media_type="text/html", headers=NO_STORE)
        w = who(request)
        query = request.url.query
        if (w is None or w.must_reset) and oauth.NEXT_QUERY_RE.fullmatch(query):
            # signing in lands on "/": this brings the browser back here (the consent page checks
            # the request again, so the cookie is only a way back, never an approval)
            resp.set_cookie(
                OAUTH_NEXT_COOKIE,
                state.sealer.seal({"kind": "oauth_next", "q": query}, oauth.NEXT_TTL_S),
                max_age=int(oauth.NEXT_TTL_S),
                path="/",
                httponly=True,
                samesite="strict",
                secure=secure(),
            )
        return resp

    @app.get("/api/oauth/consent")
    async def oauth_consent(request: Request) -> Response:
        """What the consent page shows: the request checked again, and the person it is for. A browser
        with no session gets ``signed_in: false`` (a 200, since the page asks again every few seconds
        until someone signs in, and a refused request is a console error each time)."""
        try:
            req = oauth.parse_authorize(state, oauth_items(request))
        except oauth.OAuthError as e:
            return _err(400, e.code, e.description)
        w = who(request)
        if w is None or w.must_reset:
            return _ok({"signed_in": False})
        return _ok({"signed_in": True, **oauth.consent_view(state, req, w)})

    async def oauth_decision(
        request: Request,
    ) -> tuple[oauth.AuthRequest, people.Who, dict[str, Any]] | Response:
        """The session, and the request named in the body (its query, checked again), for approve and
        deny. An agent token is no session: only a person's cookie gets here."""
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            query = body.get("query")
            if not isinstance(query, str) or len(query) > oauth.QUERY_MAX:
                raise ServiceError("bad_request", "the sign-in request is missing")
            items = urllib.parse.parse_qsl(query.lstrip("?"), keep_blank_values=True, max_num_fields=40)
            return oauth.parse_authorize(state, items), w, body
        except oauth.OAuthError as e:
            return _err(400, e.code, e.description)
        except (ServiceError, ValueError) as e:
            return _err(400, "bad_request", str(e))

    @app.post("/api/oauth/approve")
    async def oauth_approve(request: Request) -> Response:
        """The person says yes (a click on the consent page, with the page's Origin, X-Switchboard
        and their cookie): the one-time code, as the address to send the browser to. A hosted broker
        wants a password or passkey check in the last five minutes first, as Make a token does."""
        got = await oauth_decision(request)
        if isinstance(got, Response):
            return got
        req, w, body = got
        if state.hosted and not state.fresh_check(w.h):
            return _err(403, "reauth", "confirm it's you first")
        try:
            return _ok({"redirect": oauth.approve(state, req, w, body.get("device"))})
        except ServiceError as e:
            return _svc_err(e)

    @app.post("/api/oauth/deny")
    async def oauth_deny(request: Request) -> Response:
        got = await oauth_decision(request)
        if isinstance(got, Response):
            return got
        return _ok({"redirect": oauth.denied_redirect(got[0])})

    # --------------------------------------------------------------- api
    @app.get("/api/me")
    async def me(request: Request) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        has_password = (
            state.store.owner_password_hash() is not None
            if w.person_id is None
            else getattr(state.store.person(w.person_id), "password_hash", None) is not None
        )
        resp = _ok(
            {
                "human": w.name,
                "person_id": state.service.person_key(w.person_id),
                "email": people.email_of(state, w.person_id),  # who signs in (#192, §39)
                "first_name": people.names_of(state, w.person_id)[0],  # §39.6
                "last_name": people.names_of(state, w.person_id)[1],
                # everyone's full name by their name in the rooms: Members and a message's sender
                "full_names": people.full_names(state),
                # the owner's name is fixed by SWITCHBOARD_HUMAN_NAME: Settings can't rename it (#114)
                "name_locked": w.owner and state.cfg.human_name_from_env,
                "admin": w.is_admin and state.hosted,  # the admin section (§32.3, §51)
                # may this person upload the agents' avatars (§29.11)? Any admin: on a desktop
                # broker the one person signed in, on a hosted one an admin (§51)
                "can_set_icons": w.is_admin,
                "can_backup": w.is_admin,
                "version": __version__,
                "commit": build_info.commit(),
                "test_mode": state.test_mode,
                "port": state.info.port,
                # the passkeys sheet (§31.4): shown behind a public URL where passkeys work
                "hosted": state.webauthn is not None,
                "signin": state.hosted,  # passwords and people (§32)
                "sso": state.oidc.cfg.provider if state.oidc is not None else None,  # §38
                "passkeys": len(state.store.passkeys_of(w.person_id)),
                "password": has_password,
                "fresh": state.fresh_check(w.h),
                "preferences": state.store.preferences(w.person_id),
                # what a new room this person creates would start with, and where it comes
                # from (#131): shown in Settings beside the budget/hop-limit defaults
                "wake_defaults": state.service.wake_defaults(w.person_id),
            }
        )
        # Slide the browser cookie along with the server-side session.
        _set_cookie(resp, request.cookies.get(COOKIE_NAME) or "", secure=state.web_origin.secure)
        return resp

    def rename_route(body: dict[str, Any], person_id: int | None, by: str) -> Response:
        """Rename someone and set their first and last name (#114, §41): ``name``, ``first_name``
        and ``last_name``, each optional (a field left out stays as it is)."""
        if not body or set(body) - {"name", "first_name", "last_name"}:
            return _err(400, "bad_request", "give name, first_name or last_name")
        parts: dict[str, str | None] = {}
        for key in ("first_name", "last_name"):
            if key in body:
                value, why = people.clean_name_part(body[key])
                if why is not None:
                    return _err(400, "bad_name", why)
                parts[key] = value
        new = None
        if "name" in body:
            why = people.rename_problem(state, body["name"], person_id)
            if why is not None:
                return _err(400, "bad_name", why)
            new = body["name"].strip().lower()
            if person_id is None and state.cfg.human_name_from_env and new != state.cfg.human_name:
                return _err(409, "locked", "SWITCHBOARD_HUMAN_NAME sets this name when switchboard starts")
        if parts:
            first, last = people.names_of(state, person_id)
            state.store.set_names(person_id, parts.get("first_name", first), parts.get("last_name", last))
        if new is not None:
            try:
                people.rename(state, person_id, new, by)
            except ValueError as e:
                if parts:
                    people_changed(state)  # the first or last name was saved before the conflict
                return _err(409, "conflict", str(e))
        # a new first or last name has to reach open pages (#563); a username change already did
        if parts or new is not None:
            people_changed(state)
        first, last = people.names_of(state, person_id)
        name = (
            state.cfg.human_name if person_id is None else getattr(state.store.person(person_id), "name", "")
        )
        return _ok({"name": name, "first_name": first, "last_name": last})

    @app.post("/api/me/name")
    async def me_name(request: Request) -> Response:
        """Rename yourself, and your first and last name (#114). A new name in the rooms on a
        hosted broker needs a fresh check, as a password does: it's what you sign in with until
        you have an email, and what every agent takes your messages under."""
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        renaming = "name" in body and body["name"] != w.name
        if renaming and state.hosted and not state.fresh_check(w.h):
            return _err(403, "reauth", "confirm it's you first")
        return rename_route(body, w.person_id, w.name)

    @app.post("/api/people/{pid}/name")
    async def people_name(request: Request, pid: str) -> Response:
        """The admin renames someone (or themselves, ``owner``), with a fresh check (#114)."""
        w, no = admin(request, True)
        if no is not None:
            return no
        assert w is not None
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        if pid == "owner":
            return rename_route(body, None, w.name)
        p = state.store.person(int(pid)) if ROOM_ID_RE.fullmatch(pid) and int(pid) <= MAX_ROOM_ID else None
        if p is None or not p.active:
            return _err(404, "not_found", "no such person")
        return rename_route(body, p.id, w.name)

    # ------------------------------------------------ the agents' avatars (DESIGN.md §29.11, #498)
    # Everyone signed in reads them (the page's stylesheet and the images it names); an admin (a
    # desktop broker's one person, a hosted broker's admin) uploads or resets. The check is the bytes'.
    def icon_session(request: Request) -> tuple[people.Who | None, Response | None]:
        w = who(request)
        if w is None or w.must_reset:
            return None, unauthorized()
        return w, None

    def icon_admin(request: Request) -> tuple[people.Who | None, Response | None]:
        w, no = icon_session(request)
        if no is None and w is not None and not w.is_admin:
            return None, _err(403, "forbidden", "only an admin changes the agents' avatars")
        return w, no

    def icon_unknown(harness: str, scheme: str | None = None) -> Response | None:
        if harness not in agent_icons.SLOTS or (scheme is not None and scheme not in agent_icons.SCHEMES):
            return _err(404, "not_found", "no such avatar")
        return None

    @app.get("/api/agent-icons")
    async def agent_icons_list(request: Request) -> Response:
        """What is uploaded (never the bytes), what may be, and whether this person may change it."""
        w, no = icon_session(request)
        if no is not None or w is None:
            return no or unauthorized()
        return _ok(
            {
                "can_change": w.is_admin,
                "harnesses": list(agent_icons.HARNESSES),
                "platforms": list(agent_icons.PLATFORMS),  # the calls' icons, uploaded the same way (#623)
                "limits": {
                    "bytes": agent_icons.MAX_BYTES,
                    "side": agent_icons.MAX_SIDE,
                    "types": list(agent_icons.TYPES),
                },
                "icons": [agent_icons.icon_json(i) for i in state.store.agent_icons()],
            }
        )

    @app.get("/api/agent-icons.css", include_in_schema=False)
    async def agent_icons_css(request: Request) -> Response:
        """The page's stylesheet for the uploaded avatars, linked after ``style.css``."""
        _w, no = icon_session(request)
        if no is not None:
            return no
        return Response(
            agent_icons.stylesheet(state.store.agent_icons()),
            media_type="text/css",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/agent-icons/{harness}/{scheme}")
    async def agent_icon_get(request: Request, harness: str, scheme: str) -> Response:
        _w, no = icon_session(request)
        if no is not None:
            return no
        got = None if icon_unknown(harness, scheme) is not None else state.store.agent_icon(harness, scheme)
        if got is None:
            return _err(404, "not_found", "no such avatar")
        icon, data = got
        # the URL carries the image's hash: a request that has the current one may be cached for good
        # (an upload changes the URL), any other is revalidated, so a stale link can't pin old bytes
        cache = (
            "private, max-age=31536000, immutable"
            if request.query_params.get("v") == icon.version
            else "private, no-cache"
        )
        return Response(
            data,
            media_type=icon.mime,
            headers={
                "Cache-Control": cache,
                # every response gets nosniff from SecurityHeaders; said here too, since an image served
                # under another type than its bytes is the whole risk of an upload
                "X-Content-Type-Options": "nosniff",
                # opened on its own, the response is inert: no script, no frame, no embedded anything
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @app.put("/api/agent-icons/{harness}/{scheme}")
    async def agent_icon_put(request: Request, harness: str, scheme: str) -> Response:
        """Upload the avatar for ``harness`` in ``scheme``: the request body is the file, its
        Content-Type what it says it is. Judged by its bytes (``agent_icons.check``) before it is kept."""
        w, no = icon_admin(request)
        if no is not None or w is None:
            return no or unauthorized()
        if (bad := icon_unknown(harness, scheme)) is not None:
            return bad
        raw = await _capped_body(request, agent_icons.MAX_BYTES)
        try:
            if raw is None:
                declared = request.headers.get("content-length", "")
                raise agent_icons.too_large(int(declared) if declared.isdigit() else None)
            got = agent_icons.check(raw, request.headers.get("content-type"))
        except agent_icons.IconError as e:
            return _err(e.status, e.code, e.message)
        icon = state.store.agent_icon_set(harness, scheme, got.mime, raw, got.width, got.height, got.sha256)
        state.store.add_event(
            "icons",
            data={
                "what": "set",
                "harness": harness,
                "scheme": scheme,
                "by": w.name,
                "type": icon.mime,
                "bytes": icon.size,
                "sha": icon.version,
            },
        )
        icons_changed(state)
        return _ok(agent_icons.icon_json(icon))

    @app.delete("/api/agent-icons/{harness}")
    async def agent_icon_reset(request: Request, harness: str) -> Response:
        """Reset ``harness``'s avatars, light and dark: the built-in one comes back."""
        w, no = icon_admin(request)
        if no is not None or w is None:
            return no or unauthorized()
        if (bad := icon_unknown(harness)) is not None:
            return bad
        removed = state.store.agent_icon_reset(harness)
        if removed:
            state.store.add_event(
                "icons", data={"what": "reset", "harness": harness, "by": w.name, "removed": removed}
            )
            icons_changed(state)
        return _ok({"removed": removed})

    @app.put("/api/me/preferences")
    async def set_preferences(request: Request) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            fields = {"theme", "text_size", "room_rules", "budget_per_hour", "hop_limit"}
            if not body or set(body) - fields:
                raise ServiceError("bad_request", "unknown preference")
            if "theme" in body and body["theme"] not in ("system", "light", "dark"):
                raise ServiceError("bad_request", "theme must be system, light or dark")
            if "text_size" in body and body["text_size"] not in ("small", "default", "large", "larger"):
                raise ServiceError("bad_request", "text size must be small, default, large or larger")
            if "room_rules" in body and (
                not isinstance(body["room_rules"], str) or len(body["room_rules"]) > 2000
            ):
                raise ServiceError("bad_request", "room rules must be at most 2000 characters")
            # null clears a default back to config.toml/the built-in one (#131); only a signed-in
            # session reaches this route, so raising your own default needs no further check.
            if (
                "budget_per_hour" in body
                and body["budget_per_hour"] is not None
                and (
                    isinstance(body["budget_per_hour"], bool)
                    or not isinstance(body["budget_per_hour"], int)
                    or not 0 <= body["budget_per_hour"] <= MAX_BUDGET
                )
            ):
                raise ServiceError(
                    "bad_request", f"budget_per_hour must be an integer from 0 to {MAX_BUDGET}, or null"
                )
            if (
                "hop_limit" in body
                and body["hop_limit"] is not None
                and (
                    isinstance(body["hop_limit"], bool)
                    or not isinstance(body["hop_limit"], int)
                    or not 0 <= body["hop_limit"] <= MAX_HOPS
                )
            ):
                raise ServiceError(
                    "bad_request", f"hop_limit must be an integer from 0 to {MAX_HOPS}, or null"
                )
            return _ok({"preferences": state.store.set_preferences(w.person_id, body)})
        except ServiceError as e:
            return _svc_err(e)

    @app.get("/api/rooms")
    async def list_rooms(request: Request) -> Response:
        if session(request) is None:
            return unauthorized()
        return _ok(
            {
                "rooms": state.service.rooms(viewer(request)),
                "closed": len(state.service.closed_room_dicts(viewer(request))),
            }
        )

    # The home page (§55, #626): what waits on this person across their rooms, what got done since
    # they last looked, and what is live. A read of what is stored, for the one person asking;
    # "seen" moves their own looked-at time forward and nothing else.
    @app.get("/api/home")
    async def home_page(request: Request) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        return _ok(home.build(state, w.name, w.person_id, w.is_admin))

    @app.post("/api/home/seen")
    async def home_seen(request: Request) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            at = body.get("at")
            if set(body) - {"at"} or (
                at is not None
                and (isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 <= at < 1e12)
            ):
                raise ServiceError("bad_request", "give at, a time, or nothing")
            return _ok({"seen": home.mark_seen(state, state.service.person_key(w.person_id), at)})
        except ServiceError as e:
            return _svc_err(e)

    # What a person has read of a room, and a board card they set aside (§55.8, #663). Both are
    # the signed-in person's own rows, for a room they can see: a private room they are not in is "no
    # such room", as everywhere else.
    @app.post("/api/rooms/{slug}/seen")
    async def room_seen(request: Request, slug: str) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            if set(body) != {"id"}:
                raise ServiceError("bad_request", "give id, a message id")
            room = state.service.room_for(slug, viewer(request))
            key = state.service.person_key(w.person_id)
            return _ok({"seen": home.mark_room_seen(state, key, room, body["id"])})
        except ServiceError as e:
            return _svc_err(e)

    @app.post("/api/rooms/{slug}/home-snooze")
    async def home_snooze(request: Request, slug: str) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            if set(body) != {"url"}:
                raise ServiceError("bad_request", "give url, a board's address")
            room = state.service.room_for(slug, viewer(request))
            key = state.service.person_key(w.person_id)
            return _ok({"snoozed_at": home.snooze_board(state, key, room, body["url"])})
        except ServiceError as e:
            return _svc_err(e)

    @app.post("/api/rooms")
    async def create_room(request: Request) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            name = body.get("name")
            if not isinstance(name, str):
                raise ServiceError("bad_request", "name is required")
            private = body.get("private", False)
            if not isinstance(private, bool):
                raise ServiceError("bad_request", "private must be true or false")
            if private and not state.hosted:
                # one person: a private room would be every room (§44.1)
                raise ServiceError("bad_request", "private rooms are for a hosted broker, with people")
            room = state.service.create_room(name, creator=w.name, person_id=w.person_id, private=private)
            return _ok({"room": state.service.room_dict(room)})
        except ServiceError as e:
            return _svc_err(e)

    @app.put("/api/rooms/{slug}/rules")
    async def set_room_rules(request: Request, slug: str) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            if set(body) != {"text"}:
                raise ServiceError("bad_request", "text is required")
            state.service.room_for(slug, viewer(request))
            room = state.service.set_room_rules(slug, body["text"], w.name)
            return _ok({"rules": room.rules_text})
        except ServiceError as e:
            return _svc_err(e)

    @app.put("/api/rooms/{slug}/name")
    async def rename_room(request: Request, slug: str) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            if set(body) != {"name"} or not isinstance(body["name"], str):
                raise ServiceError("bad_request", "name is required")
            room = state.service.room_for(slug, viewer(request))
            renamed = state.service.rename_room(room, body["name"], w.name)
            return _ok({"room": state.service.room_dict(renamed)})
        except ServiceError as e:
            return _svc_err(e)

    # The room's own budget per hour and hop limit (#131), from its settings dialog: the same
    # Origin and X-Switchboard checks as /rules above, never reachable through chat.
    @app.put("/api/rooms/{slug}/wake")
    async def set_room_wake(request: Request, slug: str) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            if set(body) != {"budget_per_hour", "hop_limit"}:
                raise ServiceError("bad_request", "budget_per_hour and hop_limit are required")
            budget, hops = body["budget_per_hour"], body["hop_limit"]
            if isinstance(budget, bool) or not isinstance(budget, int) or not 0 <= budget <= MAX_BUDGET:
                raise ServiceError(
                    "bad_request", f"budget_per_hour must be an integer from 0 to {MAX_BUDGET}"
                )
            if isinstance(hops, bool) or not isinstance(hops, int) or not 0 <= hops <= MAX_HOPS:
                raise ServiceError("bad_request", f"hop_limit must be an integer from 0 to {MAX_HOPS}")
            state.service.room_for(slug, viewer(request))
            room = state.service.set_room_wake(slug, budget_per_hour=budget, hop_limit=hops, actor=w.name)
            return _ok({"settings": state.service.settings(room)})
        except ServiceError as e:
            return _svc_err(e)

    # A room's review boards (#80, §37.5, §37.14): read by anyone signed in; a person's moves are
    # theirs, about the board whose `url` they send, posted in the room as their own messages.
    # Agents move them through `review`.
    @app.get("/api/rooms/{slug}/review")
    async def room_review(request: Request, slug: str) -> Response:
        if session(request) is None:
            return unauthorized()
        try:
            room = state.service.room_for(slug, viewer(request))
            return _ok({"boards": state.boards.boards(room)})
        except ServiceError as e:
            return _svc_err(e)

    @app.post("/api/rooms/{slug}/review")
    async def room_review_move(request: Request, slug: str) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            allowed = (
                {"action", "agent", "url"}
                if body.get("action") == "handoff"
                else {"action", "item", "option", "owner", "reason", "url"}
            )
            if set(body) - allowed:
                raise ServiceError("bad_request", "unknown fields")
            room = state.service.room_for(slug, viewer(request))
            try:
                return _ok(state.boards.person(room, w.name, w.person_id, body))
            except ReviewError as e:
                raise ServiceError("bad_request", str(e)) from None
        except ServiceError as e:
            return _svc_err(e)

    # Search (§58, #724): a page of hits for the signed-in person, from the rooms they can see now. A read
    # of what is stored; the query is made safe in switchboard.search before the index sees it.
    @app.get("/api/search")
    async def search_messages(request: Request) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        key = state.service.person_key(w.person_id)
        wait = state.search_limiter.wait_s(key)
        if wait > 0:
            return _err(429, "slow_down", f"too many searches: wait {int(wait) + 1} s and try again")
        qp = request.query_params
        try:
            return _ok(
                search_api.run(
                    state,
                    viewer(request),
                    q=qp.get("q", ""),
                    room=qp.get("room"),
                    sender=qp.get("from"),
                    since=qp.get("since"),
                    until=qp.get("until"),
                    calls=qp.get("calls"),
                    before=qp.get("before"),
                )
            )
        except ServiceError as e:
            return _svc_err(e)

    @app.get("/api/rooms/{slug}/messages")
    async def messages(request: Request, slug: str) -> Response:
        if session(request) is None:
            return unauthorized()
        try:
            after = _qint(request, "after")
            limit = _qint(request, "limit") or 200
            around = _qint(request, "around")
            room = state.service.room_for(slug, viewer(request))
            if around is not None:
                # the room opened at a search result (§58.5): the same visibility check, then a window
                return _ok({"messages": state.service.history_around(room, around), "around": around})
            return _ok({"messages": state.service.history(slug, after, min(limit, 1000))})
        except ServiceError as e:
            return _svc_err(e)

    @app.get("/api/rooms/{slug}/members")
    async def members(request: Request, slug: str) -> Response:
        if session(request) is None:
            return unauthorized()
        try:
            room = state.service.room_for(slug, viewer(request))
            w = who(request)
            return _ok(
                {
                    "room": room.name,
                    "human": w.name if w is not None else state.cfg.human_name,
                    "people": people.names(state) if state.hosted else [state.cfg.human_name],
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
            state.service.room_for(slug, viewer(request))
            return _ok(state.service.member_detail(slug, name))
        except ServiceError as e:
            return _svc_err(e)

    # The call's details (§54): the room's open call as the one person looking may see it, with whether they
    # may end it. A GET for the same reason as the Inspector's (and one more): the settings frame goes to
    # everyone in the room, and what this viewer may do can't ride in it. It writes nothing and publishes
    # nothing.
    @app.get("/api/rooms/{slug}/call")
    async def call_detail(request: Request, slug: str) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            room = state.service.room_for(slug, viewer(request))
            return _ok({"room": room.name, "call": state.service.call_detail(room, w.person_id, w.is_admin)})
        except ServiceError as e:
            return _svc_err(e)

    @app.post("/api/rooms/{slug}/say")
    async def say(request: Request, slug: str) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            text = body.get("text")
            if not isinstance(text, str):
                raise ServiceError("bad_request", "text is required")
            state.service.room_for(slug, viewer(request))
            msg = state.service.human_say(
                slug, text, via="web", person=(w.name, w.person_id), reply_to=body.get("reply_to")
            )
            return _ok({"id": msg.id})
        except ServiceError as e:
            return _svc_err(e)

    def backup_admin(request: Request, *, fresh: bool) -> tuple[people.Who | None, Response | None]:
        w = who(request)
        if w is None or w.must_reset:
            return None, unauthorized()
        if not w.is_admin:
            return None, _err(403, "forbidden", "only an admin backs up this broker")
        if fresh and not state.fresh_check(w.h):
            return None, _err(403, "reauth", "confirm it's you first")
        return w, None

    @app.get("/api/backups")
    async def backups_status(request: Request) -> Response:
        w, no = backup_admin(request, fresh=False)
        if no is not None:
            return no
        assert w is not None and state.backups is not None
        return _ok(state.backups.status(w))

    @app.post("/api/backups")
    async def backups_start(request: Request) -> Response:
        w, no = backup_admin(request, fresh=True)
        if no is not None:
            return no
        assert w is not None and state.backups is not None
        try:
            if await _json_body(request):
                return _err(400, "bad_request", "a backup takes no options")
            job = state.backups.start(w)
            return JSONResponse({"id": job.id, "state": job.state}, status_code=202, headers=NO_STORE)
        except ServiceError as e:
            return _svc_err(e)

    @app.get("/api/backups/{job_id}")
    async def backup_check(request: Request, job_id: str) -> Response:
        # A JSON preflight lets withFreshCheck confirm identity before a native browser
        # download, keeping the archive out of JavaScript memory. GET below checks again.
        _w, no = backup_admin(request, fresh=True)
        if no is not None:
            return no
        assert state.backups is not None
        try:
            state.backups.ready(job_id)
            return _ok({"ok": True})
        except ServiceError as e:
            return _svc_err(e)

    @app.get("/api/backups/{job_id}/download")
    async def backup_download(request: Request, job_id: str) -> Response:
        w, no = backup_admin(request, fresh=True)
        if no is not None:
            return no
        assert w is not None and state.backups is not None
        try:
            job = state.backups.ready(job_id)
            state.backups.downloaded(job, w)
            return FileResponse(
                state.backups.root / f"backup-{job.id}.tar",
                filename=f"switchboard-backup-{job.id}.tar",
                media_type="application/x-tar",
                headers={
                    **NO_STORE,
                    "X-Content-Type-Options": "nosniff",
                    "Cross-Origin-Resource-Policy": "same-origin",
                    "Content-Security-Policy": "default-src 'none'; sandbox",
                },
            )
        except ServiceError as e:
            return _svc_err(e)

    def _files_admin(request: Request) -> tuple[people.Who | None, JSONResponse | None]:
        """Retention is an admin's setting (the owner, on a desktop broker)."""
        w = who(request)
        if w is None or w.must_reset:
            return None, unauthorized()
        if not w.is_admin:
            return None, _err(403, "forbidden", "only an admin sets how long files are kept")
        return w, None

    @app.get("/api/settings/files")
    async def files_settings_get(request: Request) -> Response:
        _w, no = _files_admin(request)
        if no is not None:
            return no
        return _ok({"days": state.attachments.retention_days()})

    @app.put("/api/settings/files")
    async def files_settings_put(request: Request) -> Response:
        _w, no = _files_admin(request)
        if no is not None:
            return no
        try:
            body = await _json_body(request)
        except ServiceError as e:
            return _svc_err(e)
        if set(body) != {"days"}:
            return _err(400, "bad_request", "days is required")
        try:
            days = state.attachments.set_retention(body.get("days"))
        except ValueError as e:
            return _err(400, "bad_request", str(e))
        return _ok({"days": days})

    @app.post("/api/rooms/{slug}/files")
    async def room_file(request: Request, slug: str) -> Response:
        """A signed-in person uploads one file. The page then posts the artifact line for it."""
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            room = state.service.room_for(slug, viewer(request))
        except ServiceError as e:
            return _svc_err(e)
        att = state.attachments
        room_left = att.room_cap - state.store.room_stored_bytes(room.id)
        if room_left <= 0:
            return _err(413, "too_large", f"this room's files would pass {state.cfg.files.room_mb} MB")
        limit = min(att.max_bytes, room_left)
        declared = request.headers.get("content-length")
        if declared is not None and not declared.isdigit():
            return _err(400, "bad_request", "content-length must be a number")
        if declared is not None:
            why = att.length_refusal(int(declared), limit)
            if why is not None:
                return _err(413, "too_large", why[1])
        part = att.folder.part_path(new_part_id())
        try:
            got = await _spool_upload(request, part, limit)
        except Exception:
            part.unlink(missing_ok=True)
            raise
        if got is None:
            message = (
                f"file is larger than {state.cfg.files.max_mb} MB"
                if limit >= att.max_bytes
                else f"this room's files would pass {state.cfg.files.room_mb} MB"
            )
            return _err(413, "too_large", message)
        size, digest = got
        try:
            row = att.store_human(
                room.id,
                w.name,
                file_name(unquote(request.headers.get("x-switchboard-filename") or "file")),
                part,
                size,
                digest,
                request.headers.get("content-type") or "",
            )
        except Exception:
            part.unlink(missing_ok=True)
            raise
        log.info("file %s stored by %s in room %d", row.id, w.name, room.id)
        return _ok({"id": row.id, "name": row.name, "size": row.size, "sha256": row.sha256})

    @app.put("/api/upload/{token}")
    async def upload_put(request: Request, token: str) -> Response:
        """An agent's one-time upload. The token is the capability; it is not logged."""
        if not upload_path(request.url.path):
            return PlainTextResponse("no such upload\n", status_code=404, headers=NO_STORE)
        att = state.attachments
        begun = att.begin_upload(token)
        if begun[0] is None:
            status, message = begun[1]
            return PlainTextResponse(message + "\n", status_code=status, headers=NO_STORE)
        _row, limit = begun
        declared = request.headers.get("content-length")
        if declared is not None and not declared.isdigit():
            return PlainTextResponse("content-length must be a number\n", status_code=400, headers=NO_STORE)
        if declared is not None:
            why = att.length_refusal(int(declared), limit)
            if why is not None:
                return PlainTextResponse(why[1] + "\n", status_code=why[0], headers=NO_STORE)
        part = att.folder.part_path(new_part_id())  # its own, so two uploads on one link never share it
        got = await _spool_upload(request, part, limit)
        if got is None:
            message = (
                f"file is larger than {state.cfg.files.max_mb} MB"
                if limit >= att.max_bytes
                else f"this room's files would pass {state.cfg.files.room_mb} MB"
            )
            return PlainTextResponse(message + "\n", status_code=413, headers=NO_STORE)
        size, digest = got
        status, message = att.accept_upload(
            token, part, size, digest, request.headers.get("content-type") or ""
        )
        return PlainTextResponse(message + "\n", status_code=status, headers=NO_STORE)

    @app.get("/api/files/{file_id}")
    async def file_get(request: Request, file_id: str) -> Response:
        """A download. Always an attachment, never a page. A room the caller cannot read is a 404."""
        att = state.attachments
        token = request.query_params.get("t")
        can_read = False
        if token is None and FILE_ID_RE.fullmatch(file_id):
            w = who(request)
            row = state.store.file_get(file_id) if w is not None and not w.must_reset else None
            room = state.store.room_by_id(row.room_id) if row is not None else None
            can_read = room is not None and state.service.visible(room, viewer(request))
        got = att.for_download(file_id, token if token else None, can_read=can_read)
        if got is None:
            return _err(404, "not_found", "no such file")
        path = att.folder.path(got.id)
        if path is None:
            return _err(404, "not_found", "no such file")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": file_disposition(got.name),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/rooms/{slug}/command")
    async def command(request: Request, slug: str) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            text = body.get("text")
            if not isinstance(text, str):
                raise ServiceError("bad_request", "text is required")
            state.service.room_for(slug, viewer(request))
            res = state.service.command(
                slug,
                text,
                Actor(role="human", via="web", name=w.name, person_id=w.person_id, admin=w.is_admin),
            )
            return _ok(res)
        except ServiceError as e:
            return _svc_err(e)

    # A private room's people (§44.2): any of them adds or removes one, and anyone may leave.
    @app.post("/api/rooms/{slug}/people")
    async def room_people(request: Request, slug: str) -> Response:
        w = who(request)
        if w is None or w.must_reset:
            return unauthorized()
        try:
            body = await _json_body(request)
            action, name = body.get("action"), body.get("name")
            if (
                set(body) != {"action", "name"}
                or action not in ("add", "remove")
                or not isinstance(name, str)
            ):
                raise ServiceError("bad_request", 'action ("add" or "remove") and name are required')
            me = viewer(request)
            room = state.service.room_for(slug, me)
            if action == "add":
                text = state.service.add_room_person(room, name[:64], me)
            else:
                text = state.service.remove_room_person(room, name[:64], me)
            fresh = state.store.room_by_id(room.id)
            return _ok({"text": text, "room": state.service.room_dict(fresh) if fresh else None})
        except ServiceError as e:
            return _svc_err(e)

    # An admin sees every private room's name and people, never its contents (§44.4, §51).
    @app.get("/api/private-rooms")
    async def private_rooms(request: Request) -> Response:
        _w, no = admin(request, False)
        if no is not None:
            return no
        return _ok({"rooms": state.service.private_rooms()})

    # ------------------------------------------------------- closed rooms
    # Not under /api/rooms/{slug}: a room named #closed must not clash (§28).
    @app.get("/api/closed-rooms")
    async def closed_rooms(request: Request) -> Response:
        if session(request) is None:
            return unauthorized()
        return _ok({"rooms": state.service.closed_room_dicts(viewer(request))})

    @app.post("/api/closed-rooms/{rid}/reopen")
    async def reopen_room(request: Request, rid: str) -> Response:
        if session(request) is None:
            return unauthorized()
        try:
            await _json_body(request)
            if not ROOM_ID_RE.fullmatch(rid) or int(rid) > MAX_ROOM_ID:
                raise ServiceError("bad_request", "bad room id")
            closed = state.store.room_by_id(int(rid))
            if closed is not None and not state.service.visible(closed, viewer(request)):
                raise ServiceError("not_found", "no such closed room")
            room = state.service.reopen_room(int(rid), via="web")
            return _ok({"room": state.service.room_dict(room)})
        except ServiceError as e:
            return _svc_err(e)

    # ---------------------------------------------------------- websocket
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        origin_ok = ws.headers.get("origin") == state.origin
        w = people.who_of(state, state.sessions.check(ws.cookies.get(COOKIE_NAME))) if origin_ok else None
        if not origin_ok or w is None or w.must_reset:
            await ws.close(code=1008)
            return
        sid_hash = w.h
        await ws.accept()
        sub = WsSubscriber()
        sub.sid_hash = sid_hash
        sub.person = 0 if w.person_id is None else w.person_id  # whose page, for private rooms (§44)
        state.hub.add(sub)
        people_changed(state)  # this page learns who is here, and the others that it is

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
                # A board frame carries elements, so it may be larger than a hello. Anything else
                # over the small cap is closed without being parsed.
                board_frame = isinstance(text, str) and _looks_like_board(text)
                limit = board.MAX_BYTES if board_frame else WS_FRAME_MAX
                if isinstance(text, str) and len(text) <= limit:
                    try:
                        frame = json.loads(text)
                    except ValueError:
                        frame = None
                t = frame.get("t") if isinstance(frame, dict) else None
                if t == "ping":
                    sub.offer({"t": "pong"})
                elif t == "hello" and isinstance(frame, dict):
                    if not _ws_hello(state, sub, frame):
                        break
                elif t == "board" and isinstance(frame, dict):
                    if not _ws_board(state, sub, frame):
                        break
                else:
                    break  # anything else closes it
        except WebSocketDisconnect:
            pass
        finally:
            state.hub.remove(sub)  # queues the close sentinel
            people_changed(state)
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


def people_frame(state: "BrokerState") -> dict[str, Any] | None:
    """Everyone on a hosted broker, and which of them has a page open (§44.8, #354): what the
    Members pane lists under People. ``full_names`` is each person's real name, so a page that
    is already open shows someone added or renamed without a reload (#563). ``admins`` is the
    name of everyone who is an admin, the owner first (§51), so an open page can see that its
    own person was made one or had it taken away. It never includes an email. None on a desktop
    broker, which has one person. The names are no news to a signed-in page (a room's members
    answer has them, for @ completion); who is connected, and who is an admin, is not a secret
    among signed-in people, and every one of them gets it, whatever rooms they can see."""
    if not state.hosted:
        return None
    here = state.hub.online_persons()
    online = [state.cfg.human_name] if 0 in here else []
    online += [p.name for p in state.store.people() if p.id in here]
    admins = [state.cfg.human_name] + [p.name for p in state.store.people() if p.admin]
    return {
        "people": people.names(state),
        "online": online,
        "full_names": people.full_names(state),
        "admins": admins,
    }


def icons_changed(state: "BrokerState") -> None:
    """Tell every open page an agent's avatar was set or reset: it fetches the stylesheet again."""
    state.hub.publish("icons", None, {})


def people_changed(state: "BrokerState") -> None:
    """Tell every open page: someone connected or left, or was added, removed or renamed."""
    frame = people_frame(state)
    if frame is not None:
        state.hub.publish("people", None, frame)


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
        # a private room the page's person isn't in is no room at all (§44): never subscribed
        if room is None or not state.service.visible(room, sub.person if sub.person is not None else -1):
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
            sub.offer(
                {
                    "t": "notice",
                    "room": name,
                    "level": "info",
                    "text": f"{skipped} earlier message(s) not shown here; `switchboard tail` has them all",
                }
            )
        for msg in state.service.message_frames(backlog):
            sub.offer({"t": "msg", "room": name, "msg": msg})
        sub.offer({"t": "room", "room": name, "settings": state.service.settings(room)})
        sub.offer(
            {
                "t": "members",
                "room": name,
                "members": state.service.members(name),
            }
        )
        sub.offer({"t": "board", "room": name, **state.room_board.snapshot(room)})
    return True


def _looks_like_board(text: str) -> bool:
    """True when a frame is large only because it is a board update. The check is the first
    bytes, so a hello the size of a board is not parsed."""
    head = text.lstrip()[:16]
    return head.startswith('{"t":"board"') or head.startswith('{"t": "board"')


def _ws_board(state: "BrokerState", sub: WsSubscriber, frame: dict[str, Any]) -> bool:
    """One batch of elements from a signed-in page. False closes the socket: a room the person
    cannot see, or a frame that is not a list of elements. A bad element is dropped by the merge;
    it does not close the socket."""
    raw_room = frame.get("room")
    elements = frame.get("elements")
    if not isinstance(raw_room, str):
        return False
    try:
        name = normalize_room(raw_room)
    except InvalidName:
        return False
    room = state.store.get_room(name)
    person = sub.person if sub.person is not None else -1
    if room is None or not state.service.visible(room, person):
        return False
    if "request" in frame:
        if set(frame) != {"t", "room", "request", "result"}:
            return False
        # A late or malformed picture is dropped, like a rejected element, without disconnecting a page.
        state.room_board.image_result(room, frame["request"], frame["result"])
        return True
    if not isinstance(elements, list) or len(elements) > board.MAX_ELEMENTS:
        return False
    who = state.service.person_name(person) or "someone"
    state.room_board.from_page(room, who, elements)
    return True
