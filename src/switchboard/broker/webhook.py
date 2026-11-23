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
to that checked address, not to the name, so the name cannot change between the check and the connect; one
deadline of ``TIMEOUT_S`` for the whole send (resolve, connect, TLS, request, response), every step given only
the time left; a redirect is a failure, never followed; the answer is read to a small cap and thrown away.

A send counts once against the hourly cap (``Notifier._send``), so it makes at most one POST: the next address
a name resolves to is tried only while nothing of the request has gone out (connect or TLS failed). Once the
request may have reached the receiver, whatever happens to the response ends the send (#927).
"""

from __future__ import annotations

import asyncio
import http.client
import io
import json
import logging
import socket
import ssl
import time
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
monotonic: Callable[[], float] = time.monotonic  # what a send's deadline is measured on (a test's fake)


class SendError(Exception):
    """A send that failed; ``code`` is a key of ``notify.fail_reason``. Once the request may have
    gone out (the answer failed), the receiver may or may not have the POST: a failed send may have
    arrived."""

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


class _Deadline:
    """The time left of one send's deadline. ``left`` raises ``TimeoutError`` once it is spent, so a step
    never starts with no time, and is never given more than what remains (#927)."""

    def __init__(self) -> None:
        self._end = monotonic() + TIMEOUT_S

    def left(self) -> float:
        t = self._end - monotonic()
        if t <= 0:
            raise TimeoutError
        return t


class _DeadlineSocket:
    """The connection's socket as ``http.client`` uses it (``sendall``, ``makefile``, ``close``), with its
    timeout set to the time left before every send and every read. A socket timeout alone is per operation,
    so a receiver that answers a byte at a time would hold a send for many of them. Like a socket, it closes
    once it is closed and no file made from it is still open: ``http.client`` closes the connection before
    the body of an answer that ends the connection is read."""

    def __init__(self, sock: ssl.SSLSocket, deadline: _Deadline) -> None:
        self._sock = sock
        self._deadline = deadline
        self._files = 0
        self._closed = False

    def _arm(self) -> None:
        self._sock.settimeout(self._deadline.left())

    def sendall(self, data: Any) -> None:
        self._arm()
        self._sock.sendall(data)

    def recv_into(self, buf: Any, nbytes: int = 0) -> int:
        self._arm()
        return self._sock.recv_into(buf, nbytes)

    def makefile(self, mode: str = "rb", *args: Any, **kwargs: Any) -> Any:
        self._files += 1
        return io.BufferedReader(socket.SocketIO(self, "rb"))  # type: ignore[arg-type]

    def _decref_socketios(self) -> None:  # ``SocketIO.close`` calls it
        self._files = max(0, self._files - 1)
        if self._closed and self._files == 0:
            self._sock.close()

    def close(self) -> None:
        self._closed = True
        if self._files == 0:
            self._sock.close()


class _Pinned(http.client.HTTPSConnection):
    """An HTTPS connection that goes to a given, already checked address, and verifies the certificate for
    the name in the URL, within the send's deadline."""

    def __init__(self, host: str, port: int, ip: str, context: ssl.SSLContext, deadline: _Deadline) -> None:
        super().__init__(host, port, timeout=deadline.left(), context=context)
        self._pinned_ip = ip
        self._deadline = deadline

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self._deadline.left())
        try:
            sock.settimeout(self._deadline.left())  # the handshake has what is left after the connect
            tls = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]
        except BaseException:
            sock.close()
            raise
        self.sock = _DeadlineSocket(tls, self._deadline)


def _code_of(e: BaseException) -> str:
    if isinstance(e, ssl.SSLError):
        return "tls"
    if isinstance(e, TimeoutError):
        return "timeout"
    return "connect"


def post_blocking(
    url: str,
    body: bytes,
    *,
    allow_private: bool,
    resolve: Resolver = system_resolve,
    context: ssl.SSLContext | None = None,
) -> None:
    """POST ``body`` to ``url`` or raise ``SendError``, all within ``TIMEOUT_S`` of the call. Blocking: run it
    in a thread. The resolver's time counts too; it cannot be cut short (``getaddrinfo`` has no timeout), so
    what it leaves is all the connections get, and nothing is sent once it has used the deadline up."""
    deadline = _Deadline()
    host, port, ips = checked_addresses(url, allow_private, resolve)
    u = urllib.parse.urlsplit(url)
    target = (u.path or "/") + (f"?{u.query}" if u.query else "")
    ctx = context or ssl.create_default_context()
    last = "connect"
    for ip in ips:
        try:
            conn = _Pinned(host, port, ip, ctx, deadline)
        except TimeoutError:
            raise SendError("timeout") from None
        try:
            try:
                conn.connect()
            except (OSError, http.client.HTTPException) as e:
                last = _code_of(e)  # nothing of the request went out: the next address may, in the time left
                continue
            # From here the POST may reach the receiver, so this is the send's only POST (#927).
            try:
                conn.request(
                    "POST",
                    target,
                    body=body,
                    headers={"Content-Type": "application/json", "User-Agent": "switchboard-webhook"},
                )
                resp = conn.getresponse()
                try:
                    resp.read(READ_MAX)
                    status = resp.status
                finally:
                    resp.close()
            except (OSError, http.client.HTTPException) as e:
                raise SendError(_code_of(e)) from None
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
HOST_MAX = 253

