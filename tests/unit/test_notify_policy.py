"""The webhook policy (DESIGN.md §62, #723): when a person's webhook fires and when it stops, what a POST
says, and where a webhook may never point. Pure: the time is an argument, advanced by a ``FakeClock``."""

from __future__ import annotations

import json

import pytest
from conftest import FakeClock

from switchboard import notify
from switchboard.notify import Limiter, Need, Policy

BASE = "https://sb.example.com"


def need(
    key: str = "q:build:7", kind: str = "question", slug: str = "build", at: float = 0.0, who: str = "scout"
) -> Need:
    return Need(key, kind, slug, slug, who, at)


def test_nothing_is_sent_before_the_wait_and_one_is_sent_after() -> None:
    """An item waits ``wait_s`` before it is due, then is due. Fails if it fires at once or never."""
    clock, p = FakeClock(), Policy()
    n = need()
    assert p.due([n], clock.now(), 300) == []
    clock.advance(299)
    assert p.due([n], clock.now(), 300) == []
    clock.advance(1)
    assert p.due([n], clock.now(), 300) == [n]


def test_a_sent_item_is_not_sent_again_and_survives_a_restart() -> None:
    """Once sent, an item stays quiet while it still needs the person, and a restart (the sent keys are
    kept by the caller) does not send it again. Fails if a second tick repeats it."""
    p = Policy()
    n = need()
    p.due([n], 0, 60)
    due = p.due([n], 100, 60)
    p.sent(due, 100, True)
    assert p.due([n], 5000, 60) == []
    again = Policy(p.sent_keys())
    assert again.due([n], 0, 60) == [] and again.due([n], 9000, 60) == []


def test_got_it_stops_it_and_a_return_is_new() -> None:
    """An item that no longer needs the person (answered, decided, cleared) is dropped before it fires, and if
    it comes back it waits again from the start. Fails if a vanished item still sends."""
    p = Policy()
    n = need()
    p.due([n], 0, 60)
    assert p.due([], 50, 60) == []
    assert p.due([n], 100, 60) == []  # new again at 100
    assert p.due([n], 160, 60) == [n]


def test_opening_the_room_after_it_began_is_got_it_but_a_later_item_still_sends() -> None:
    """The person opening the room is "got it" for what began before they opened it; an item that began after
    is not covered. Fails if opening a room silences what it has not shown."""
    p = Policy()
    old, new = need("q:build:1", at=10), need("q:build:2", at=500)
    p.due([old, new], 0, 60)
    p.ack_room("build", 100)
    assert p.due([old, new], 1000, 60) == [new]
    p.ack_room("other", 5000)
    assert p.due([new], 1000, 60) == [new]


def test_a_failure_is_retried_slowly_and_then_given_up() -> None:
    """A failed send is tried again after ``RETRY_S`` and gives up after ``MAX_ATTEMPTS``. Fails if it hammers
    the address or retries forever."""
    p, n, t = Policy(), need(), 0.0
    p.due([n], t, 60)
    t = 60.0
    for attempt in range(notify.MAX_ATTEMPTS):
        due = p.due([n], t, 60)
        assert due == [n], attempt
        p.sent(due, t, False)
        assert p.due([n], t + 1, 60) == []
        t += notify.RETRY_S
    assert p.due([n], t + 10_000, 60) == []


def test_unknown_state_and_a_bad_wait_send_nothing() -> None:
    """A need list that could not be worked out (None) and a wait outside its bounds send nothing, and the
    first forgets nothing. Fails open if either sends."""
    p, n = Policy(), need()
    p.due([n], 0, 60)
    assert p.due(None, 10_000, 60) == []
    assert p.due([n], 10_000, 59) == []
    assert p.due([n], 10_000, notify.WAIT_MAX_S + 1) == []
    assert p.due([n], 10_000, float("nan")) == []
    assert p.due([n], 10_000, 60) == [n]  # still known after the None


