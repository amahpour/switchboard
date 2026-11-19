"""A person's webhook: the edge of reaching them when they are needed (DESIGN.md §62, #723).

``notify.py`` is the policy and is pure. This is everything with I/O: the person's own settings (one
``broker_settings`` row each, never a room message), working out what needs them (``home.needs``), and the
one outbound POST, which is the only request the broker makes on a person's behalf.

The address is a secret. It is never returned in full (``public`` gives the host), never logged, never in a
room, and a failure is kept as a fixed sentence, never an exception's text, which can carry the address.

The POST is made as safely as a request from a server can be: https only; the host is resolved at every
send and every address it resolves to must be allowed (``notify.address_problem``), then the connection goes
to that checked address, not to the name, so the name cannot change between the check and the connect; a
short timeout; a redirect is a failure, never followed; the answer is read to a small cap and thrown away.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import socket
import ssl
import urllib.parse
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from switchboard import notify
from switchboard.broker import home

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState

log = logging.getLogger("switchboard.webhook")

TICK_S = 10.0
TIMEOUT_S = 5.0
READ_MAX = 1024

Resolver = Callable[[str, int], list[str]]


class SendError(Exception):
    """A send that did not happen; ``code`` is a key of ``notify.fail_reason``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def system_resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to (blocking: call it off the event loop)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    seen: list[str] = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in seen:
            seen.append(ip)
    return seen


def checked_addresses(url: str, allow_private: bool, resolve: Resolver) -> tuple[str, int, list[str]]:
    """The host, port and addresses to use for ``url``, or a ``SendError``. All of the addresses must be
    allowed: a name that resolves to one public and one internal address is refused."""
    if notify.url_problem(url) is not None:
        raise SendError("blocked")
    u = urllib.parse.urlsplit(url)
    host = u.hostname or ""
    port = u.port or 443
    ips = resolve(host, port)
    if not ips:
        raise SendError("resolve")
    if any(notify.address_problem(ip, allow_private) is not None for ip in ips):
        raise SendError("blocked")
    return host, port, ips


class _Pinned(http.client.HTTPSConnection):
    """An HTTPS connection that goes to a given, already checked address, and verifies the certificate for
    the name in the URL."""

    def __init__(self, host: str, port: int, ip: str, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=TIMEOUT_S, context=context)
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), TIMEOUT_S)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


def post_blocking(
    url: str,
    body: bytes,
    *,
    allow_private: bool,
    resolve: Resolver = system_resolve,
    context: ssl.SSLContext | None = None,
) -> None:
    """POST ``body`` to ``url`` or raise ``SendError``. Blocking: run it in a thread."""
    host, port, ips = checked_addresses(url, allow_private, resolve)
    u = urllib.parse.urlsplit(url)
    target = (u.path or "/") + (f"?{u.query}" if u.query else "")
    ctx = context or ssl.create_default_context()
    last = "connect"
    for ip in ips:
        conn = _Pinned(host, port, ip, ctx)
        try:
            conn.request(
                "POST",
                target,
                body=body,
                headers={"Content-Type": "application/json", "User-Agent": "switchboard-webhook"},
            )
            resp = conn.getresponse()
            resp.read(READ_MAX)
            status = resp.status
        except ssl.SSLError:
            last = "tls"
            continue
        except TimeoutError:
            last = "timeout"
            continue
        except (OSError, http.client.HTTPException):
            last = "connect"
            continue
        finally:
            conn.close()
        if 300 <= status < 400:
            raise SendError("redirect")
        if not 200 <= status < 300:
            raise SendError("status")
        return
    raise SendError(last)


# ---------------------------------------------------------------- a person's settings
def cfg_key(person_key: int) -> str:
    return f"webhook:{person_key}"


def status_key(person_key: int) -> str:
    return f"webhook_status:{person_key}"


def sent_key(person_key: int) -> str:
    return f"webhook_sent:{person_key}"


def load(state: Any, person_key: int) -> dict[str, Any] | None:
    """The person's webhook, ``{"url": str, "wait_s": float}``, or None. A row that does not parse or does
    not pass the checks is no webhook (it fails closed)."""
    raw = state.store.setting_get(cfg_key(person_key))
    if raw is None:
        return None
    try:
        d = json.loads(raw)
        url, wait = d["url"], float(d["wait_s"])
    except (ValueError, KeyError, TypeError):
        return None
    if notify.url_problem(url) is not None or not notify.WAIT_MIN_S <= wait <= notify.WAIT_MAX_S:
        return None
    return {"url": url, "wait_s": wait}


def save(state: Any, person_key: int, url: str, wait_s: float) -> None:
    state.store.setting_set(cfg_key(person_key), json.dumps({"url": url, "wait_s": wait_s}))
    state.store.setting_set(status_key(person_key), "")  # an old failure is not about this address


def remove(state: Any, person_key: int) -> None:
    for key in (cfg_key(person_key), status_key(person_key), sent_key(person_key)):
        state.store.setting_delete(key)
    n = getattr(state, "notifier", None)
    if n is not None:
        n.forget(person_key)


def last_status(state: Any, person_key: int) -> dict[str, Any] | None:
    raw = state.store.setting_get(status_key(person_key))
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return {"at": float(d["at"]), "ok": bool(d["ok"]), "error": str(d["error"]) if d["error"] else None}
    except (ValueError, KeyError, TypeError):
        return None


