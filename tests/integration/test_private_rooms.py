"""Private rooms on a hosted broker (#74, DESIGN.md §44): only a private room's people see it,
by any route, REST, the WebSocket and the agents' MCP calls, and a person who isn't in it gets
the words a room that doesn't exist gets, so its name isn't confirmed either. A person's agents
go where the person goes; the admin sees names and people, never contents."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from test_passkeys import Browser, hosted_broker, stop
from test_people import join_as_bob, set_up

from switchboard.broker.hub import WsSubscriber
from switchboard.broker.web import _ws_hello


@pytest.fixture
def hosted() -> Iterator[InProcBroker]:
    b = hosted_broker()
    try:
        yield b
    finally:
        stop(b)


def room_names(br: Browser) -> list[str]:
    return [r["name"] for r in br.get("/api/rooms").json()["rooms"]]


def make_private(br: Browser, name: str) -> dict[str, Any]:
    r = br.post("/api/rooms", {"name": name, "private": True})
    assert r.status_code == 200, r.text
    return r.json()["room"]


NOPE = {"error": "not_found", "message": "no such room: #secret"}


def test_a_non_member_cant_see_read_or_post_by_any_web_route(hosted: InProcBroker) -> None:
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    room = make_private(bob, "#secret")
    assert (room["private"], room["people"]) == (True, ["bob"])
    assert bob.post("/api/rooms/secret/say", {"text": "only us"}).status_code == 200
    assert "#secret" in room_names(bob)
    # the admin isn't in it: every route says what it says of a room that isn't there
    assert "#secret" not in room_names(admin)
    for path in ("/api/rooms/secret/messages", "/api/rooms/secret/members", "/api/rooms/secret/review",
                 "/api/rooms/secret/members/claude-1"):  # fmt: skip
        r = admin.get(path)
        assert (r.status_code, r.json()) == (404, NOPE), path
    for path, body in (
        ("/api/rooms/secret/say", {"text": "hi"}),
        ("/api/rooms/secret/command", {"text": "/who"}),
        ("/api/rooms/secret/review", {"action": "close"}),
        ("/api/rooms/secret/people", {"action": "add", "name": "alice"}),
    ):
        r = admin.post(path, body)
        assert (r.status_code, r.json()) == (404, NOPE), path
    for path, body in (("/api/rooms/secret/rules", {"text": "x"}),
                       ("/api/rooms/secret/wake", {"budget_per_hour": 1, "hop_limit": 1})):  # fmt: skip
        r = admin.c.put(path, json=body, headers=admin.headers(True))
        assert (r.status_code, r.json()) == (404, NOPE), path
    # and bob's message stayed his room's
    assert [
        m["text"] for m in bob.get("/api/rooms/secret/messages").json()["messages"] if m["kind"] == "chat"
    ] == ["only us"]


def test_the_websocket_never_subscribes_a_non_member(hosted: InProcBroker) -> None:
    """A hello naming a private room the page's person isn't in subscribes nothing and replays
    nothing; the rooms frame every page gets never names a private room."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    make_private(bob, "#secret")
    bob.post("/api/rooms/secret/say", {"text": "only us"})
    state = hosted.state

    def hello(person: int) -> WsSubscriber:
        sub = WsSubscriber()
        sub.person = person
        assert hosted.on_loop(_ws_hello, state, sub, {"t": "hello", "rooms": ["#secret"], "after": {}})
        return sub

    outsider = hello(0)  # the admin's page
    assert outsider.rooms == set() and outsider.q.empty()
    insider = hello(state.store.person_named("bob").id)
    assert insider.rooms == {"#secret"} and not insider.q.empty()
    frames: list[Any] = []
    hosted.on_loop(lambda: state.hub.add(outsider))
    hosted.on_loop(state.service.rooms_changed)
    while not outsider.q.empty():
        frames.append(outsider.q.get_nowait())
    rooms = [f for f in frames if f and f.get("t") == "rooms"]
    assert rooms and all("#secret" not in f["rooms"] for f in rooms)


