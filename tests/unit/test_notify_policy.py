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
    return Need(key, kind, f"#{slug}", slug, who, at)


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


def test_the_line_has_one_hash_before_a_room_name_that_already_has_it() -> None:
    """A room's stored name is ``#build``: the line must not add a second ``#`` (F1)."""
    n = Need("q:build:1", "question", "#build", "build", "scout", 0.0)
    assert "##" not in str(notify.payload([n], BASE)["text"])
    assert "in #build:" in str(notify.payload([n], BASE)["text"])


@pytest.mark.parametrize(
    "ip", ["168.63.129.16", "192.0.0.192", "100.100.100.200", "169.254.170.2", "fd00:ec2::254"]
)
@pytest.mark.parametrize("allow", [False, True])
def test_known_cloud_metadata_addresses_are_never_allowed(ip: str, allow: bool) -> None:
    """Azure's and Oracle's and Alibaba's metadata addresses, the ECS one and AWS's IPv6 one are refused with
    or without the operator's private setting (F2). Fails if one is reachable."""
    assert notify.address_problem(ip, allow) is not None


def test_a_failure_reason_is_a_fixed_sentence() -> None:
    assert notify.fail_reason("tls") == "The webhook's certificate was not trusted."
    assert notify.fail_reason("whatever https://secret") == notify.fail_reason("error")


# ------------------------------------------------------------------ the second alert and quiet hours (#923)
def _at(tz: str, *ymdhm: int) -> float:
    """The instant of a wall-clock time in a zone."""
    import datetime
    import zoneinfo

    return datetime.datetime(*ymdhm, tzinfo=zoneinfo.ZoneInfo(tz)).timestamp()


def tick(p: Policy, needs: list[Need], now: float, quiet: notify.Quiet | None = None) -> tuple[list, list]:
    """One notifier tick: the first alerts, then the second, as ``Notifier._one`` asks for them."""
    first = p.due(needs, now, 300, quiet)
    p.sent(first, now, True)
    second = p.due_second(needs, now, 1200, quiet)
    p.sent_second(second, now, True)
    return first, second


def test_the_second_alert_fires_its_delay_after_the_first_was_due_and_only_once() -> None:
    """The first alert goes after the wait; the second, 20 minutes after that, once. Fails if the second
    comes with the first, before its delay, or again on a later tick."""
    clock, p, n = FakeClock(), Policy(), need()
    assert tick(p, [n], clock.now()) == ([], [])
    clock.advance(300)
    assert tick(p, [n], clock.now()) == ([n], [])
    clock.advance(1199)
    assert tick(p, [n], clock.now()) == ([], [])
    clock.advance(1)
    assert tick(p, [n], clock.now()) == ([], [n])
    clock.advance(5000)
    assert tick(p, [n], clock.now()) == ([], [])


def test_the_second_alert_fires_when_the_first_post_failed() -> None:
    """It is about the person not having looked, not the first channel working: a first alert that failed
    every try still gets a second alert on time. Fails if the second waits for the first to succeed."""
    p, n, t = Policy(), need(), 0.0
    p.due([n], t, 300)
    t = 300.0
    p.sent(p.due([n], t, 300), t, False)
    for _ in range(4):
        t += notify.RETRY_S
        p.sent(p.due([n], t, 300), t, False)
    t = 300.0 + 1200
    p.due([n], t, 300)
    assert p.due_second([n], t, 1200) == [n]


def test_a_new_second_address_starts_its_tries_over_and_keeps_what_was_sent_and_got() -> None:
    """A second alert that failed every try at the old address is tried at a new one (a new generation,
    ``see``); one that went is not sent again, and a room the person opened stays got. Fails if the new
    address inherits the spent tries (#923, F4), or the new generation forgets what was sent or got."""
    p, n, done, got = Policy(), need(), need("q:build:8"), need("q:other:3", slug="other")
    t = 0.0
    p.due([n, done, got], t, 300)
    t = 300.0
    p.due([n, done, got], t, 300)
    p.ack_room("other", 400)
    t = 1500.0
    p.due([n, done, got], t, 1200)
    assert p.due_second([n, done, got], t, 1200) == [n, done]
    p.sent_second([done], t, True)
    for _ in range(notify.MAX_ATTEMPTS):
        p.sent_second([n], t, False)
        t += notify.RETRY_S
    p.due([n, done, got], t, 300)
    assert p.due_second([n, done, got], t, 1200) == []
    p.see(0, 1)
    assert p.due_second([n, done, got], t, 1200) == [n]
    assert p.sent_second_keys() == [done.key]


def test_a_late_result_for_a_replaced_address_counts_for_nothing_on_the_new_one() -> None:
    """A second alert is out to the old address (generation 0) when the address is replaced (1); the old
    post then fails, or goes. The new address's first try is due at once and has every try, and a late
    success is not "sent". Fails if a result is recorded by item alone, whatever address it was for (#923,
    F4: the new address's first try waited ``RETRY_S`` with one try spent)."""
    for ok in (False, True):
        p, n = Policy(), need(at=0)
        p.due([n], 0, 300)
        p.due([n], 300, 300)
        t = 300.0 + 1200
        p.due([n], t, 300)
        assert p.due_second([n], t, 1200) == [n]  # sent to generation 0 ...
        p.see(0, 1)  # ... the address is replaced while it is out ...
        p.sent_second([n], t, ok, 0)  # ... and its result is back
        assert p.sent_second_keys() == []
        for _ in range(notify.MAX_ATTEMPTS):
            assert p.due_second([n], t, 1200) == [n], ok
            p.sent_second([n], t, False, 1)
            t += notify.RETRY_S
        assert p.due_second([n], t, 1200) == []


