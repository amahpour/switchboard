"""Whose an agent is (#496, DESIGN.md §29.10): the member row names its owner.

A member's device is the one its agent token names (§49.1), so the owner is that token's person.
What a person is shown is their display name, never an email, and only where they can already see
the agent: a private room (§44) names an owner only if the owner is in it. An owner that can't be
resolved (no live token for the device, a person who is gone, a name that isn't a screen name) is
``None``, not a guess: the page and ``who`` then say nothing about it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard.broker.hub import Hub
from switchboard.broker.service import BrokerInfo, RoomService, member_dict
from switchboard.config import Config
from switchboard.models import Member
from switchboard.store import Store


@pytest.fixture
def world(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config(human_name="alice"))


@pytest.fixture
def svc(world: World, clock: FakeClock) -> RoomService:
    return RoomService(world.store, Hub(), world.cfg, BrokerInfo(port=7419, test_mode=True), clock)


def person(store: Store, name: str) -> int:
    handle = hashlib.sha256(name.encode()).digest()[:16]  # a person's handle is unique
    return store.person_add(name, handle, "hash", 3600).id


def token(store: Store, person_id: int | None, device: str) -> None:
    """A live agent token for ``device``, the person's (None: the owner's)."""
    assert store.agent_token_add(person_id, device, hashlib.sha256(device.encode()).hexdigest())


def owners(svc: RoomService, room_id: int) -> dict[str, str | None]:
    return {m.name: m.owner for m in svc.member_rows(room_id)}


def test_member_dict_names_the_owner() -> None:
    """The members frame, the members route and the Inspector's detail all start from
    ``member_dict``, so the owner has to be one of its keys: the page reads it from the frame.
    Fails if ``member_dict`` leaves it out, or invents one for a member with none."""
    base = dict(
        membership_id=1,
        participant_id=1,
        room_id=1,
        name="claude-1",
        harness="claude",
        status="idle",
        tier="claude:https",
        tier_note=None,
        away=None,
        approval_mode="prompting",
        held=False,
        joined_at=1.0,
        host="alices-pi",
    )
    assert member_dict(Member(**base, owner="alice"))["owner"] == "alice"  # type: ignore[arg-type]
    assert member_dict(Member(**base))["owner"] is None  # type: ignore[arg-type]


def test_an_agents_owner_is_the_person_its_devices_token_names(world: World, svc: RoomService) -> None:
    """The owner's token's device is the owner's (their display name, ``human_name``), bob's token's
    device is bob's. Fails if the owner isn't looked up through the device's token."""
    bob = person(world.store, "bob")
    token(world.store, None, "alices-pi")
    token(world.store, bob, "bobs-laptop")
    world.agent("claude-1", harness="claude", host="alices-pi")
    world.agent("codex-1", harness="codex", host="bobs-laptop")
    assert owners(svc, world.room.id) == {"claude-1": "alice", "codex-1": "bob"}


def test_an_owner_that_cant_be_resolved_shows_nothing(world: World, svc: RoomService) -> None:
    """Fail closed: no live token for the device (never made, or revoked), no device at all, a person
    who is gone, and a "name" that is an email each give ``None``, not the machine's owner that
    ``host_person`` falls back to for a private room's gate. Fails if any of them is guessed at."""
    store = world.store
    gone, mailish, revoked = person(store, "carol"), person(store, "dave"), person(store, "erin")
    token(store, gone, "carols-mac")
    token(store, mailish, "daves-mac")
    token(store, revoked, "erins-mac")
    world.agent("a-none", harness="claude", host="no-such-device")  # no token names this device
    world.agent("a-test", harness="test")  # no device (a scripted test agent)
    world.agent("a-gone", harness="claude", host="carols-mac")
    world.agent("a-mail", harness="claude", host="daves-mac")
    world.agent("a-revoked", harness="claude", host="erins-mac")
    store.con.execute("UPDATE people SET removed_at=1 WHERE id=?", (gone,))  # the person only
    store.con.execute("UPDATE people SET name='dave@example.com' WHERE id=?", (mailish,))
    store.con.execute("UPDATE agent_tokens SET revoked_at=1 WHERE device='erins-mac'")
    store.con.commit()
    assert svc.host_person("no-such-device") == 0  # what the gate does with it: the owner's
    assert owners(svc, world.room.id) == dict.fromkeys(("a-none", "a-test", "a-gone", "a-mail", "a-revoked"))


def test_a_private_room_names_an_owner_only_who_is_in_it(world: World, svc: RoomService) -> None:
    """A person sees an owner only where they can already see the agent (§44.2): in a private room
    that is its people. An agent whose person isn't in the room (left over from before the person
    was taken out) is named for nobody. Fails if a private room shows an owner who isn't in it."""
    bob = person(world.store, "bob")
    token(world.store, bob, "bobs-laptop")
    token(world.store, None, "alices-pi")
    room = world.store.create_room("#secret", "alice", 20, 6, private_for=bob)
    world.agent("codex-1", harness="codex", host="bobs-laptop", room_id=room.id)
    world.agent("claude-1", harness="claude", host="alices-pi", room_id=room.id)
    assert owners(svc, room.id) == {"codex-1": "bob", "claude-1": None}
    world.store.room_person_add(room.id, 0, bob)  # the owner is let in
    assert owners(svc, room.id) == {"codex-1": "bob", "claude-1": "alice"}


def test_the_owner_follows_a_rename(world: World, svc: RoomService) -> None:
    """The name shown is the person's name now: it is read when a row is built, not kept on the
    agent. Fails if a renamed person's agents keep the old name."""
    bob = person(world.store, "bob")
    token(world.store, bob, "bobs-laptop")
    world.agent("codex-1", harness="codex", host="bobs-laptop")
    world.store.person_rename(bob, "robert")
    assert owners(svc, world.room.id) == {"codex-1": "robert"}