async def test_an_agent_joins_only_where_its_person_is(hosted: InProcBroker) -> None:
    """An agent on the broker's own machine is the admin's (§44.3): it can't join bob's private
    room, in the words of a room that isn't there, until bob adds the admin; when the admin
    leaves, so does their agent."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    make_private(bob, "#secret")
    hosted.state.test_mode = True
    async with FakeAgent(hosted.home, "k1") as agent:
        r = await agent.call("join", room="#secret", screen_name="claude-1")
        assert not r["ok"] and r["error"] == "no such room #secret: ask your user to create it"
        assert bob.post("/api/rooms/secret/people", {"action": "add", "name": "alice"}).status_code == 200
        assert "#secret" in room_names(admin)
        joined = await agent.join("#secret", "claude-1")
        assert joined["ok"], joined
        # the join text and who() name the room's people, not everyone on the broker
        assert "bob" in joined["text"]
        r = admin.post("/api/rooms/secret/people", {"action": "remove", "name": "alice"})
        assert r.status_code == 200 and r.json()["text"] == "you left #secret"
        members = bob.get("/api/rooms/secret/members").json()["members"]
        assert [m["name"] for m in members] == []
        assert "#secret" not in room_names(admin)


def test_people_come_and_go_and_the_last_one_stays(hosted: InProcBroker) -> None:
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    make_private(bob, "#secret")
    r = bob.post("/api/rooms/secret/people", {"action": "add", "name": "nobody"})
    assert (r.status_code, r.json()["message"]) == (404, "no such person: nobody")
    r = bob.post("/api/rooms/secret/people", {"action": "add", "name": "bob"})
    assert (r.status_code, r.json()["message"]) == (409, "bob is in #secret already")
    r = bob.post("/api/rooms/secret/people", {"action": "remove", "name": "bob"})
    assert r.status_code == 400 and r.json()["message"].startswith("the last person can't leave #secret")
    assert bob.post("/api/rooms/secret/people", {"action": "add", "name": "alice"}).json()["room"][
        "people"
    ] == [
        "bob",
        "alice",
    ]
    notices = [
        m["text"] for m in bob.get("/api/rooms/secret/messages").json()["messages"] if m["kind"] == "notice"
    ]
    assert "bob added alice to #secret" in notices
    # a public room has no people to add
    admin.post("/api/rooms", {"name": "#open"})
    r = admin.post("/api/rooms/open/people", {"action": "add", "name": "bob"})
    assert (r.status_code, r.json()["message"]) == (400, "#open isn't private: everyone signed in sees it")


def test_the_admin_sees_names_and_people_and_a_removed_person_leaves(hosted: InProcBroker) -> None:
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    make_private(bob, "#secret")
    bob.post("/api/rooms/secret/people", {"action": "add", "name": "alice"})
    assert admin.get("/api/private-rooms").json()["rooms"] == [
        {"name": "#secret", "people": ["bob", "alice"], "created_by": "bob"}
    ]
    assert bob.get("/api/private-rooms").status_code == 403  # the admin section is the admin's
    pid = hosted.state.store.person_named("bob").id
    assert admin.post(f"/api/people/{pid}/remove", {}).status_code == 200
    assert admin.get("/api/private-rooms").json()["rooms"] == [
        {"name": "#secret", "people": ["alice"], "created_by": "bob"}
    ]


def test_a_desktop_broker_has_no_private_rooms(broker: InProcBroker) -> None:
    web = broker.web_client()
    try:
        r = web.post("/api/rooms", json={"name": "#solo", "private": True}, headers=broker.write_headers())
        assert (r.status_code, r.json()["message"]) == (
            400,
            "private rooms are for a hosted broker, with people",
        )
        r = web.post("/api/rooms", json={"name": "#solo", "private": "yes"}, headers=broker.write_headers())
        assert r.status_code == 400
    finally:
        web.close()


def test_existing_rooms_stay_public(hosted: InProcBroker) -> None:
    admin = set_up(hosted)
    room = admin.post("/api/rooms", {"name": "#build"}).json()["room"]
    assert (room["private"], room["people"]) == (False, None)
    json.dumps(room)


def test_a_machine_is_told_only_the_private_rooms_its_person_is_in(hosted: InProcBroker) -> None:
    """A machine that dials in is welcomed with the open rooms' names (§28.8): a private room's
    name goes only to a machine whose person is in it (§44.3), and an agent there is that
    person's."""
    from types import SimpleNamespace

    from switchboard.broker.remote import RemoteLink

    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    admin.post("/api/rooms", {"name": "#open"})
    make_private(bob, "#secret")
    store = hosted.state.store
    bob_id = store.person_named("bob").id
    hosted.on_loop(lambda: store.machine_pair("bobs-laptop", b"k" * 32, "fp-bob", {}, person_id=bob_id))
    hosted.on_loop(lambda: store.machine_pair("alices-pi", b"a" * 32, "fp-alice", {}, person_id=None))

    def welcome(name: str) -> list[str]:
        link = SimpleNamespace(entry=SimpleNamespace(rooms=("*",), name=name), st=hosted.state)
        return hosted.on_loop(RemoteLink._welcome_rooms, link)

    assert welcome("bobs-laptop") == ["#open", "#secret"]
    assert welcome("alices-pi") == ["#open"]
    assert hosted.state.service.host_person("bobs-laptop") == bob_id
    assert hosted.state.service.host_person("alices-pi") == 0 and hosted.state.service.host_person("") == 0