def test_got_it_stops_the_second_alert() -> None:
    """Opening the room, or the item no longer needing them, after the first alert stops the second. Fails
    if a second alert goes to someone who already looked."""
    p, n = Policy(), need(at=0)
    tick(p, [n], 0)
    tick(p, [n], 300)
    p.ack_room("build", 600)
    assert tick(p, [n], 2000) == ([], [])
    q, m = Policy(), need("q:build:9", at=0)
    tick(q, [m], 0)
    tick(q, [m], 300)
    assert tick(q, [], 1000) == ([], [])
    assert tick(q, [m], 1600) == ([], [])  # back again: a new item, its wait starts over


def test_a_sent_second_alert_survives_a_restart_and_old_kept_lists_still_load() -> None:
    """The second alerts sent are kept apart from the first, so a restart does not send one again; a kept
    list from before #923 still means "first sent". Fails if either sends again after a restart."""
    p, n = Policy(), need()
    tick(p, [n], 0)
    tick(p, [n], 300)
    tick(p, [n], 1500)
    assert p.sent_keys() == [n.key] and p.sent_second_keys() == [n.key]
    again = Policy(p.sent_keys(), p.sent_second_keys())
    for t in (0, 300, 2000, 90_000):
        assert tick(again, [n], t) == ([], []), t
    only_first = Policy(p.sent_keys())  # the second alert had not gone before the restart: it still goes
    assert [tick(only_first, [n], t) for t in (0, 300, 1500)] == [([], []), ([], []), ([], [n])]
    old = Policy([n.key])
    assert old.due([n], 0, 300) == []


def test_a_second_delay_outside_its_bounds_or_unknown_needs_send_no_second_alert() -> None:
    p, n = Policy(), need()
    tick(p, [n], 0)
    tick(p, [n], 300)
    p.due([n], 99_999, 300)
    assert p.due_second([n], 99_999, notify.SECOND_MIN_S - 1) == []
    assert p.due_second([n], 99_999, notify.SECOND_MAX_S + 1) == []
    assert p.due_second([n], 99_999, float("nan")) == []
    assert p.due_second(None, 99_999, 1200) == []
    assert p.due_second([n], 99_999, 1200) == [n]


NY = "America/New_York"


def quiet(start: str = "22:00", end: str = "07:00", always: tuple[str, ...] = ("approval",)) -> notify.Quiet:
    s, e = notify.parse_hhmm(start), notify.parse_hhmm(end)
    assert s is not None and e is not None
    assert notify.quiet_problem(s, e, NY, list(always)) is None
    return notify.Quiet(s, e, NY, frozenset(always))


def test_quiet_hours_hold_a_question_and_a_board_but_not_an_approval() -> None:
    """Inside quiet hours only the kinds marked always sent go (an approval by default). Fails if a question
    or a board is posted at night, or a stuck agent is held."""
    q, night = quiet(), _at(NY, 2026, 6, 10, 23, 0)
    p = Policy()
    items = [need("q:build:1"), need("b:build:x", "board"), need("a:1:1", "approval")]
    p.due(items, night - 600, 300, q)
    assert [n.kind for n in p.due(items, night, 300, q)] == ["approval"]
    loud = quiet(always=())
    p2 = Policy()
    p2.due(items, night - 600, 300, loud)
    assert p2.due(items, night, 300, loud) == []


def test_a_held_item_goes_when_quiet_hours_end_and_its_second_alert_counts_from_then() -> None:
    """A question that came at 23:00 goes at 07:00 if it still needs them, and its second alert 20 minutes
    after 07:00, not 20 minutes after it first waited. Fails if it is dropped, sent early, or its second
    alert's clock started in the night."""
    q, p, n = quiet(), Policy(), need()
    t = _at(NY, 2026, 6, 10, 23, 0)
    for _ in range(int(8 * 3600 / 600) - 1):
        assert tick(p, [n], t, q) == ([], [])
        t += 600
    end = _at(NY, 2026, 6, 11, 7, 0)
    assert tick(p, [n], end - 1, q) == ([], [])
    assert tick(p, [n], end, q) == ([n], [])
    assert tick(p, [n], end + 1199, q) == ([], [])
    assert tick(p, [n], end + 1200, q) == ([], [n])


def test_a_second_alert_that_falls_in_quiet_hours_waits_for_their_end() -> None:
    q, p, n = quiet(), Policy(), need()
    t = _at(NY, 2026, 6, 10, 21, 50)
    tick(p, [n], t, q)
    assert tick(p, [n], t + 300, q) == ([n], [])  # 21:55, before quiet hours
    assert tick(p, [n], t + 300 + 1200, q) == ([], [])  # 22:15, held
    assert tick(p, [n], _at(NY, 2026, 6, 11, 7, 0), q) == ([], [n])