# Why a stored webhook is not used (``check``), as ``public`` names it to the page: ``zone``, quiet hours in a
# zone this server cannot read (its tzdata lost the name since the save); ``address`` and ``second_address``,
# an address the current rules refuse; ``settings``, anything else a save would not have kept.
PROBLEMS = ("zone", "address", "second_address", "settings")


def check(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """A stored ``webhooks`` row read through the same checks as a save: ``(webhook, None)``, or ``(None,
    problem)`` with a code of ``PROBLEMS`` when any part of it does not pass. The webhook is ``{"id",
    "first_gen", "second_gen", "url", "wait_s", "second": {"url", "after_s"} | None, "quiet": notify.Quiet |
    None}``."""
    try:
        url, wait = row["url"], float(row["wait_s"])
        if notify.url_problem(url) is not None:
            return None, "address"
        if not notify.WAIT_MIN_S <= wait <= notify.WAIT_MAX_S:
            return None, "settings"
        second = None
        if row["second_url"] is not None:
            s_url, after = row["second_url"], float(row["second_after_s"])
            if notify.url_problem(s_url) is not None:
                return None, "second_address"
            if not notify.SECOND_MIN_S <= after <= notify.SECOND_MAX_S:
                return None, "settings"
            second = {"url": s_url, "after_s": after}
        quiet = None
        if row["quiet_from"] is not None:
            start, end = notify.parse_hhmm(row["quiet_from"]), notify.parse_hhmm(row["quiet_to"])
            tz, always = row["quiet_tz"], [k for k in str(row["quiet_always"]).split(",") if k]
            if start is None or end is None:
                return None, "settings"
            if notify.zone_of(tz) is None:
                return None, "zone"
            if notify.quiet_problem(start, end, tz, always) is not None:
                return None, "settings"
            quiet = notify.Quiet(start, end, tz, frozenset(always))
        gens = int(row["id"]), int(row["first_gen"]), int(row["second_gen"])
    except (ValueError, KeyError, TypeError):
        return None, "settings"
    return {
        "id": gens[0],
        "first_gen": gens[1],
        "second_gen": gens[2],
        "url": url,
        "wait_s": wait,
        "second": second,
        "quiet": quiet,
    }, None


def load(state: Any, person_key: int) -> dict[str, Any] | None:
    """The person's webhook as ``check`` gives it, or None. Their ``webhooks`` row (schema 60, §62.4): a row
    that does not pass the checks, the second address's and the quiet hours' included, is no webhook (it
    fails closed). So is quiet hours whose zone this server can no longer read: nothing is sent then, not
    even the kinds marked as always sent. ``public`` still shows such a row to its owner, to fix or remove
    (#923, F12)."""
    row = state.store.webhook_get(person_key)
    return None if row is None else check(row)[0]


def save(state: Any, person_key: int, changes: dict[str, Any]) -> str:
    """Keep only the fields in ``changes`` (``Store.webhook_set``): ``"ok"``, ``"none"`` (there is no webhook
    to change, and no address to make one), ``"no_second"`` or ``"gone"`` (the person was removed meanwhile).
    A new address bumps its lane's generation, which is how the notifier forgets the old one's tries."""
    return str(state.store.webhook_set(person_key, changes, second_default_s=notify.SECOND_DEFAULT_S))


def remove(state: Any, person_key: int) -> None:
    state.store.webhook_remove(person_key)


def last_status(state: Any, person_key: int, second: bool = False) -> dict[str, Any] | None:
    row = state.store.webhook_get(person_key)
    p = "second" if second else "first"
    if row is None or row[f"{p}_at"] is None:
        return None
    ok = bool(row[f"{p}_ok"])
    return {"at": float(row[f"{p}_at"]), "ok": ok, "error": None if ok else row[f"{p}_error"]}


def _host(url: object) -> str | None:
    """The host of ``url`` when it parses as one, else None."""
    if not isinstance(url, str):
        return None
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None
    return host[:HOST_MAX] if host else None


def _minutes(raw: object, lo: float, hi: float, default: float) -> int:
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        v = default
    return round((v if lo <= v <= hi else default) / 60)


NO_QUIET = {
    "set": False,
    "from": "22:00",
    "to": "07:00",
    "tz": None,
    "always": list(notify.QUIET_ALWAYS_DEFAULT),
}


def _unusable(state: Any, person_key: int, row: dict[str, Any], problem: str) -> dict[str, Any]:
    """What ``public`` says of a stored row that fails the checks: that it is there, why it is not used, and
    as much of it as can be read, so the person can fix or remove it (#923, F12). The page puts these in
    ``textContent`` only."""
    has_second = row.get("second_url") is not None
    quiet = dict(NO_QUIET)
    if row.get("quiet_from") is not None:
        quiet = {
            "set": True,
            "from": str(row["quiet_from"]),
            "to": str(row["quiet_to"]),
            "tz": str(row["quiet_tz"])[: notify.TZ_MAX],
            "always": sorted(k for k in str(row["quiet_always"]).split(",") if k in notify.KINDS),
        }
    return {
        "set": True,
        "usable": False,
        "problem": problem,
        "host": _host(row.get("url")),
        "wait_min": _minutes(row.get("wait_s"), notify.WAIT_MIN_S, notify.WAIT_MAX_S, notify.WAIT_DEFAULT_S),
        "last": last_status(state, person_key),
        "second": {
            "set": has_second,
            "host": _host(row.get("second_url")) if has_second else None,
            "after_min": _minutes(
                row.get("second_after_s"), notify.SECOND_MIN_S, notify.SECOND_MAX_S, notify.SECOND_DEFAULT_S
            ),
            "last": last_status(state, person_key, True) if has_second else None,
        },
        "quiet": quiet,
    }


def public(state: Any, person_key: int) -> dict[str, Any]:
    """What ``/api/me`` says of a person's webhook: whether there is one, whether it is used (``usable``,
    and a ``problem`` code of ``PROBLEMS`` when not), its host (never the path or query, which is where the
    secret is), the wait, how the last post went, and the same of the second address, and the quiet hours."""
    no_second = {"set": False, "after_min": round(notify.SECOND_DEFAULT_S / 60), "last": None}
    row = state.store.webhook_get(person_key)
    if row is None:
        return {
            "set": False,
            "usable": False,
            "problem": None,
            "wait_min": round(notify.WAIT_DEFAULT_S / 60),
            "last": None,
            "second": no_second,
            "quiet": dict(NO_QUIET),
        }
    w, problem = check(row)
    if w is None:
        return _unusable(state, person_key, row, problem or "settings")
    s, q = w["second"], w["quiet"]
    return {
        "set": True,
        "usable": True,
        "problem": None,
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
        "quiet": dict(NO_QUIET)
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
    """Ticks every ``TICK_S``: for each person with a webhook, what needs them, and the policy's answer.

    A policy belongs to one webhook (its ``id``): one removed and made again starts with a new one. What a
    post writes when it is back (its status, its sent keys, its tries) names the webhook and the generation
    of the address it went to, so a post that was out while the address was replaced or the webhook remade
    lands nowhere anyone reads (#923, F4, F10)."""

    def __init__(
        self, state: "BrokerState", resolve: Resolver | None = None, context: ssl.SSLContext | None = None
    ):
        self.state = state
        self.resolve = resolve or system_resolve
        self.context = context
        self._policy: dict[int, notify.Policy] = {}  # by webhook id
        self._limit: dict[int, notify.Limiter] = {}  # by person: the hourly cap outlives a remade webhook

    def _policy_of(self, w: dict[str, Any]) -> notify.Policy:
        """The policy of the webhook ``w`` (as ``load`` read it), told its lanes' address generations."""
        wid = int(w["id"])
        if wid not in self._policy:
            store = self.state.store
            self._policy[wid] = notify.Policy(
                store.webhook_sent_keys(wid, "first"), store.webhook_sent_keys(wid, "second")
            )
        pol = self._policy[wid]
        pol.see(w["first_gen"], w["second_gen"])
        return pol

    def _limiter(self, person_key: int) -> notify.Limiter:
        return self._limit.setdefault(person_key, notify.Limiter())

    def opened(self, person_key: int, slug: str) -> None:
        """The person opened the room ``slug``: what needed them there before now is got."""
        if (w := load(self.state, person_key)) is not None:
            self._policy_of(w).ack_room(slug, self.state.clock.now())

    async def post(
        self, person_key: int, w: dict[str, Any], body: dict[str, Any], second: bool = False
    ) -> str | None:
        """Send ``body`` now to the webhook ``w``, as the caller read it (``second``: to its second address),
        never to what is current by then. None when it went, else a failure code. Both addresses count toward
        the one hourly cap, and how it went is recorded where the person's settings show it, only while
        ``w`` is still the webhook with that address (``_send``)."""
        if second and w["second"] is None:
            return "error"
        return await self._send(person_key, w, body, second)

    def unchanged(self, person_key: int, w: dict[str, Any]) -> bool:
        """Whether the person's webhook is still ``w``: the same id (not removed and made again) and the same
        address generations (neither address replaced). Send a test checks it between its posts and before
        it answers, so a test never reaches an address it did not read (#923, F15)."""
        row = self.state.store.webhook_get(person_key)
        return row is not None and (int(row["id"]), int(row["first_gen"]), int(row["second_gen"])) == (
            int(w["id"]),
            int(w["first_gen"]),
            int(w["second_gen"]),
        )

    async def _send(
        self, person_key: int, w: dict[str, Any], body: dict[str, Any], second: bool
    ) -> str | None:
        """POST ``body`` to a lane of the webhook ``w`` as it was read, and keep its status only while that
        webhook's lane still has that address (``Store.webhook_status_set``)."""
        now = self.state.clock.now()
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
        lane = "second" if second else "first"
        error = None if code is None else notify.fail_reason(code)
        self.state.store.webhook_status_set(w["id"], lane, w[f"{lane}_gen"], now, code is None, error)
        return code

    async def tick(self) -> None:
        for person_key in self.state.store.webhook_people():
            try:
                await self._one(person_key)
            except Exception:
                log.warning("a webhook tick failed for person %s", person_key)
        live = self.state.store.webhook_ids()
        for wid in [k for k in self._policy if k not in live]:
            del self._policy[wid]  # a removed webhook's policy, and whatever a late post left in it

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

    def _after_post(self, person_key: int, sent_w: dict[str, Any], pol: notify.Policy) -> None:
        """A post of ``sent_w`` is back: tell its policy the generations as they are now, before the result is
        recorded, so a result for an address replaced meanwhile is kept as that address's, which nobody reads
        (``Policy.sent``)."""
        row = self.state.store.webhook_get(person_key)
        if row is not None and int(row["id"]) == int(sent_w["id"]):
            pol.see(int(row["first_gen"]), int(row["second_gen"]))

    async def _one(self, person_key: int) -> None:
        st = self.state
        got = self._now_of(person_key)
        if got is None:
            return
        w, needs, now = got
        pol = self._policy_of(w)
        due = pol.due(needs, now, w["wait_s"], w["quiet"])
        lim = self._limiter(person_key)
        if due and lim.allow(now):
            code = await self._send(person_key, w, notify.payload(due, st.web_origin.origin), False)
            self._after_post(person_key, w, pol)
            pol.sent(due, now, code is None, w["first_gen"])
            self._keep(w["id"], pol, now)
            # The first post took time, and in it the person may have answered, opened the room, lost the
            # room, saved quiet hours or removed the address (#923, F1): the second alerts are chosen from
            # what is true once it is back, never from before it.
            got = self._now_of(person_key)
            if got is None:
                return
            w, needs, now = got
            pol = self._policy_of(w)
            pol.due(needs, now, w["wait_s"], w["quiet"])  # drops what was got; a new first goes next tick
        if w["second"] is not None and lim.allow(now):
            second = pol.due_second(needs, now, w["second"]["after_s"], w["quiet"])
            if second:
                code = await self._send(
                    person_key, w, notify.payload(second, st.web_origin.origin, True), True
                )
                self._after_post(person_key, w, pol)
                pol.sent_second(second, now, code is None, w["second_gen"])
        self._keep(w["id"], pol, now)

    def _keep(self, wid: int, pol: notify.Policy, now: float) -> None:
        """Keep what was sent on each lane across a restart (``webhook_sent``), pruned to what still needs
        the person, as the policy keeps it; written only while the webhook and that lane's address are the
        ones the policy last saw."""
        store = self.state.store
        for lane, keys in (("first", pol.sent_keys()), ("second", pol.sent_second_keys())):
            if keys != store.webhook_sent_keys(wid, lane):
                store.webhook_keep_sent(wid, lane, pol.gen(lane), keys, now)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            await self.tick()
