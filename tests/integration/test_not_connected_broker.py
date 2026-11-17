"""Not connected through a real in-process broker (#814, DESIGN.md §49.14): what a person, an agent and a
session's own listener see, on a ``FakeClock`` the test moves.

The session is Claude Code over HTTPS (the shape the issue is about): it holds a wait open while it listens,
and is "not connected" when it holds none and has been quiet for 30 minutes. Nothing here sleeps to mean
"by now": the clock is advanced by hand, and the one wait for the broker's own tick is an awaited condition.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker
from test_agents_http_claude import SESSION_1, Claude
from test_agents_http_wake import joined_claude, listen, parked, participant, row, until

from switchboard.config import Config
from switchboard.delivery import rules

OTHER_SESSION = "00000000-0000-4000-8000-0000000000c2"
CFG = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0, rate_limit_s=0.0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def build(tmp_home: Path, clock: FakeClock) -> Iterator[InProcBroker]:
    b = InProcBroker(tmp_home, CFG, clock=clock).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web  # type: ignore[attr-defined]
    try:
        yield b
    finally:
        web.close()
        b.stop()


def two_sessions(b: InProcBroker) -> tuple[Claude, Claude, str]:
    one, token = joined_claude(b)
    two = Claude(b, token, OTHER_SESSION)
    assert two.call("join", room="#build", screen_name="claude-2")["ok"]
    two.call("read", room="#build")
    return one, two, token


def gone(b: InProcBroker, name: str) -> float | None:
    return row(b, name)["not_connected_since"]


def say_as_person(b: InProcBroker, text: str) -> int:
    web: Any = b.web  # type: ignore[attr-defined]
    r = web.post("/api/rooms/build/say", json={"text": text}, headers=b.write_headers())
    assert r.status_code == 200, r.text
    return int(r.json()["id"])


def room_text(b: InProcBroker) -> str:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    try:
        return "\n".join(r[0] for r in con.execute("SELECT text FROM messages ORDER BY id"))
    finally:
        con.close()


def test_a_quiet_session_shows_not_connected_to_the_page_and_to_who_and_any_call_clears_it(
    build: InProcBroker, clock: FakeClock
) -> None:
    """After 30 minutes with no wait open, claude-1 is not connected: Members' row carries when it was last
    heard from, and another agent's who() says so. Its own next call (not a hook, not a listener) clears it.
    Fails if the state isn't in the members frame, who() hides it, or a call doesn't count as being there."""
    one, two, _token = two_sessions(build)
    heard = clock.now()
    assert gone(build, "claude-1") is None
    clock.advance(rules.NOT_CONNECTED_IDLE_S - 1)
    assert gone(build, "claude-1") is None  # the edge is the second, not the minute
    clock.advance(1)
    assert gone(build, "claude-1") == heard
    assert gone(build, "claude-2") == heard  # neither is listening
    two_view = two.call("who", room="#build")
    assert two_view["ok"]
    assert "not_connected_since=" in two_view["text"].split("claude-1")[1].splitlines()[0]
    assert gone(build, "claude-2") is None  # that call was its proof: who() of the one calling is clear
    one.call("read", room="#build")
    assert gone(build, "claude-1") is None


def test_a_listener_round_keeps_a_session_connected_and_the_end_of_its_last_round_starts_the_clock(
    build: InProcBroker, clock: FakeClock
) -> None:
    """The proof an idle session is there is a wait it holds open: parked for three hours, it is still
    connected; when its listener ends (here, a message wakes it, so it is busy) and no hook follows, 2 hours
    after that moment it is not. Fails if the clock ran from the park's start (gone at once), or ignored the
    park."""
    _one, _two, token = two_sessions(build)
    p = participant(build, "claude-1")
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(listen, build, token, SESSION_1)
        until(lambda: parked(build, p.id))
        clock.advance(3 * 3600)
        assert gone(build, "claude-1") is None
        woke_at = clock.now()
        build.call("human.say", {"room": "#build", "text": "@claude-1 are you there"})
        assert waiting.result(30).status_code == 200
    clock.advance(rules.NOT_CONNECTED_BUSY_S - 1)
    assert gone(build, "claude-1") is None
    clock.advance(1)
    assert gone(build, "claude-1") == woke_at


def test_naming_a_member_that_is_not_connected_tells_the_sender_and_a_broadcast_skips_it(
    build: InProcBroker, clock: FakeClock
) -> None:
    """A person's @claude-1 and an agent's @claude-1 each get one sentence saying since when (a notice in
    the room for the person, ``note`` in the agent's say result); the message is still stored and goes to
    it. @everyone from a person leaves claude-1 out of the mentions. Fails if a message to a gone member is
    silent, or a broadcast still names it."""
    _one, two, _token = two_sessions(build)
    clock.advance(rules.NOT_CONNECTED_IDLE_S + 1)
    assert gone(build, "claude-1") is not None
    mid = say_as_person(build, "@claude-1 are you there?")
    assert "claude-1 hasn't been connected since" in room_text(build)
    stored = build.on_loop(build.state.store.get_message, mid)
    assert stored is not None and "claude-1" in stored.mentions  # still stored, still addressed
    said = two.call("say", room="#build", text="@claude-1 please look")
    assert said["ok"] and "claude-1 hasn't been connected since" in said["note"]
    assert "claude-2" not in said["note"]
    bid = say_as_person(build, "@everyone status?")
    mentions = build.on_loop(build.state.store.get_message, bid).mentions
    assert "claude-1" not in mentions and "everyone" in mentions


def test_remove_ends_a_member_that_is_not_connected_but_refuses_one_that_is(
    build: InProcBroker, clock: FakeClock
) -> None:
    """Remove is for the gone only: a connected claude-2 is refused with 409, the gone claude-1 is removed
    from its rooms, and its listener's next call is told why, in words that say it may join again. Fails if
    Remove becomes a second kick that works on a live agent, or the removed session is left without a word."""
    _one, _two, token = two_sessions(build)
    clock.advance(rules.NOT_CONNECTED_IDLE_S + 1)
    _two.call("read", room="#build")  # claude-2 is back; claude-1 is not
    h = build.write_headers()
    web: Any = build.web  # type: ignore[attr-defined]
    refused = web.post("/api/rooms/build/members/claude-2/remove", json={}, headers=h)
    assert refused.status_code == 409, refused.text
    ok = web.post("/api/rooms/build/members/claude-1/remove", json={}, headers=h)
    assert ok.status_code == 200 and ok.json() == {"removed": "claude-1"}, ok.text
    assert all(m["name"] != "claude-1" for m in build.call("room.who", {"room": "#build"})["members"])
    r = listen(build, token, SESSION_1)
    assert r.status_code == 410, r.text
    assert "so your user removed it: join again" in r.text, r.text
    assert web.post("/api/rooms/build/members/ghost/remove", json={}, headers=h).status_code == 404


def test_the_cleanup_ends_a_session_after_the_admins_days_and_its_410_says_so(
    build: InProcBroker, clock: FakeClock
) -> None:
    """After 3 days with no proof, the broker's own tick ends claude-1 (not claude-2, which called), and its
    listener is answered 410 with 'not connected for 3 days: join again'. The only wait here is for the
    runner's tick to have run. Fails if the tick never ends it, ends a live one, or the 410 says nothing."""
    _one, two, token = two_sessions(build)
    clock.advance(3 * rules.DAY_S + 1)
    two.call("read", room="#build")
    until(lambda: all(m["name"] != "claude-1" for m in build.call("room.who", {"room": "#build"})["members"]))
    assert [m["name"] for m in build.call("room.who", {"room": "#build"})["members"]] == ["claude-2"]
    r = listen(build, token, SESSION_1)
    assert r.status_code == 410, r.text
    assert "not connected for 3 days: join again" in r.text, r.text
    assert "claude-1 was removed: not connected for 3 days" in room_text(build)


def test_the_admin_setting_is_read_and_written_with_a_closed_range(build: InProcBroker) -> None:
    """GET gives 3 until set. PUT takes 0 (never) or 1 to 365, and nothing else (a string, a bool, 366, extra
    keys): an unrecognised value must never be stored, since the engine would then fall back to the default
    in silence. Fails if the range is open or the value isn't stored."""
    web: Any = build.web  # type: ignore[attr-defined]
    h = build.write_headers()
    assert web.get("/api/settings/not-connected").json() == {"days": 3}
    assert web.put("/api/settings/not-connected", json={"days": 7}, headers=h).json() == {"days": 7}
    assert web.get("/api/settings/not-connected").json() == {"days": 7}
    assert web.put("/api/settings/not-connected", json={"days": 0}, headers=h).json() == {"days": 0}
    for bad in ({"days": 366}, {"days": "3"}, {"days": True}, {"days": 2, "x": 1}, {}):
        r = web.put("/api/settings/not-connected", json=bad, headers=h)
        assert r.status_code == 400, (bad, r.text)
    assert web.get("/api/settings/not-connected").json() == {"days": 0}  # none of them changed it
