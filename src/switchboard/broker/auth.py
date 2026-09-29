"""Web auth (DESIGN.md §5.4): one-time login links, cookie sessions, Host/Origin guard.

- Login tokens are 32 random bytes, live 300 s, work once, and exist only in memory.
- Sessions are stored as sha256(sid); they slide to 12 h on every request.
- ``HostOriginGuard`` is pure ASGI so it covers WebSocket scopes too.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from switchboard.clock import Clock, SystemClock
from switchboard.store import Store

COOKIE_NAME = "switchboard_session"
SESSION_TTL_S = 12 * 3600
LOGIN_TTL_S = 300
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
UI_HOST = "switchboard.localhost"
# paths a platform's health checker may GET without the UI's Host (it probes the container's own
# address): they answer "ok" and nothing else
OPEN_PATHS = frozenset({"/healthz"})
_DNS_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")

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


@dataclass(frozen=True)
class WebOrigin:
    """Where browsers reach the web UI (DESIGN.md §30): the ``Host`` they send, the ``Origin``
    of their requests, and whether it is https (then the session cookie is ``Secure`` and the
    WebSocket is ``wss``). By default it is ``http://switchboard.localhost:<port>``; with
    ``--public-url`` it is that URL, served behind a proxy that terminates TLS."""

    scheme: str
    host: str  # as browsers send it in Host: the name, and the port when it isn't the default

    @classmethod
    def local(cls, port: int) -> WebOrigin:
        return cls("http", f"{UI_HOST}:{port}")

    @classmethod
    def parse(cls, url: str) -> WebOrigin:
        """A public URL, which must be an origin only: ``https://<name>[:<port>]``. Plain http
        is accepted only for a local test host (localhost, ``*.localhost``, ``*.test``, 127.0.0.1),
        never for a name other machines use: the session cookie and sign-in links would cross
        the network in the clear."""
        try:
            u = urllib.parse.urlsplit(url.strip())
            port = u.port
        except ValueError as e:
            raise ValueError(f"not a URL: {e}") from None
        if u.scheme not in ("http", "https"):
            raise ValueError("it must start with https:// (or http:// for a local test host)")
        if u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
            raise ValueError("it is an origin only: the scheme, the host and a port if needed, no path")
        name = (u.hostname or "").lower()
        if not name or not (_DNS_NAME_RE.fullmatch(name) or _is_ipv4(name)):
            raise ValueError("its host must be a DNS name or an IPv4 address")
        if port == 0:
            raise ValueError("port 0 is not an address browsers can use")
        if u.scheme == "http" and not _local_test_host(name):
            raise ValueError("plain http:// is only for a local test host; use https:// behind a proxy that "
                             "terminates TLS")
        default = 443 if u.scheme == "https" else 80
        return cls(u.scheme, name if port in (None, default) else f"{name}:{port}")

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}"

    @property
    def ws(self) -> str:
        return f"{'wss' if self.scheme == 'https' else 'ws'}://{self.host}"

    @property
    def secure(self) -> bool:
        return self.scheme == "https"


def _is_ipv4(name: str) -> bool:
    try:
        ipaddress.IPv4Address(name)
    except ValueError:
        return False
    return True


def _local_test_host(name: str) -> bool:
    return name in ("localhost", "127.0.0.1") or name.endswith((".localhost", ".test"))


def csp(origin: WebOrigin) -> str:
    return (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        f"connect-src 'self' {origin.ws}; img-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )


class SecurityHeaders:
    """Adds CSP, nosniff, no-referrer (and a few friends) to every HTTP response."""

    def __init__(self, app: ASGIApp, origin: WebOrigin):
        self.app = app
        self.headers = [
            (b"content-security-policy", csp(origin).encode()),
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

    - ``Host`` must be exactly the UI's (``switchboard.localhost:<port>``, or the public URL's
      host), else 421 (http) or a handshake refused with close code 1008, which the server
      sends as 403 (websocket). The one exception is a GET or HEAD of ``OPEN_PATHS``
      (``/healthz``), which a platform's health checker sends to the container's own address.
    - Unsafe http methods also need ``Origin`` to be the UI's origin and ``X-Switchboard: 1``,
      else 403. (The session cookie is checked by routes.)
    """

    def __init__(self, app: ASGIApp, origin: WebOrigin):
        self.app = app
        self.host = origin.host
        self.origin = origin.origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = _header(scope, b"host")
        health = (kind == "http" and scope.get("path") in OPEN_PATHS
                  and scope.get("method", "GET").upper() in ("GET", "HEAD"))
        if host != self.host and not health:
            if kind == "http":
                await _plain(send, 421, f"open {self.origin}/\n")
            else:
                await _refuse_ws(scope, send, 421, f"open {self.origin}/\n")  # -> 403
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
