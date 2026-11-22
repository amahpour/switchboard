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
- A **second alert** (#923, §62.4), when the person set a second address: an item that still needs them
  ``after_s`` after its first alert was due is posted once more, there, whether or not the first post went
  through. It is stopped by the same "got it", and both addresses share the hourly cap.
- **Quiet hours** (``Quiet``): from a start to an end in the person's own zone (an IANA name, read with
  ``zoneinfo``, so a daylight-saving change is the zone's), nothing is sent, first or second alert, but the
  kinds they marked as always sent. A held item goes when they end, if it still needs them, and the second
  alert's clock starts then. A zone this server cannot read is always quiet here (``in_quiet``), and a
  stored one makes the whole webhook none for sending (``webhook.load``), so nothing is sent at all; its
  owner still sees it, unusable and why, to fix or remove (``webhook.public``, #923, F12).
- A failed try is kept by lane and by the generation of that lane's address (``Policy.see``): a new address
  starts with every try, and a result for the old one, back after the change, counts for nothing (F4).
"""

from __future__ import annotations

import datetime
import ipaddress
import re
import urllib.parse
import zoneinfo
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

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

SECOND_DEFAULT_S = 1200.0  # twenty minutes after the first alert was due (#923)
SECOND_MIN_S = 300.0
SECOND_MAX_S = 14400.0
DAY_MIN = 1440
TZ_MAX = 64
QUIET_ALWAYS_DEFAULT = ("approval",)  # a stuck agent costs the most; a question and a board can wait


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


@dataclass(frozen=True)
class Quiet:
    """Quiet hours: ``start`` and ``end`` are minutes after local midnight in the zone ``tz`` (``end`` before
    ``start`` spans midnight), and ``always`` the kinds sent anyway. Made through ``quiet_problem`` first."""

    start: int
    end: int
    tz: str
    always: frozenset[str] = field(default_factory=lambda: frozenset(QUIET_ALWAYS_DEFAULT))


def zone_of(name: object) -> zoneinfo.ZoneInfo | None:
    """The zone called ``name``, or None when it is not a name this server knows."""
    if not isinstance(name, str) or not name or len(name) > TZ_MAX:
        return None
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None


def quiet_problem(start: object, end: object, tz: object, always: object) -> str | None:
    """Why these cannot be quiet hours, or None."""
    for m in (start, end):
        if isinstance(m, bool) or not isinstance(m, int) or not 0 <= m < DAY_MIN:
            return "Quiet hours need a start and an end time."
    if start == end:
        return "Quiet hours start and end at the same time."
    if zone_of(tz) is None:
        return "That time zone is not one this switchboard knows."
    if (
        not isinstance(always, (list, tuple, frozenset, set))
        or len(always) > len(KINDS)
        or any(k not in KINDS for k in always)
    ):
        return "Those are not kinds of alert."
    return None


def parse_hhmm(raw: object) -> int | None:
    """``"22:00"`` as minutes after midnight, or None. Only ``HH:MM``, 00:00 to 23:59."""
    if not isinstance(raw, str) or not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", raw):
        return None
    return int(raw[:2]) * 60 + int(raw[3:])


def hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def in_quiet(q: Quiet, now: float) -> bool:
    """Whether ``now`` is inside the quiet hours, by the wall clock of their zone at that instant. A zone
    that cannot be read is always quiet (it fails closed)."""
    z = zone_of(q.tz)
    if z is None:
        return True
    local = datetime.datetime.fromtimestamp(now, z)
    m = local.hour * 60 + local.minute
    if q.start < q.end:
        return q.start <= m < q.end
    return m >= q.start or m < q.end


def holds(q: Quiet | None, kind: str, now: float) -> bool:
    """Whether quiet hours hold an alert of ``kind`` now: inside them, and not a kind sent anyway."""
    return q is not None and kind not in q.always and in_quiet(q, now)


@dataclass
class _Try:
    """One alert's failed sends to one address."""

    attempts: int = 0
    last_try: float = 0.0

    def blocked(self, now: float) -> bool:
        return self.attempts >= MAX_ATTEMPTS or bool(self.attempts and now - self.last_try < RETRY_S)

    def record(self, now: float) -> None:
        self.attempts += 1
        self.last_try = now


@dataclass
class _Track:
    first_seen: float
    sent: set[str] = field(default_factory=set)  # the lanes it went out on, whatever address they had since
    tries: dict[tuple[str, int], _Try] = field(default_factory=dict)  # by lane and address generation
    released: float | None = None  # when its first alert was due: waited, not got, not held by quiet hours


class Policy:
    """When one webhook fires. State is the items it has seen, which of them it already sent on each lane
    (``sent``, and ``sent_second`` for second alerts, kept by the caller across restarts), the rooms the
    person opened and when, and each item's failed tries, kept by lane and by the generation of the lane's
    address (``see``): a new address starts with every try, and a result for the old one, back late, is kept
    where nothing reads it (#923, F4)."""

    def __init__(self, sent: Iterable[str] = (), sent_second: Iterable[str] = ()) -> None:
        self._tracks: dict[str, _Track] = {}
        for k in sent:
            self._tracks.setdefault(k, _Track(first_seen=0.0)).sent.add("first")
        for k in sent_second:
            self._tracks.setdefault(k, _Track(first_seen=0.0)).sent |= {"first", "second"}
        self._opened: dict[str, float] = {}
        self._gen = {"first": 0, "second": 0}

    def see(self, first_gen: int, second_gen: int) -> None:
        """The lanes' address generations as stored now. The tries of any other generation are dropped."""
        self._gen = {"first": first_gen, "second": second_gen}
        for t in self._tracks.values():
            for k in [k for k in t.tries if k[1] != self._gen[k[0]]]:
                del t.tries[k]

    def gen(self, lane: str) -> int:
        return self._gen[lane]

    def ack_room(self, slug: str, at: float) -> None:
        """The person opened this room at ``at``: whatever needed them in it since before then is got."""
        if at > self._opened.get(slug, float("-inf")):
            self._opened[slug] = at

    def sent_keys(self) -> list[str]:
        return sorted(k for k, t in self._tracks.items() if "first" in t.sent)

    def sent_second_keys(self) -> list[str]:
        return sorted(k for k, t in self._tracks.items() if "second" in t.sent)

    def _got(self, n: Need) -> bool:
        return n.at <= self._opened.get(n.slug, float("-inf"))  # they opened the room after this began

    def _blocked(self, t: _Track, lane: str, now: float) -> bool:
        if lane in t.sent:
            return True
        tr = t.tries.get((lane, self._gen[lane]))
        return tr is not None and tr.blocked(now)

    def due(
        self, needs: list[Need] | None, now: float, wait_s: float, quiet: Quiet | None = None
    ) -> list[Need]:
        """The first alerts to send now, oldest first, at most ``MAX_ITEMS``. ``needs`` None (not worked out)
        or a wait outside its bounds sends nothing and forgets nothing. An item that is no longer a need is
        dropped: it was got, and if it comes back it is new. An item quiet hours hold waits for their end,
        and the moment it is no longer held is when its first alert was due (``released``), sent or not."""
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
            if now - t.first_seen < wait_s or self._got(n) or holds(quiet, n.kind, now):
                continue
            if t.released is None:
                t.released = now
            if self._blocked(t, "first", now):
                continue
            out.append(n)
        return out[:MAX_ITEMS]

    def due_second(
        self, needs: list[Need] | None, now: float, after_s: float, quiet: Quiet | None = None
    ) -> list[Need]:
        """The second alerts to send now: items still needing the person ``after_s`` after their first
        alert was due, whether or not it went, not got and not held by quiet hours. Call it after ``due``
        in the same tick, which keeps ``released``. Unknown needs or an ``after_s`` outside its bounds send
        nothing."""
        if needs is None or not SECOND_MIN_S <= after_s <= SECOND_MAX_S:
            return []
        out: list[Need] = []
        for n in sorted(needs, key=lambda n: (n.at, n.key)):
            t = self._tracks.get(n.key)
            if t is None or t.released is None or now - t.released < after_s:
                continue
            if self._got(n) or holds(quiet, n.kind, now) or self._blocked(t, "second", now):
                continue
            out.append(n)
        return out[:MAX_ITEMS]

    def _record(self, lane: str, items: Iterable[Need], now: float, ok: bool, gen: int | None) -> None:
        g = self._gen[lane] if gen is None else gen
        for n in items:
            if (t := self._tracks.get(n.key)) is None:
                continue
            if not ok:  # kept by the address it was for: one replaced since is read by nobody, and pruned
                t.tries.setdefault((lane, g), _Try()).record(now)
            elif g == self._gen[lane]:
                t.sent.add(lane)

    def sent(self, items: Iterable[Need], now: float, ok: bool, gen: int | None = None) -> None:
        """Record a try of first alerts to the address of generation ``gen`` (the current one when None):
        sent when it went, else a counted failure to try again later. A try of an address that has since
        been replaced counts for nothing on the current one."""
        self._record("first", items, now, ok, gen)

    def sent_second(self, items: Iterable[Need], now: float, ok: bool, gen: int | None = None) -> None:
        """Record a try of second alerts, as ``sent`` does."""
        self._record("second", items, now, ok, gen)


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


def payload(items: list[Need], base: str, second: bool = False) -> dict[str, object]:
    """The JSON that is posted: who and where, and a link, and never what a message said. ``text`` is what
    a Slack incoming webhook posts and ``content`` what Discord does; ntfy takes the whole body. A second
    alert has one more field, ``"alert": "second"``, so a flow can route it; the first's is unchanged."""
    items = items[:MAX_ITEMS]
    lines = [f"{_line(n)}: {link_for(base, n.slug)}" for n in items]
    head = "switchboard, second alert: " if second else "switchboard: "
    text = head + (
        "\n".join(lines) if len(lines) == 1 else f"{len(lines)} things need you\n" + "\n".join(lines)
    )
    body: dict[str, object] = {
        "text": text,
        "content": text,
        "items": [
            {"kind": n.kind, "room": n.room, "who": n.who, "url": link_for(base, n.slug)} for n in items
        ],
    }
    if second:
        body["alert"] = "second"
    return body


def test_payload(base: str, second: bool = False) -> dict[str, object]:
    """What "Send a test" posts: the same shape, saying it is a test (and, to the second address, that it is
    the second alert's)."""
    if second:
        text = f"switchboard, second alert: this is a test, your second address works. {base.rstrip('/')}/"
        return {"text": text, "content": text, "items": [], "alert": "second"}
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
