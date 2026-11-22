"""A person's webhook: the edge of reaching them when they are needed (DESIGN.md §62, #723).

``notify.py`` is the policy and is pure. This is everything with I/O: the person's own settings (one
``broker_settings`` row each, never a room message), working out what needs them (``home.needs``), and the
one outbound POST, which is the only request the broker makes on a person's behalf.

A person may also give a second address for a louder second alert, and quiet hours (#923, §62.4). Each is a
row of its own beside the first (``webhook_second:``, ``webhook_quiet:``), since a settings value is at most
200 characters; the second address has exactly the first one's checks and is the same kind of secret.

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


def status_key(person_key: int, second: bool = False) -> str:
    return f"webhook_status2:{person_key}" if second else f"webhook_status:{person_key}"


def sent_key(person_key: int, second: bool = False) -> str:
    return f"webhook_sent2:{person_key}" if second else f"webhook_sent:{person_key}"


def second_key(person_key: int) -> str:
    return f"webhook_second:{person_key}"


def quiet_key(person_key: int) -> str:
    return f"webhook_quiet:{person_key}"


VALUE_MAX = 200  # broker_settings.value's CHECK (db.py)


def load(state: Any, person_key: int) -> dict[str, Any] | None:
    """The person's webhook, ``{"url": str, "wait_s": float, "second": {"url", "after_s"} | None, "quiet":
    notify.Quiet | None}``, or None. A row that does not parse or does not pass the checks, the second
    address's and the quiet hours' included, is no webhook (it fails closed). A webhook of before #923, with
    neither of those rows, is one with no second alert and no quiet hours."""
    store = state.store
    raw = store.setting_get(cfg_key(person_key))
    if raw is None:
        return None
    try:
        d = json.loads(raw)
        url, wait = d["url"], float(d["wait_s"])
        second = _second_of(store.setting_get(second_key(person_key)))
        quiet = _quiet_of(store.setting_get(quiet_key(person_key)))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if notify.url_problem(url) is not None or not notify.WAIT_MIN_S <= wait <= notify.WAIT_MAX_S:
        return None
    return {"url": url, "wait_s": wait, "second": second, "quiet": quiet}


def _second_of(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    d = json.loads(raw)
    url, after = d["url"], float(d["after_s"])
    if notify.url_problem(url) is not None or not notify.SECOND_MIN_S <= after <= notify.SECOND_MAX_S:
        raise ValueError("second")
    return {"url": url, "after_s": after}


def _quiet_of(raw: str | None) -> notify.Quiet | None:
    if raw is None:
        return None
    d = json.loads(raw)
    start, end, tz, always = d["start"], d["end"], d["tz"], d["always"]
    if notify.quiet_problem(start, end, tz, always) is not None:
        raise ValueError("quiet")
    return notify.Quiet(start, end, tz, frozenset(always))


def _second_row(second: dict[str, Any]) -> str:
    return json.dumps({"url": second["url"], "after_s": second["after_s"]})


def _quiet_row(q: notify.Quiet) -> str:
    return json.dumps({"start": q.start, "end": q.end, "tz": q.tz, "always": sorted(q.always)})


def too_long(second: dict[str, Any] | None) -> str | None:
    """Why the second address cannot be kept, or None: a settings value holds at most ``VALUE_MAX``
    characters, the address and its delay included."""
    if second is not None and len(_second_row(second)) > VALUE_MAX:
        return "That address is too long to keep. Use a shorter one."
    return None


def save(
    state: Any,
    person_key: int,
    url: str,
    wait_s: float,
    second: dict[str, Any] | None = None,
    quiet: notify.Quiet | None = None,
) -> None:
    """Keep the person's webhook. ``second`` and ``quiet`` None remove those rows. Check ``too_long`` first."""
    store = state.store
    store.setting_set(cfg_key(person_key), json.dumps({"url": url, "wait_s": wait_s}))
    if second is None:
        store.setting_delete(second_key(person_key))
    else:
        store.setting_set(second_key(person_key), _second_row(second))
    if quiet is None:
        store.setting_delete(quiet_key(person_key))
    else:
        store.setting_set(quiet_key(person_key), _quiet_row(quiet))
    for k in (status_key(person_key), status_key(person_key, True)):
        store.setting_set(k, "")  # an old failure is not about these addresses


def remove(state: Any, person_key: int) -> None:
    for key in (
        cfg_key(person_key),
        second_key(person_key),
        quiet_key(person_key),
        status_key(person_key),
        status_key(person_key, True),
        sent_key(person_key),
        sent_key(person_key, True),
    ):
        state.store.setting_delete(key)
    n = getattr(state, "notifier", None)
    if n is not None:
        n.forget(person_key)


def last_status(state: Any, person_key: int, second: bool = False) -> dict[str, Any] | None:
    raw = state.store.setting_get(status_key(person_key, second))
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return {"at": float(d["at"]), "ok": bool(d["ok"]), "error": str(d["error"]) if d["error"] else None}
    except (ValueError, KeyError, TypeError):
        return None


def _record(state: Any, person_key: int, now: float, code: str | None, second: bool = False) -> None:
    state.store.setting_set(
        status_key(person_key, second),
        json.dumps(
            {"at": now, "ok": code is None, "error": None if code is None else notify.fail_reason(code)}
        ),
    )


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

    def _kept(self, person_key: int, second: bool) -> list[str]:
        try:
            raw = self.state.store.setting_get(sent_key(person_key, second)) or "[]"
            return [str(k) for k in json.loads(raw)]
        except (ValueError, TypeError):
            return []

    def _policy_of(self, person_key: int) -> notify.Policy:
        if person_key not in self._policy:
            self._policy[person_key] = notify.Policy(
                self._kept(person_key, False), self._kept(person_key, True)
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
        _record(self.state, person_key, now, code, second)
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
        quiet = w["quiet"]
        due = pol.due(needs, now, w["wait_s"], quiet)
        second = [] if w["second"] is None else pol.due_second(needs, now, w["second"]["after_s"], quiet)
        lim = self._limiter(person_key)
        if due and lim.allow(now):
            code = await self.post(person_key, notify.payload(due, st.web_origin.origin))
            pol.sent(due, now, code is None)
        if second and lim.allow(now):
            code = await self.post(person_key, notify.payload(second, st.web_origin.origin, True), True)
            pol.sent_second(second, now, code is None)
        self._keep(person_key, pol)

    def _keep(self, person_key: int, pol: notify.Policy) -> None:
        for second, keys in ((False, pol.sent_keys()), (True, pol.sent_second_keys())):
            raw, row = json.dumps(keys), sent_key(person_key, second)
            if st_get := self.state.store.setting_get(row):
                if st_get == raw:
                    continue
            elif raw == "[]":
                continue
            self.state.store.setting_set(row, raw)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            await self.tick()
