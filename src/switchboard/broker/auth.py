"""Web auth (DESIGN.md §5.4): one-time login links, cookie sessions, Host/Origin guard.

- Login tokens are 32 random bytes, live 300 s, work once, and exist only in memory.
- Sessions are stored as sha256(sid); they slide to 12 h on every request.
- ``HostOriginGuard`` is pure ASGI so it covers WebSocket scopes too.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from switchboard.clock import Clock, SystemClock
from switchboard.store import Store

COOKIE_NAME = "switchboard_session"
SESSION_TTL_S = 12 * 3600
LOGIN_TTL_S = 300
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
UI_HOST = "switchboard.localhost"

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class LoginTokens:
    """One-time login tokens, in memory only."""

    def __init__(self, clock: Clock | None = None, ttl_s: float = LOGIN_TTL_S):
        self.clock = clock or SystemClock()
        self.ttl_s = ttl_s
        self._tokens: dict[str, float] = {}  # sha256(token) -> expiry

    def mint(self) -> str:
        self.purge()
        tok = secrets.token_urlsafe(32)
        self._tokens[sha256_hex(tok)] = self.clock.now() + self.ttl_s
        return tok

    def consume(self, token: str | None) -> bool:
        if not token or len(token) > 256:
            return False
        exp = self._tokens.pop(sha256_hex(token), None)
        return exp is not None and exp > self.clock.now()

    def purge(self) -> None:
        now = self.clock.now()
        for k in [k for k, exp in self._tokens.items() if exp <= now]:
            del self._tokens[k]

    def __len__(self) -> int:
        return len(self._tokens)


class Sessions:
    """Web sessions persisted as sha256(sid) with a sliding 12 h expiry."""

    def __init__(self, store: Store, ttl_s: float = SESSION_TTL_S):
        self.store = store
        self.ttl_s = ttl_s

    def create(self) -> str:
        sid = secrets.token_urlsafe(32)
        self.store.web_session_create(sha256_hex(sid), self.ttl_s)
        return sid

    def check(self, sid: str | None) -> str | None:
        """Return the session's id hash if valid (and slide it), else None."""
        if not sid or len(sid) > 256:
            return None
        h = sha256_hex(sid)
        return h if self.store.web_session_touch(h, self.ttl_s) else None

    def revoke(self, sid: str | None) -> int:
        if not sid:
            return 0
        return self.store.web_session_delete(sha256_hex(sid))

    def revoke_all(self) -> int:
        return self.store.web_session_delete_all()


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers") or ():
        if k.lower() == name:
            return v.decode("latin-1")
    return None


def csp(port: int) -> str:
    return (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        f"connect-src 'self' ws://{UI_HOST}:{port}; img-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )


class SecurityHeaders:
    """Adds CSP, nosniff, no-referrer (and a few friends) to every HTTP response."""

    def __init__(self, app: ASGIApp, port: int):
        self.app = app
        self.headers = [
            (b"content-security-policy", csp(port).encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (b"x-frame-options", b"DENY"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"cross-origin-resource-policy", b"same-origin"),
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in self.headers if k not in existing]
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class HostOriginGuard:
    """Pure ASGI guard for http and websocket scopes.

    - ``Host`` must be exactly ``switchboard.localhost:<port>``, else 421 (http) or
      a handshake refused with close code 1008, which the server sends as 403
      (websocket).
    - Unsafe http methods also need ``Origin == http://switchboard.localhost:<port>``
      and ``X-Switchboard: 1``, else 403. (The session cookie is checked by routes.)
    """

    def __init__(self, app: ASGIApp, port: int):
        self.app = app
        self.port = port
        self.host = f"{UI_HOST}:{port}"
        self.origin = f"http://{UI_HOST}:{port}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = _header(scope, b"host")
        if host != self.host:
            if kind == "http":
                await _plain(send, 421, f"open http://{self.host}/\n")
            else:
                await _refuse_ws(scope, send, 421, f"open http://{self.host}/\n")  # -> 403
            return
        if kind == "http" and scope.get("method", "GET").upper() in UNSAFE_METHODS:
            if _header(scope, b"origin") != self.origin or _header(scope, b"x-switchboard") != "1":
                await _json(send, 403, {"error": "forbidden", "message": "bad origin"})
                return
        await self.app(scope, receive, send)


async def _plain(send: Send, status: int, text: str) -> None:
    body = text.encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _json(send: Send, status: int, data: dict[str, Any]) -> None:
    body = json.dumps(data).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _refuse_ws(scope: Scope, send: Send, status: int, text: str) -> None:
    """Refuse a WebSocket handshake by closing with 1008 before accept (HTTP 403).

    Not the ``websocket.http.response`` denial extension: uvicorn 0.53's sansio
    WebSocket implementation logs a spurious ERROR after sending one.
    """
    await send({"type": "websocket.close", "code": 1008})
