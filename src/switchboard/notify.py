"""Reaching a person with a webhook when they are needed and have not seen it (DESIGN.md §62, #723).

Pure policy, no I/O and no clock of its own: the time is an argument, so every rule is tested with a
``FakeClock``. ``broker/webhook.py`` is the edge that reads what needs a person, sends the POST and keeps
the person's settings; this module decides *when* to send and when to stop, what a POST says, and which
addresses a webhook may never point at.

The rules, in short:

- An item **needs** a person (a question to them, a review board waiting for their call, an agent of theirs
  waiting on an approval). It is sent once, after it has been waiting ``wait_s``, to that person's own URL.
- **Got it** stops it, from anywhere: the item no longer needs them (they answered, decided, or the prompt
  cleared), or they opened its room after it began (``ack_room``).
- A failed send is tried again a few times, slowly, then given up on. A person never gets more than
  ``MAX_PER_HOUR`` posts an hour. Anything this module does not understand (a need list it could not read,
  a wait outside its bounds) sends nothing.
- A POST says who and where, with a link, and never the message's text (``payload``).
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

WAIT_DEFAULT_S = 300.0  # five minutes (#723)
WAIT_MIN_S = 60.0
WAIT_MAX_S = 7200.0
MAX_PER_HOUR = 12  # posts a person's webhook gets in an hour, a test included
RETRY_S = 300.0  # a failed send is tried again this long after the last try
MAX_ATTEMPTS = 3
MAX_ITEMS = 5  # needs named in one POST
URL_MAX = 500
HOUR_S = 3600.0

KINDS = ("question", "board", "approval")


@dataclass(frozen=True)
class Need:
    """One thing that needs a person. ``key`` is stable while it is the same item (a newer question in the
    room is another item), ``at`` is when it began needing them, and ``who`` is a screen name."""

    key: str
    kind: str
    room: str
    slug: str
    who: str
    at: float


@dataclass
class _Track:
    first_seen: float
    done: bool = False
    attempts: int = 0
    last_try: float = 0.0


class Policy:
    """When one person's webhook fires. State is the items it has seen, which of them it already sent
    (``sent``, kept by the caller across restarts), and the rooms the person opened and when."""

    def __init__(self, sent: Iterable[str] = ()) -> None:
        self._tracks: dict[str, _Track] = {k: _Track(first_seen=0.0, done=True) for k in sent}
        self._opened: dict[str, float] = {}

    def ack_room(self, slug: str, at: float) -> None:
        """The person opened this room at ``at``: whatever needed them in it since before then is got."""
        if at > self._opened.get(slug, float("-inf")):
            self._opened[slug] = at

    def sent_keys(self) -> list[str]:
        return sorted(k for k, t in self._tracks.items() if t.done)

    def due(self, needs: list[Need] | None, now: float, wait_s: float) -> list[Need]:
        """What to send now, oldest first, at most ``MAX_ITEMS``. ``needs`` None (not worked out) or a
        wait outside its bounds sends nothing and forgets nothing. An item that is no longer a need is
        dropped: it was got, and if it comes back it is new."""
        if needs is None or not WAIT_MIN_S <= wait_s <= WAIT_MAX_S:
            return []
        live = {n.key for n in needs}
        for key in [k for k in self._tracks if k not in live]:
            del self._tracks[key]
        out: list[Need] = []
        for n in sorted(needs, key=lambda n: (n.at, n.key)):
            t = self._tracks.get(n.key)
            if t is None:
                t = self._tracks[n.key] = _Track(first_seen=now)
            if t.done or now - t.first_seen < wait_s:
                continue
            if n.at <= self._opened.get(n.slug, float("-inf")):
                continue  # got it: they opened the room after this began
            if t.attempts >= MAX_ATTEMPTS or (t.attempts and now - t.last_try < RETRY_S):
                continue
            out.append(n)
        return out[:MAX_ITEMS]

    def sent(self, items: Iterable[Need], now: float, ok: bool) -> None:
        """Record a try: done when it went, else a counted failure to try again later."""
        for n in items:
            t = self._tracks.get(n.key)
            if t is None:
                continue
            t.attempts += 1
            t.last_try = now
            if ok:
                t.done = True


class Limiter:
    """At most ``MAX_PER_HOUR`` posts in any hour."""

    def __init__(self) -> None:
        self._at: deque[float] = deque()

    def _trim(self, now: float) -> None:
        while self._at and now - self._at[0] >= HOUR_S:
            self._at.popleft()

    def allow(self, now: float) -> bool:
        self._trim(now)
        return len(self._at) < MAX_PER_HOUR

    def record(self, now: float) -> None:
        self._trim(now)
        self._at.append(now)


# ------------------------------------------------------------------ what a POST says
def _line(n: Need) -> str:
    who = n.who or "Someone"
    if n.kind == "approval":
        return f"{who} is waiting for your approval in {n.room}"
    if n.kind == "board":
        return f"A review needs your call in {n.room}"
    return f"{who} has a message for you in {n.room}"


def link_for(base: str, slug: str) -> str:
    """Where tapping lands: the room, as the web page's own address for it (``/#slug``)."""
    return f"{base.rstrip('/')}/#{urllib.parse.quote(slug, safe='')}"


