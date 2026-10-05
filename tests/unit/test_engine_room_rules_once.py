"""Room rules fit whole in a delivery or wait for a larger one (DESIGN.md §35)."""

from __future__ import annotations

from pathlib import Path

from conftest import FakeClock
from engine_world import World

from switchboard.config import Config


def test_rules_wait_for_a_batch_that_can_fit_them(tmp_path: Path, clock: FakeClock) -> None:
    """A short context never cuts the guidance or marks it seen."""
    small = Config(human_name="alice").with_delivery(pull_max_chars=700)
    world = World(tmp_path, clock, small)
    rules = "R" * 2000
    world.store.set_room_rules(world.room.id, rules)
    participant, member = world.agent("bot")
    world.human("First task")

    short, *_ = world.engine.pull(world.p(participant), world.m(member), "read", 20)
    assert rules not in short
    assert world.m(member).rules_seen < world.store.get_room("#build").rules_version

    world.engine.cfg = Config(human_name="alice").with_delivery(pull_max_chars=6000)
    world.human("Second task")
    full, *_ = world.engine.pull(world.p(participant), world.m(member), "read", 20)
    assert rules in full
    assert world.m(member).rules_seen == world.store.get_room("#build").rules_version

    world.human("Third task")
    later, *_ = world.engine.pull(world.p(participant), world.m(member), "read", 20)
    assert rules not in later
