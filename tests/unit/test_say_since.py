"""Opt-in stale say validation (DESIGN.md §8.4)."""

from __future__ import annotations

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard.delivery import rules


def test_since_is_an_id_clamped_to_the_room_and_future_ids_are_refused() -> None:
    """A bad or future claim cannot bypass the stale check or overflow SQLite."""
    assert rules.since_id(0, 7) == 0
    assert rules.since_id(-9, 7) == 0
    assert rules.since_id(7, 7) == 7
    for bad in (True, "7", 7.0, 8, 2**100):
        with pytest.raises(ValueError):
            rules.since_id(bad, 7)


def test_newer_chat_uses_this_members_delivery_rows(tmp_path, clock: FakeClock) -> None:
    """Read chat still counts; pre-join, self, withheld and notice messages do not."""
    world = World(tmp_path, clock)
    world.human("before join")
    _p, member = world.agent("bot")
    _q, peer = world.agent("peer")
    seen = world.human("visible")
    world.store.mark_handled(member.id)
    other = world.agent_says(peer, "peer chat")
    world.agent_says(member, "self")
    world.store.insert_message(
        world.room.id, sender_name="alice", sender_kind="human", via="web",
        text="withheld", skip_memberships=(member.id,),
    )  # fmt: skip
    world.store.insert_message(
        world.room.id, sender_name="switchboard", sender_kind="system", via="system",
        text="joined", kind="notice",
    )  # fmt: skip
    assert [item.message_id for item in world.store.visible_items_since(member.id, 0)] == [seen.id, other.id]
    assert [item.message_id for item in world.store.visible_items_since(member.id, seen.id)] == [other.id]