def payload(items: list[Need], base: str) -> dict[str, object]:
    """The JSON that is posted: who and where, and a link, and never what a message said. ``text`` is what
    a Slack incoming webhook posts and ``content`` what Discord does; ntfy takes the whole body."""
    items = items[:MAX_ITEMS]
    lines = [f"{_line(n)}: {link_for(base, n.slug)}" for n in items]
    text = "switchboard: " + (
        "\n".join(lines) if len(lines) == 1 else f"{len(lines)} things need you\n" + "\n".join(lines)
    )
    return {
        "text": text,
        "content": text,
        "items": [
            {"kind": n.kind, "room": n.room, "who": n.who, "url": link_for(base, n.slug)} for n in items
        ],
    }


def test_payload(base: str) -> dict[str, object]:
    """What "Send a test" posts: the same shape, saying it is a test."""
    text = f"switchboard: this is a test, your webhook works. {base.rstrip('/')}/"
    return {"text": text, "content": text, "items": []}


test_payload.__test__ = False  # type: ignore[attr-defined]  # a payload builder, not a pytest test


# ------------------------------------------------------------------ where a webhook may point
_HOST_OK = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def url_problem(raw: object) -> str | None:
    """Why ``raw`` cannot be a webhook address, or None. Syntax only: https, a host, no sign-in in the
    address, no spaces or control characters. Where the host points is checked when it is resolved
    (``address_problem``), at save and again at every send."""
    if not isinstance(raw, str) or not raw:
        return "Give the address to post to."
    if len(raw) > URL_MAX:
        return f"That address is longer than {URL_MAX} characters."
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw):
        return "An address has no spaces or control characters."
    try:
        u = urllib.parse.urlsplit(raw)
        port = u.port
    except ValueError:
        return "That is not an address."
    if u.scheme.lower() != "https":
        return "The address must start with https://."
    if u.username is not None or u.password is not None or "@" in u.netloc:
        return "An address with a name and password in it is not accepted."
    host = u.hostname or ""
    if not host or not (_HOST_OK.match(host) or _ip_of(host) is not None):
        return "That address has no valid host."
    if port is not None and not 1 <= port <= 65535:
        return "That port is not valid."
    return None


def _ip_of(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


# cloud metadata services that are not link-local, so the general rules would let some through: never allowed
_METADATA = {
    ipaddress.ip_address(x)
    for x in (
        "169.254.169.254",
        "169.254.170.2",  # ECS task credentials
        "168.63.129.16",  # Azure's wireserver, a public address
        "192.0.0.192",  # Oracle Cloud
        "100.100.100.200",  # Alibaba
        "fd00:ec2::254",  # AWS, IPv6
    )
}


def address_problem(ip: str, allow_private: bool) -> str | None:
    """Why the broker may not connect to ``ip``, or None. A webhook runs from the broker, so by default it
    may reach only public addresses: no loopback, private, link-local (the cloud metadata address is one),
    carrier-grade NAT, multicast, reserved or unspecified, and no IPv6 form that wraps one of those. An
    operator who turns on private addresses (a home network) gets private and loopback, never link-local,
    unspecified, multicast or reserved. An address that cannot be read is refused."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "That address does not resolve to an IP address."
    if isinstance(a, ipaddress.IPv6Address):
        if a.ipv4_mapped is not None:
            return address_problem(str(a.ipv4_mapped), allow_private)
        if a.sixtofour is not None:
            return address_problem(str(a.sixtofour), allow_private)
        if any(a in net for net in _NAT64):
            return "That address points inside a network the broker may not reach."
        if a.teredo is not None:
            return "That address points inside a network the broker may not reach."
    if a in _METADATA:
        return "That address points inside a network the broker may not reach."
    if allow_private and a.is_loopback:
        return None  # ::1 sits in a range Python calls reserved
    if a.is_unspecified or a.is_multicast or a.is_link_local or a.is_reserved:
        return "That address points inside a network the broker may not reach."
    if a.is_global:
        return None
    if allow_private and (a.is_private or a.is_loopback):
        return None
    return (
        "That address is private. The broker only posts to public addresses unless its operator allows more."
    )


def fail_reason(code: str) -> str:
    """The fixed sentence for a failure code. A reason is never built from an exception's text, which can
    carry the address."""
    return _REASONS.get(code, _REASONS["error"])


_REASONS = {
    "resolve": "The address could not be found.",
    "blocked": "The address points somewhere the broker may not post to.",
    "timeout": "The webhook did not answer in time.",
    "tls": "The webhook's certificate was not trusted.",
    "connect": "Could not connect to the webhook.",
    "redirect": "The webhook answered with a redirect, which is not followed.",
    "status": "The webhook answered with an error.",
    "limit": "Too many posts this hour. It will try again later.",
    "error": "The post failed.",
}
