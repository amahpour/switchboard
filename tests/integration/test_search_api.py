"""Search over the web API (#724, DESIGN.md §58): ``GET /api/search`` and the window around a message.

On a hosted broker with alice (the owner) and bob, with public and private rooms. What these catch: a person
reading, or being told the count of, a room they cannot see (private and not a member, left, closed); a
route that answers a signed-out request; a room filter that confirms a private room's name; a query that
reaches the index as syntax and becomes an error page; and a limit that never bites."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import pytest
from conftest import FakeClock, InProcBroker
from test_passkeys import hosted_broker, stop
from test_people import Browser, join_as_bob, set_up

from switchboard.models import session_key


@pytest.fixture
def team() -> Iterator[tuple[InProcBroker, Browser, Browser]]:
    clock = FakeClock()
    b = hosted_broker(clock)
    b.clock = clock  # type: ignore[attr-defined]
    admin = set_up(b)
    bob = join_as_bob(b, admin)
    try:
        assert admin.post("/api/rooms", {"name": "#build"}).status_code == 200
        yield b, admin, bob
    finally:
        bob.close()
        admin.close()
        stop(b)


def post(
    b: InProcBroker, room: str, text: str, *, name: str = "claude-1", kind: str = "agent", **kw: Any
) -> int:
    def run() -> int:
        store = b.state.store
        r = store.get_room(room)
        assert r is not None
        if kind == "agent":
            p = store.upsert_participant("test", session_key("test", "", name), host="")
            if store.find_member(r.id, name) is None:
                store.create_membership(r.id, p.id, name)
        return store.insert_message(
            r.id,
            sender_name=name,
            sender_kind=kind,
            via="mcp" if kind == "agent" else "web",
            text=text,
            sender_harness="claude" if kind == "agent" else None,
            **kw,
        ).id

    return int(b.on_loop(run))


def get(who: Browser, path: str, **params: Any) -> Any:
    return who.get(path + ("?" + urlencode(params) if params else ""))


def search(who: Browser, **params: Any) -> dict[str, Any]:
    r = get(who, "/api/search", **params)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_signed_out_request_is_refused(team: tuple[InProcBroker, Browser, Browser]) -> None:
    """Fails if the route answers without a session."""
    b, admin, _ = team
    r = Browser(b).get("/api/search?q=x")
    assert r.status_code == 401


def test_a_person_finds_lines_from_the_rooms_they_see_with_highlights(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """The hit says its room, author, kind and a snippet whose ranges cover the words. Fails if a hit is
    missing a field the page draws from, or the offsets do not point at the matched words."""
    b, admin, bob = team
    mid = post(b, "#build", "if the migration fails we need a rollback path", name="claude-1")
    got = search(admin, q="migration rollback")
    assert got["total"] == 1 and got["capped"] is False and got["next"] is None
    (h,) = got["hits"]
    assert (h["id"], h["room"], h["slug"], h["from"], h["kind"], h["harness"], h["call"]) == (
        mid,
        "#build",
        "build",
        "claude-1",
        "agent",
        "claude",
        False,
    )
    t = h["snippet"]["text"]
    assert [t[a:z] for a, z in h["snippet"]["marks"]] == ["migration", "rollback"]
    assert search(bob, q="migration")["total"] == 1  # a public room is everyone's


def test_a_private_room_is_searched_only_for_its_people_and_never_counted(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """Bob is not in #secret: its lines are not hits, not in the total, and naming the room is "no such
    room". Adding him makes them his; removing him takes them away again (a room he left). Fails if
    visibility is checked after counting, or a second rule for who sees a room is used."""
    b, admin, bob = team
    assert admin.post("/api/rooms", {"name": "#secret", "private": True}).status_code == 200
    post(b, "#secret", "the launch codes are swordfish", name="alice", kind="human")
    post(b, "#build", "swordfish is also a public word")
    assert search(admin, q="swordfish")["total"] == 2
    got = search(bob, q="swordfish")
    assert got["total"] == 1 and [h["room"] for h in got["hits"]] == ["#build"]
    r = get(bob, "/api/search", **{"q": "swordfish", "room": "secret"})
    assert r.status_code == 404 and "no such room" in r.text
    ghost = get(bob, "/api/search", **{"q": "swordfish", "room": "nowhere"})
    assert (ghost.status_code, ghost.json()["message"].replace("nowhere", "secret")) == (
        r.status_code,
        r.json()["message"],
    )
    assert admin.post("/api/rooms/secret/people", {"action": "add", "name": "bob"}).status_code == 200
    assert search(bob, q="swordfish")["total"] == 2
    assert bob.post("/api/rooms/secret/people", {"action": "remove", "name": "bob"}).status_code == 200
    assert search(bob, q="swordfish")["total"] == 1


def test_a_closed_room_is_not_searched(team: tuple[InProcBroker, Browser, Browser]) -> None:
    """Fails if a closed room's old lines stay searchable."""
    b, admin, _ = team
    assert admin.post("/api/rooms", {"name": "#old"}).status_code == 200
    post(b, "#old", "legacy plumbing notes", name="alice", kind="human")
    assert search(admin, q="plumbing")["total"] == 1
    assert admin.post("/api/rooms/old/command", {"text": "/close"}).status_code == 200
    assert search(admin, q="plumbing")["total"] == 0
    assert get(admin, "/api/search", **{"q": "plumbing", "room": "old"}).status_code == 404