def test_one_post_names_at_most_five_oldest_first() -> None:
    p = Policy()
    needs = [need(f"q:r:{i}", at=float(i)) for i in range(8)]
    p.due(needs, 0, 60)
    assert [n.key for n in p.due(needs, 100, 60)] == [f"q:r:{i}" for i in range(5)]


def test_the_limiter_allows_twelve_an_hour() -> None:
    """The twelfth post is allowed and the thirteenth is not until an hour after the first."""
    clock, lim = FakeClock(), Limiter()
    for _ in range(notify.MAX_PER_HOUR):
        assert lim.allow(clock.now())
        lim.record(clock.now())
        clock.advance(10)
    assert not lim.allow(clock.now())
    clock.advance(3600 - 10 * notify.MAX_PER_HOUR)
    assert lim.allow(clock.now())


def test_a_post_says_who_and_where_with_a_link_and_never_the_text() -> None:
    """The payload names who and which room and links to it. Fails if it grows a text field, or a link that
    is not the room's address."""
    body = notify.payload([need(), need("a:1:2", "approval", "uploads", who="bench")], BASE)
    assert json.dumps(body) == json.dumps(json.loads(json.dumps(body)))
    assert body["text"] == body["content"]
    assert "scout has a message for you in #build: https://sb.example.com/#build" in str(body["text"])
    assert "bench is waiting for your approval in #uploads: https://sb.example.com/#uploads" in str(
        body["text"]
    )
    assert set(body) == {"text", "content", "items"}
    assert all(set(i) == {"kind", "room", "who", "url"} for i in body["items"])  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "ftp://hooks.example.com/x",
        "https://user:pw@hooks.example.com/x",
        "https://hooks.example.com@evil.example.net/x",
        "https:///x",
        "https://hooks.example.com:99999/x",
        "https://hooks.example.com/a b",
        "https://hooks.example.com/\n",
        "https://" + "a" * 600 + ".example.com/",
        "",
        None,
        5,
    ],
)
def test_a_bad_address_is_refused_before_anything_is_resolved(url: object) -> None:
    assert notify.url_problem(url) is not None


def test_a_good_address_passes_the_syntax_check() -> None:
    for ok in ("https://hooks.example.com/services/T0/B0/xyz", "https://ntfy.example.com:8443/topic?x=1"):
        assert notify.url_problem(ok) is None


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.9",
        "192.168.1.5",
        "169.254.169.254",  # the cloud metadata address
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fd00:ec2::254",  # the metadata address, IPv6
        "::ffff:127.0.0.1",  # IPv4 wrapped in IPv6
        "::ffff:169.254.169.254",
        "64:ff9b::7f00:1",  # NAT64 around 127.0.0.1
        "2002:7f00:1::",  # 6to4 around 127.0.0.1
        "not an ip",
    ],
)
def test_internal_addresses_are_refused_by_default(ip: str) -> None:
    """Loopback, private, link-local, metadata, carrier-grade NAT, multicast, unspecified and every IPv6 form
    that wraps one are refused. Fails if the broker could be pointed at its own network."""
    assert notify.address_problem(ip, False) is not None


def test_public_addresses_pass() -> None:
    for ip in ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946", "::ffff:93.184.216.34"):
        assert notify.address_problem(ip, False) is None


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "192.168.1.5", "fd12::1", "::1"])
def test_the_operators_setting_allows_private_and_loopback(ip: str) -> None:
    assert notify.address_problem(ip, True) is None


@pytest.mark.parametrize(
    "ip", ["169.254.169.254", "fe80::1", "0.0.0.0", "224.0.0.1", "::ffff:169.254.169.254"]
)
def test_the_operators_setting_never_allows_metadata_or_unspecified(ip: str) -> None:
    """Even a home network's setting never reaches link-local (metadata), unspecified or multicast."""
    assert notify.address_problem(ip, True) is not None


def test_a_failure_reason_is_a_fixed_sentence() -> None:
    assert notify.fail_reason("tls") == "The webhook's certificate was not trusted."
    assert notify.fail_reason("whatever https://secret") == notify.fail_reason("error")
