"""Whose an agent is (#496, DESIGN.md §29.10), on a hosted broker with alice (the admin) and bob.

Every agent over HTTPS has a person's agent token, so the broker knows whose it is. The members route,
the Inspector's detail and the other agents' ``who`` name that person, by display name; a private
room names only a person in it, and an agent whose owner can't be worked out is named for nobody.
The agents are Codex sessions on two devices, one per person's token, as ``test_agents_http.py`` drives them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_agents_http import THREAD_A, THREAD_B, Codex, make_token
from test_passkeys import HOST, Browser, hosted_broker, stop
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


class Room:
    """A hosted broker with alice and bob, a room, and an agent of each: ``codex-1`` on alice's device
    and ``codex-2`` on bob's, both joined."""

    def __init__(self, hosted: InProcBroker, room: str = "#build", private: bool = False) -> None:
        self.b = hosted
        self.room = room
        self.admin = set_up(hosted)
        self.bob = join_as_bob(hosted, self.admin)
        made = (self.bob if private else self.admin).post("/api/rooms", {"name": room, "private": private})
        assert made.status_code == 200, made.text
        self.bob_id = hosted.state.store.person_named("bob").id
        self.alices = self.codex(make_token(hosted, "alices-pi", None))
        self.bobs = self.codex(make_token(hosted, "bobs-laptop", self.bob_id))
        assert self.alices.start().status_code == 200 and self.bobs.start().status_code == 200
        if not private:
            self.join(self.alices, THREAD_A, "codex-1")
        self.join(self.bobs, THREAD_B, "codex-2")

    def codex(self, token: str) -> Codex:
        """A Codex client at the broker's public host."""
        c = Codex(self.b, token)
        c.c = httpx.Client(base_url=f"http://127.0.0.1:{self.b.port}", timeout=30.0, headers={"host": HOST})
        return c

    def join(self, client: Codex, thread: str, name: str) -> None:
        assert client.call("join", thread, room=self.room, screen_name=name)["ok"]

    def who(self, client: Codex, thread: str) -> dict[str, str]:
        """``who``'s agent lines by agent name."""
        text = client.call("who", thread, room=self.room)["text"]
        return {line.split()[1]: line for line in text.splitlines() if line.startswith("- ")}

    def members(self, who: Browser) -> list[dict[str, Any]]:
        r = who.get(f"/api/rooms/{self.room.lstrip('#')}/members")
        assert r.status_code == 200, r.text
        return list(r.json()["members"])

    def close(self) -> None:
        self.alices.c.close()
        self.bobs.c.close()


def field(line: str, key: str) -> str | None:
    """``key=value`` out of a ``who`` line, or None."""
    return next((w.split("=", 1)[1] for w in line.split() if w.startswith(key + "=")), None)


def test_who_names_each_agents_owner_and_nobody_it_cant(hosted: InProcBroker) -> None:
    """``who`` says whose each agent is, right after its device, so an agent can tell whose agent it
    is talking to (#472): alice's and bob's, by display name. An agent whose device no live token
    names any more (its token was revoked and the membership is still there) has no ``owner=`` at
    all: nothing is guessed. Fails if ``who`` leaves the owner out, names the wrong person, or names
    one for a device no token vouches for."""
    r = Room(hosted)
    try:
        lines = r.who(r.bobs, THREAD_B)
        assert {n: field(line, "owner") for n, line in lines.items()} == {
            "codex-1": "alice",
            "codex-2": "bob",
        }
        assert "host=alices-pi owner=alice status=" in lines["codex-1"]  # right after the device
        store = hosted.state.store
        token = store.agent_token_for_device("bobs-laptop")
        assert token is not None and hosted.on_loop(store.agent_token_revoke_id, token.id)
        lines = r.who(r.alices, THREAD_A)
        assert field(lines["codex-2"], "host") == "bobs-laptop" and field(lines["codex-2"], "owner") is None
        assert field(lines["codex-1"], "owner") == "alice"
    finally:
        r.close()


def test_the_members_route_and_the_inspector_carry_the_owner(hosted: InProcBroker) -> None:
    """What the page reads, the members route and the Inspector's detail, names the owner by display
    name and never an email (bob signs in with ``bob@example.com``). Fails if either leaves it out, or
    shows an email for it."""
    r = Room(hosted)
    try:
        for person in (r.admin, r.bob):  # everyone sees the same
            assert {m["name"]: m["owner"] for m in r.members(person)} == {
                "codex-1": "alice",
                "codex-2": "bob",
            }
        detail = r.admin.get("/api/rooms/build/members/codex-2").json()
        assert detail["member"]["owner"] == "bob"
        assert "@" not in json.dumps([m["owner"] for m in r.members(r.admin)])
    finally:
        r.close()


def test_a_private_room_names_its_owners_only_to_its_people(hosted: InProcBroker) -> None:
    """Private rooms (§44): a person who is not in one is told nothing of it, so they see none of its
    agents' owners; its people see them, and so does a person once they are let in. Fails if a
    non-member's route answers with an owner, or if an owner in the room isn't named to its people."""
    r = Room(hosted, "#secret", private=True)
    try:
        for path in ("/api/rooms/secret/members", "/api/rooms/secret/members/codex-2"):
            nope = r.admin.get(path)
            assert nope.status_code == 404 and "bob" not in nope.text and "codex-2" not in nope.text
        assert [(m["name"], m["owner"]) for m in r.members(r.bob)] == [("codex-2", "bob")]
        added = r.bob.post("/api/rooms/secret/people", {"action": "add", "name": "alice"})
        assert added.status_code == 200, added.text
        assert [(m["name"], m["owner"]) for m in r.members(r.admin)] == [("codex-2", "bob")]
    finally:
        r.close()


def test_a_rename_reaches_every_open_page(hosted: InProcBroker) -> None:
    """Pages read an owner from the members frame, which nothing else sends when a person renames
    themselves (a stray engine snapshot may, by luck), so a rename publishes it again for every open
    room, and the frame names the new name. Fails if a rename leaves a room's members unpublished:
    bob's agent would stay ``bob``'s on a page that is open when he becomes robert."""
    r = Room(hosted)
    try:
        assert r.admin.post("/api/rooms", {"name": "#ops"}).status_code == 200
        state = hosted.state
        sub = WsSubscriber()
        sub.person = 0
        assert hosted.on_loop(_ws_hello, state, sub, {"t": "hello", "rooms": ["#build"], "after": {}})
        hosted.on_loop(state.hub.add, sub)
        published: list[str] = []
        real = state.hub.members_changed

        def record(room: str) -> None:
            published.append(room)
            real(room)

        state.hub.members_changed = record  # type: ignore[method-assign]
        renamed = r.bob.post("/api/me/name", {"name": "robert"})
        assert renamed.status_code == 200, renamed.text
        assert {"#build", "#ops"} <= set(published), published
        deadline = time.monotonic() + 10
        owners: dict[str, Any] = {}
        while time.monotonic() < deadline and owners.get("codex-2") != "robert":
            while not sub.q.empty():
                frame = sub.q.get_nowait()
                if frame and frame.get("t") == "members" and frame.get("room") == "#build":
                    owners = {m["name"]: m["owner"] for m in frame["members"]}
            time.sleep(0.02)
        assert owners == {"codex-1": "alice", "codex-2": "robert"}
    finally:
        r.close()