def test_filters_and_the_cursor_over_the_route(team: tuple[InProcBroker, Browser, Browser]) -> None:
    """Room, from, time and calls filters combine; 60 matches page as 50 and 10 with ``next``. Fails if a
    filter is dropped on the way from the query string, or the cursor repeats a hit."""
    b, admin, _ = team
    assert admin.post("/api/rooms", {"name": "#other"}).status_code == 200
    ids = [post(b, "#build", f"tick {i}", name="claude-1") for i in range(60)]
    post(b, "#other", "tick elsewhere", name="alice", kind="human")
    call = post(b, "#build", "tick spoken", name="bob", kind="meeting")
    first = search(admin, q="tick", room="build")
    assert first["total"] == 61 and len(first["hits"]) == 50 and first["next"] == first["hits"][-1]["id"]
    assert first["hits"][0]["id"] == call and first["hits"][0]["call"] is True
    second = search(admin, q="tick", room="build", before=first["next"])
    assert "total" not in second and second["next"] is None
    assert [h["id"] for h in first["hits"][1:] + second["hits"]] == ids[::-1]
    assert search(admin, q="tick", calls=0)["total"] == 61
    assert search(admin, q="tick", **{"from": "alice"})["total"] == 1
    assert search(admin, q="tick", since=2e9)["total"] == 0
    assert search(admin, q="tick", until=1)["total"] == 0


@pytest.mark.parametrize("q", ["", "   ", "NEAR(a b", "a OR", '"', "col:x", "^x", "x" * 5000, "\x00"])
def test_a_query_that_cannot_be_made_safe_is_no_results_not_an_error(
    team: tuple[InProcBroker, Browser, Browser], q: str
) -> None:
    """Fails if hostile text reaches FTS5 as syntax (a 500) or an empty query is an error."""
    _, admin, _ = team
    got = search(admin, q=q)
    assert got["hits"] == []


@pytest.mark.parametrize("bad", [{"since": "x"}, {"until": "-1"}, {"before": "abc"}, {"calls": "2"}])
def test_bad_parameters_are_refused(team: tuple[InProcBroker, Browser, Browser], bad: dict[str, str]) -> None:
    _, admin, _ = team
    assert get(admin, "/api/search", **{"q": "x", **bad}).status_code == 400


def test_one_person_is_limited_and_another_is_not(team: tuple[InProcBroker, Browser, Browser]) -> None:
    """The 31st search in ten seconds is a 429 for that person only, and comes back when the window moves.
    Fails if there is no limit, or if one person's searches slow another's."""
    b, admin, bob = team
    for _ in range(30):
        assert get(admin, "/api/search", **{"q": "x"}).status_code == 200
    r = get(admin, "/api/search", **{"q": "x"})
    assert r.status_code == 429 and r.json()["error"] == "slow_down"
    assert get(bob, "/api/search", **{"q": "x"}).status_code == 200
    b.clock.advance(11)  # type: ignore[attr-defined]
    assert get(admin, "/api/search", **{"q": "x"}).status_code == 200


def test_the_window_around_a_message_is_checked_like_the_room(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """``messages?around=`` answers 20 and 20 for a visible room and "no such room" for a private one;
    a message id of another room is "no message". Fails if the new parameter skips ``room_for``."""
    b, admin, bob = team
    ids = [post(b, "#build", f"w {i}", name="claude-1") for i in range(50)]
    r = get(bob, "/api/rooms/build/messages", **{"around": ids[25]})
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["messages"]] == ids[5:46]
    assert admin.post("/api/rooms", {"name": "#secret", "private": True}).status_code == 200
    s = post(b, "#secret", "hidden", name="alice", kind="human")
    assert get(bob, "/api/rooms/secret/messages", **{"around": s}).status_code == 404
    assert get(admin, "/api/rooms/build/messages", **{"around": s}).status_code == 404
    assert get(admin, "/api/rooms/secret/messages", **{"around": s}).status_code == 200
