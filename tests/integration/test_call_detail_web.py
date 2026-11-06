"""The call's details over the web API (#623, DESIGN.md §54): ``GET /api/rooms/{slug}/call``.

The call is a member of the room in the page (a row in Members that opens its details, with **End this
call**). The settings frame every page gets says the same to everyone, so whether *this* person may end the
call is asked for, per viewer, here. The route is read-only, answers only a signed-in person who may see the
room, and says nothing that could be used to end the call or to find another person: no key, no hash, no
person's id, only the key's fingerprint. The hosted broker and its people are ``test_people``'s; a hosted
call is planted in its rows, as the other tests of calls plant theirs, and the end is the real command.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_people import Browser, join_as_bob, set_up

from switchboard.meeting import keys

pytest_plugins = ("test_people",)  # makes its hosted fixture available to this module

ALICE_KEY = "sbc_" + "a1" * 32
BOB_KEY = "sbc_" + "b2" * 32


def plant(
    b: InProcBroker, room: str, device: str, person: int | None, key: str, *, live: bool = True
) -> None:
    """A call hosted from ``device`` by ``person`` (None: the owner) in ``room``, with its token's row."""

    def run() -> None:
        store = b.state.store
        name = store.person(person).name if person is not None else b.state.cfg.human_name
        token = store.agent_token_add(person, device, hashlib.sha256(device.encode()).hexdigest())
        assert token is not None
        r = store.get_room(room)
        assert r is not None
        row = store.insert_hosted_meeting(
            r.id,
            started_by=name,
            platform="zoom",
            key_hash=keys.key_hash(key),
            token_id=token.id,
            owner_person_id=person,
            host=device,
            model="large-v3-turbo",
        )
        if live:
            store.set_meeting_live(row.id)

    b.on_loop(run)


@pytest.fixture
def team(hosted: InProcBroker) -> Iterator[tuple[InProcBroker, Browser, Browser]]:
    """A hosted broker: alice the owner (an admin) and bob an ordinary person, with #build (a call alice hosts
    from her laptop) and #uploads (a call bob hosts from his)."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    try:
        for name in ("#build", "#uploads"):
            assert admin.post("/api/rooms", {"name": name}).status_code == 200
        plant(hosted, "#build", "alice-laptop", None, ALICE_KEY)
        plant(hosted, "#uploads", "bobs-laptop", hosted.state.store.person_named("bob").id, BOB_KEY)
        yield hosted, admin, bob
    finally:
        bob.close()
        admin.close()


def call(who: Browser, room: str) -> dict[str, Any]:
    r = who.get(f"/api/rooms/{room}/call")
    assert r.status_code == 200, r.text
    return r.json()


def test_each_viewer_is_told_whether_they_may_end_the_call_and_the_frame_is_the_same_for_all(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """The owner's call: its owner (alice, an admin) may end it, bob may not. Bob's call: bob may, and alice
    may too (an admin). The same rule the command holds, so the page shows End this call only where the
    server would not refuse it. The settings the room's frame carries are the same for both viewers and
    hold no such answer. Fails if the answer is not per viewer, or is given to everyone, or goes in the
    frame."""
    b, admin, bob = team
    assert call(admin, "build")["call"]["can_end"] is True
    assert call(bob, "build")["call"]["can_end"] is False
    assert call(bob, "uploads")["call"]["can_end"] is True
    assert call(admin, "uploads")["call"]["can_end"] is True
    refused = bob.post("/api/rooms/build/command", {"text": "/meeting leave"})
    assert refused.status_code == 403  # what the page hid is what the server refuses
    rooms = {
        n: {r["name"]: r["settings"]["meeting"] for r in w.get("/api/rooms").json()["rooms"]}
        for n, w in (("alice", admin), ("bob", bob))
    }
    assert rooms["alice"] == rooms["bob"] and set(rooms["bob"]["#build"]) == {
        "state",
        "platform",
        "voice",
        "source",
        "host",
        "fingerprint",
    }


def test_the_answer_says_what_the_details_show_and_nothing_that_could_end_the_call_or_name_another_person(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """The fields the details show (where it runs, who started it, since when, the model, the lines it has
    sent, voice) and the key's fingerprint. Never the key, its hash, a person's or a token's id, or the call's
    own id. Fails if one of them is in the text."""
    b, admin, bob = team
    got = call(bob, "uploads")
    assert got["room"] == "#uploads"
    d = got["call"]
    assert (d["source"], d["state"], d["platform"], d["host"]) == ("hosted", "live", "zoom", "bobs-laptop")
    assert (d["started_by"], d["model"], d["lines"], d["voice"]) == ("bob", "large-v3-turbo", 0, False)
    assert d["fingerprint"] == keys.fingerprint(keys.key_hash(BOB_KEY))
    assert isinstance(d["live_at"], float) and d["live_at"] >= d["started_at"]
    text = json.dumps(got)
    assert BOB_KEY not in text and keys.key_hash(BOB_KEY) not in text and "sbc_" not in text
    assert not {"owner_person_id", "token_id", "key_hash", "id", "person_id", "error"} & set(d)
    # alice's call, seen by bob: the same shape, with her name and her machine, nothing that is hers alone
    theirs = call(bob, "build")["call"]
    assert (theirs["started_by"], theirs["host"]) == ("alice", "alice-laptop")


def test_a_line_the_call_posts_counts_in_the_details(team: tuple[InProcBroker, Browser, Browser]) -> None:
    """The lines sent are the call's own count (the store counts a line in the same transaction as it posts
    it), so the page shows a number that moves as the relay's lines arrive. Fails if the details read some
    other number than the row's."""
    b, admin, bob = team

    def post_line() -> None:
        store = b.state.store
        room, row = store.get_room("#build"), store.meeting_by_key(keys.key_hash(ALICE_KEY))
        assert room is not None and row is not None
        store.insert_message(
            room.id,
            sender_name="carol",
            sender_kind="meeting",
            via="meeting",
            text="a line",
            sender_host="alice-laptop",
            meeting_id=row.id,
        )

    assert call(admin, "build")["call"]["lines"] == 0
    b.on_loop(post_line)
    b.on_loop(post_line)
    assert call(admin, "build")["call"]["lines"] == 2
    assert call(bob, "uploads")["call"]["lines"] == 0  # another call's lines are not this one's