def _record(state: Any, person_key: int, now: float, code: str | None) -> None:
    state.store.setting_set(
        status_key(person_key),
        json.dumps(
            {"at": now, "ok": code is None, "error": None if code is None else notify.fail_reason(code)}
        ),
    )


def public(state: Any, person_key: int) -> dict[str, Any]:
    """What ``/api/me`` says of a person's webhook: whether there is one, its host (never the path or query,
    which is where the secret is), the wait, and how the last post went."""
    w = load(state, person_key)
    if w is None:
        return {"set": False, "wait_min": round(notify.WAIT_DEFAULT_S / 60), "last": None}
    host = urllib.parse.urlsplit(w["url"]).hostname or ""
    return {
        "set": True,
        "host": host,
        "wait_min": round(w["wait_s"] / 60),
        "last": last_status(state, person_key),
    }


async def check_for_save(state: Any, url: str, resolve: Resolver | None = None) -> str | None:
    """Why ``url`` cannot be saved, or None: its syntax, then where it points now (it is checked again at
    every send, since a name can come to point somewhere else)."""
    if (why := notify.url_problem(url)) is not None:
        return why
    if resolve is None:
        n = getattr(state, "notifier", None)
        resolve = n.resolve if n is not None else system_resolve
    try:
        await asyncio.to_thread(checked_addresses, url, state.cfg.notify.allow_private_webhooks, resolve)
    except SendError as e:
        return notify.fail_reason(e.code)
    return None


# ---------------------------------------------------------------- the notifier
class Notifier:
    """Ticks every ``TICK_S``: for each person with a webhook, what needs them, and the policy's answer."""

    def __init__(
        self, state: "BrokerState", resolve: Resolver | None = None, context: ssl.SSLContext | None = None
    ):
        self.state = state
        self.resolve = resolve or system_resolve
        self.context = context
        self._policy: dict[int, notify.Policy] = {}
        self._limit: dict[int, notify.Limiter] = {}

    def forget(self, person_key: int) -> None:
        self._policy.pop(person_key, None)

    def _policy_of(self, person_key: int) -> notify.Policy:
        if person_key not in self._policy:
            kept: list[str] = []
            try:
                kept = [
                    str(k) for k in json.loads(self.state.store.setting_get(sent_key(person_key)) or "[]")
                ]
            except (ValueError, TypeError):
                kept = []
            self._policy[person_key] = notify.Policy(kept)
        return self._policy[person_key]

    def _limiter(self, person_key: int) -> notify.Limiter:
        return self._limit.setdefault(person_key, notify.Limiter())

    def opened(self, person_key: int, slug: str) -> None:
        """The person opened the room ``slug``: what needed them there before now is got."""
        if load(self.state, person_key) is not None:
            self._policy_of(person_key).ack_room(slug, self.state.clock.now())

    async def post(self, person_key: int, body: dict[str, Any]) -> str | None:
        """Send ``body`` to the person's webhook now. None when it went, else a failure code. Counts toward
        the hourly cap, and records how it went where the person's settings show it."""
        now = self.state.clock.now()
        w = load(self.state, person_key)
        if w is None:
            return "error"
        lim = self._limiter(person_key)
        if not lim.allow(now):
            return "limit"
        lim.record(now)
        code: str | None = None
        try:
            await asyncio.to_thread(
                post_blocking,
                w["url"],
                json.dumps(body).encode(),
                allow_private=self.state.cfg.notify.allow_private_webhooks,
                resolve=self.resolve,
                context=self.context,
            )
        except SendError as e:
            code = e.code
        except Exception:
            log.warning("a webhook post failed unexpectedly")  # nothing of the address
            code = "error"
        _record(self.state, person_key, now, code)
        return code

    async def tick(self) -> None:
        st = self.state
        for key, _ in st.store.settings_with_prefix("webhook:"):
            try:
                person_key = int(key.split(":", 1)[1])
            except ValueError:
                continue
            try:
                await self._one(person_key)
            except Exception:
                log.warning("a webhook tick failed for person %s", person_key)

    async def _one(self, person_key: int) -> None:
        st = self.state
        w = load(st, person_key)
        name = st.service.person_name(person_key)
        if w is None or name is None:
            return
        now = st.clock.now()
        pol = self._policy_of(person_key)
        try:
            needs: list[notify.Need] | None = home.needs(st, name, None if person_key == 0 else person_key)
        except Exception:
            needs = None  # could not tell what needs them: send nothing
        due = pol.due(needs, now, w["wait_s"])
        if not due:
            self._keep(person_key, pol)
            return
        if not self._limiter(person_key).allow(now):
            return
        code = await self.post(person_key, notify.payload(due, st.web_origin.origin))
        pol.sent(due, now, code is None)
        self._keep(person_key, pol)

    def _keep(self, person_key: int, pol: notify.Policy) -> None:
        raw = json.dumps(pol.sent_keys())
        if st_get := self.state.store.setting_get(sent_key(person_key)):
            if st_get == raw:
                return
        elif raw == "[]":
            return
        self.state.store.setting_set(sent_key(person_key), raw)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            await self.tick()