def test_quiet_hours_within_one_day_and_across_midnight() -> None:
    day = quiet("09:00", "17:00")
    assert notify.in_quiet(day, _at(NY, 2026, 6, 10, 12, 0))
    assert not notify.in_quiet(day, _at(NY, 2026, 6, 10, 17, 0))
    assert not notify.in_quiet(day, _at(NY, 2026, 6, 10, 8, 59))
    night = quiet("22:00", "07:00")
    for hm in ((22, 0), (23, 59), (0, 0), (6, 59)):
        assert notify.in_quiet(night, _at(NY, 2026, 6, 10, *hm)), hm
    for hm in ((7, 0), (12, 0), (21, 59)):
        assert not notify.in_quiet(night, _at(NY, 2026, 6, 10, *hm)), hm


def test_quiet_hours_follow_the_zone_on_a_spring_forward_day() -> None:
    """On 2026-03-08 New York goes from UTC-5 to UTC-4 at 02:00. 07:00 that morning is 11:00 UTC, so 11:30 UTC
    is after quiet hours. Fails if the offset of the night before (or a fixed one) is used."""
    q = quiet()
    utc = _at("UTC", 2026, 3, 8, 11, 30)
    assert not notify.in_quiet(q, utc)
    assert notify.in_quiet(q, _at("UTC", 2026, 3, 8, 10, 59))
    assert notify.in_quiet(q, _at("UTC", 2026, 3, 8, 6, 30))  # 02:30 does not exist; 01:30 EST is quiet


def test_quiet_hours_follow_the_zone_on_a_fall_back_day() -> None:
    """On 2026-11-01 New York goes from UTC-4 to UTC-5 at 02:00. 07:00 that morning is 12:00 UTC, so 11:30 UTC
    is still quiet. Fails if the summer offset is kept."""
    q = quiet()
    assert notify.in_quiet(q, _at("UTC", 2026, 11, 1, 11, 30))
    assert not notify.in_quiet(q, _at("UTC", 2026, 11, 1, 12, 0))
    assert notify.in_quiet(q, _at("UTC", 2026, 11, 1, 5, 30)) and notify.in_quiet(
        q, _at("UTC", 2026, 11, 1, 6, 30)
    )  # 01:30 happens twice; both are quiet


@pytest.mark.parametrize(
    "start,end,tz,always",
    [
        (1320, 1320, NY, []),  # the same time
        (-1, 420, NY, []),
        (1320, 1440, NY, []),
        (True, 420, NY, []),
        ("22:00", 420, NY, []),
        (1320, 420, "Mars/Olympus_Mons", []),
        (1320, 420, "../../etc/passwd", []),
        (1320, 420, "", []),
        (1320, 420, None, []),
        (1320, 420, "A" * 100, []),
        (1320, 420, NY, ["approval", "everything"]),
        (1320, 420, NY, "approval"),
        (1320, 420, NY, ["approval"] * 4),
    ],
)
def test_quiet_hours_that_cannot_be_read_are_refused(
    start: object, end: object, tz: object, always: object
) -> None:
    """A zone the server does not know, a time that is not one, an unknown kind: refused when saved."""
    assert notify.quiet_problem(start, end, tz, always) is not None


def test_a_zone_that_cannot_be_read_holds_everything() -> None:
    """Fails closed: quiet hours whose zone is gone (a row from another server) hold every kind but those
    always sent, at every hour. Fails if an unknown zone means "not quiet"."""
    q = notify.Quiet(1320, 420, "Mars/Olympus_Mons", frozenset())
    assert notify.in_quiet(q, _at("UTC", 2026, 6, 10, 12, 0))
    assert notify.holds(q, "question", 0.0) and notify.holds(q, "approval", 0.0)


def test_times_read_only_as_hh_mm() -> None:
    assert notify.parse_hhmm("22:00") == 1320 and notify.parse_hhmm("00:00") == 0
    assert notify.parse_hhmm("23:59") == 1439 and notify.hhmm(1439) == "23:59" and notify.hhmm(7) == "00:07"
    for bad in ("24:00", "7:00", "07:60", "07:00:00", " 07:00", "", None, 420):
        assert notify.parse_hhmm(bad) is None, bad


def test_the_second_alert_says_so_and_the_first_is_unchanged() -> None:
    """A second alert carries ``"alert": "second"`` and says it is one; the first alert has exactly its old
    three fields. Fails if a receiver parsing the first alert would see a new field."""
    first = notify.payload([need()], BASE)
    second = notify.payload([need()], BASE, second=True)
    assert set(first) == {"text", "content", "items"}
    assert second["alert"] == "second" and set(second) == {"text", "content", "items", "alert"}
    assert str(second["text"]).startswith("switchboard, second alert: scout has a message for you in #build")
    assert second["items"] == first["items"]
    assert notify.test_payload(BASE, second=True)["alert"] == "second"
    assert "alert" not in notify.test_payload(BASE)