def test_ending_the_call_takes_it_out_of_the_details(team: tuple[InProcBroker, Browser, Browser]) -> None:
    """After End call the room has no call: ``call`` is null, and the page closes the details. Fails if a call
    that ended is still described."""
    b, admin, bob = team
    assert bob.post("/api/rooms/uploads/command", {"text": "/meeting leave"}).status_code == 200
    assert call(bob, "uploads") == {"room": "#uploads", "call": None}
    assert call(admin, "build")["call"] is not None  # the other room's is not touched


def test_only_a_signed_in_person_who_may_see_the_room_is_answered_and_only_to_a_get(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """Signed out is 401; a room that isn't there, and a private room the person is not in, are the same
    404 (the room's name is not confirmed, §44); other methods are refused. Fails if the route answers
    without a session, tells bob a private room exists, or changes anything for a POST."""
    b, admin, bob = team
    assert admin.post("/api/rooms", {"name": "#secret", "private": True}).status_code == 200
    out = httpx.Client(base_url=b.base)
    try:
        r = out.get("/api/rooms/build/call", headers={"Host": "sb.example.com"})
        assert r.status_code == 401
    finally:
        out.close()
    assert admin.get("/api/rooms/secret/call").json() == {"room": "#secret", "call": None}
    gone = bob.get("/api/rooms/secret/call")
    nothing = bob.get("/api/rooms/nothing-here/call")
    assert gone.status_code == nothing.status_code == 404
    assert gone.json()["message"].replace("#secret", "#x") == nothing.json()["message"].replace(
        "#nothing-here", "#x"
    )
    for method in ("post", "put", "delete"):
        r = getattr(bob.c, method)("/api/rooms/uploads/call", headers=bob.headers(True))
        assert r.status_code == 405, (method, r.status_code)
    assert call(bob, "uploads")["call"] is not None


def test_in_a_private_room_who_started_the_call_is_named_only_if_they_are_in_the_room(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """alice's private room has a call her laptop hosts; bob is added to the room, so he sees it, and she is
    in it, so she is named. A call that bob's machine hosts in a private room he is not in is not shown to
    anyone (he is not in the room). Fails if a name is carried into a room it does not belong to."""
    b, admin, bob = team
    assert admin.post("/api/rooms", {"name": "#vault", "private": True}).status_code == 200
    bob_id = b.state.store.person_named("bob").id
    plant(b, "#vault", "vault-laptop", bob_id, "sbc_" + "c3" * 32)  # bob hosts it, in a room he is not in
    seen_by_admin = call(admin, "vault")["call"]
    assert seen_by_admin["started_by"] is None and seen_by_admin["host"] == "vault-laptop"
    assert bob.get("/api/rooms/vault/call").status_code == 404
    added = admin.post("/api/rooms/vault/people", {"action": "add", "name": "bob"})
    assert added.status_code == 200, added.text
    assert call(bob, "vault")["call"]["started_by"] == "bob"
    assert call(admin, "vault")["call"]["started_by"] == "bob"


def test_a_desktop_broker_s_own_bot_call_may_be_ended_by_the_person_looking(
    broker: InProcBroker, web: httpx.Client
) -> None:
    """On a desktop broker the one person is the owner and an admin, and the broker's own bot's call has no
    owner check: End this call is theirs. The details carry the bot's side only: no machine, key, model or
    count of lines. Fails if the bot's call looks like a hosted one, or is refused to the person who may
    leave it."""
    headers = broker.write_headers()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=headers).status_code == 200

    def plant_bot() -> None:
        room = broker.state.store.get_room("#build")
        assert room is not None
        row = broker.state.store.insert_meeting(room.id, started_by="alice", platform="teams")
        broker.state.store.set_meeting_live(row.id)

    assert web.get("/api/rooms/build/call").json() == {"room": "#build", "call": None}
    broker.on_loop(plant_bot)
    d = web.get("/api/rooms/build/call").json()["call"]
    assert (d["source"], d["platform"], d["state"], d["started_by"], d["can_end"]) == (
        "server",
        "teams",
        "live",
        "alice",
        True,
    )
    assert not {"host", "fingerprint", "model", "lines"} & set(d)
