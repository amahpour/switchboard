"""A person's webhook: the edge of reaching them when they are needed (DESIGN.md §62, #723).

``notify.py`` is the policy and is pure. This is everything with I/O: the person's own settings (their row
of ``webhooks``, schema 60, never a room message), working out what needs them (``home.needs``), and the
one outbound POST, which is the only request the broker makes on a person's behalf.

A person may also give a second address for a louder second alert, and quiet hours (#923, §62.4): columns of
the same row, saved field by field, never by rewriting the rest. The second address has exactly the first
one's checks and is the same kind of secret. What was sent on each lane is ``webhook_sent``, so a restart
does not send it again.

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
LANES = ("first", "second")


def load(state: Any, person_key: int) -> dict[str, Any] | None:
    """The person's webhook, ``{"url": str, "wait_s": float, "second": {"url", "after_s"} | None, "quiet":
    notify.Quiet | None}``, or None. Their ``webhooks`` row (schema 60, §62.4), read through the same checks
    as a save: a row that does not pass them, the second address's and the quiet hours' included, is no
    webhook (it fails closed). So is quiet hours whose zone this server can no longer read (its tzdata lost
    the name since the save): nothing is sent then, not even the kinds marked as always sent."""
    row = state.store.webhook_get(person_key)
    if row is None:
        return None
    try:
        url, wait = row["url"], float(row["wait_s"])
        if notify.url_problem(url) is not None or not notify.WAIT_MIN_S <= wait <= notify.WAIT_MAX_S:
            return None
        second = None
        if row["second_url"] is not None:
            s_url, after = row["second_url"], float(row["second_after_s"])
            if (
                notify.url_problem(s_url) is not None
                or not notify.SECOND_MIN_S <= after <= notify.SECOND_MAX_S
            ):
                return None
            second = {"url": s_url, "after_s": after}
        quiet = None
        if row["quiet_from"] is not None:
            start, end = notify.parse_hhmm(row["quiet_from"]), notify.parse_hhmm(row["quiet_to"])
            tz, always = row["quiet_tz"], [k for k in str(row["quiet_always"]).split(",") if k]
            if start is None or end is None or notify.quiet_problem(start, end, tz, always) is not None:
                return None
            quiet = notify.Quiet(start, end, tz, frozenset(always))
    except (ValueError, KeyError, TypeError):
        return None
    return {"url": url, "wait_s": wait, "second": second, "quiet": quiet}


def save(state: Any, person_key: int, changes: dict[str, Any]) -> str:
    """Keep only the fields in ``changes`` (``Store.webhook_set``): ``"ok"``, ``"none"`` (there is no webhook
    to change, and no address to make one) or ``"no_second"``. A new address forgets what the notifier knew
    of the old one: the first address everything (``Notifier.forget``), the second its failed tries
    (``Notifier.new_second``), keeping what was sent and what the person got."""
    out = state.store.webhook_set(person_key, changes, second_default_s=notify.SECOND_DEFAULT_S)
    n = getattr(state, "notifier", None)
    if out == "ok" and n is not None:
        if "url" in changes:
            n.forget(person_key)
        elif changes.get("second_url") is not None:
            n.new_second(person_key)
    return out


def remove(state: Any, person_key: int) -> None:
    state.store.webhook_remove(person_key)
    n = getattr(state, "notifier", None)
    if n is not None:
        n.forget(person_key)


def last_status(state: Any, person_key: int, second: bool = False) -> dict[str, Any] | None:
    row = state.store.webhook_get(person_key)
    p = "second" if second else "first"
    if row is None or row[f"{p}_at"] is None:
        return None
    ok = bool(row[f"{p}_ok"])
    return {"at": float(row[f"{p}_at"]), "ok": ok, "error": None if ok else row[f"{p}_error"]}


def _record(
    state: Any, person_key: int, url: str, now: float, code: str | None, second: bool = False
) -> None:
    error = None if code is None else notify.fail_reason(code)
    state.store.webhook_status_set(person_key, "second" if second else "first", url, now, code is None, error)


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or ""


def public(state: Any, person_key: int) -> dict[str, Any]:
    """What ``/api/me`` says of a person's webhook: whether there is one, its host (never the path or query,
    which is where the secret is), the wait, how the last post went, and the same of the second address,
    and the quiet hours."""
    w = load(state, person_key)
    no_second = {"set": False, "after_min": round(notify.SECOND_DEFAULT_S / 60), "last": None}
    no_quiet = {
        "set": False,
        "from": "22:00",
        "to": "07:00",
        "tz": None,
        "always": list(notify.QUIET_ALWAYS_DEFAULT),
    }
    if w is None:
        return {
            "set": False,
            "wait_min": round(notify.WAIT_DEFAULT_S / 60),
            "last": None,
            "second": no_second,
            "quiet": no_quiet,
        }
    s, q = w["second"], w["quiet"]
    return {
        "set": True,
        "host": _host(w["url"]),
        "wait_min": round(w["wait_s"] / 60),
        "last": last_status(state, person_key),
        "second": no_second
        if s is None
        else {
            "set": True,
            "host": _host(s["url"]),
            "after_min": round(s["after_s"] / 60),
            "last": last_status(state, person_key, True),
        },
        "quiet": no_quiet
        if q is None
        else {
            "set": True,
            "from": notify.hhmm(q.start),
            "to": notify.hhmm(q.end),
            "tz": q.tz,
            "always": sorted(q.always),
        },
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

    def new_second(self, person_key: int) -> None:
        """The second address was replaced: its failed tries were the old address's (#923, F4)."""
        if (pol := self._policy.get(person_key)) is not None:
            pol.new_second()

    def _policy_of(self, person_key: int) -> notify.Policy:
        if person_key not in self._policy:
            store = self.state.store
            self._policy[person_key] = notify.Policy(
                store.webhook_sent_keys(person_key, "first"), store.webhook_sent_keys(person_key, "second")
            )
        return self._policy[person_key]

    def _limiter(self, person_key: int) -> notify.Limiter:
        return self._limit.setdefault(person_key, notify.Limiter())

    def opened(self, person_key: int, slug: str) -> None:
        """The person opened the room ``slug``: what needed them there before now is got."""
        if load(self.state, person_key) is not None:
            self._policy_of(person_key).ack_room(slug, self.state.clock.now())

    async def post(self, person_key: int, body: dict[str, Any], second: bool = False) -> str | None:
        """Send ``body`` to the person's webhook now (``second``: to their second address). None when it
        went, else a failure code. Both addresses count toward the one hourly cap, and how it went is
        recorded where the person's settings show it."""
        now = self.state.clock.now()
        w = load(self.state, person_key)
        if w is None or (second and w["second"] is None):
            return "error"
        url = w["second"]["url"] if second else w["url"]
        lim = self._limiter(person_key)
        if not lim.allow(now):
            return "limit"
        lim.record(now)
        code: str | None = None
        try:
            await asyncio.to_thread(
                post_blocking,
                url,
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
        _record(self.state, person_key, url, now, code, second)
        return code

    async def tick(self) -> None:
        for person_key in self.state.store.webhook_people():
            try:
                await self._one(person_key)
            except Exception:
                log.warning("a webhook tick failed for person %s", person_key)

    def _now_of(self, person_key: int) -> tuple[dict[str, Any], list[notify.Need] | None, float] | None:
        """The person's webhook, what needs them and the time, read now, or None when they have no webhook
        (or are gone)."""
        st = self.state
        w = load(st, person_key)
        name = st.service.person_name(person_key)
        if w is None or name is None:
            return None
        try:
            needs: list[notify.Need] | None = home.needs(st, name, None if person_key == 0 else person_key)
        except Exception:
            needs = None  # could not tell what needs them: send nothing
        return w, needs, st.clock.now()

    async def _one(self, person_key: int) -> None:
        st = self.state
        got = self._now_of(person_key)
        if got is None:
            return
        w, needs, now = got
        pol = self._policy_of(person_key)
        due = pol.due(needs, now, w["wait_s"], w["quiet"])
        lim = self._limiter(person_key)
        if due and lim.allow(now):
            code = await self.post(person_key, notify.payload(due, st.web_origin.origin))
            pol.sent(due, now, code is None)
            self._keep(person_key, pol, now)
            # The first post took time, and in it the person may have answered, opened the room, lost the
            # room, saved quiet hours or removed the address (#923, F1): the second alerts are chosen from
            # what is true once it is back, never from before it.
            got = self._now_of(person_key)
            if got is None:
                return
            w, needs, now = got
            pol = self._policy_of(person_key)
            pol.due(needs, now, w["wait_s"], w["quiet"])  # drops what was got; a new first goes next tick
        if w["second"] is not None and lim.allow(now):
            second = pol.due_second(needs, now, w["second"]["after_s"], w["quiet"])
            if second:
                code = await self.post(person_key, notify.payload(second, st.web_origin.origin, True), True)
                pol.sent_second(second, now, code is None)
        self._keep(person_key, pol, now)

    def _keep(self, person_key: int, pol: notify.Policy, now: float) -> None:
        """Keep what was sent on each lane across a restart (``webhook_sent``), pruned to what still needs
        the person, as the policy keeps it."""
        store = self.state.store
        for lane, keys in (("first", pol.sent_keys()), ("second", pol.sent_second_keys())):
            if keys != store.webhook_sent_keys(person_key, lane):
                store.webhook_keep_sent(person_key, lane, keys, now)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            await self.tick()
