"""The agents overview's rules (#815, DESIGN.md §55.9): the four states, and when a worker is possibly stuck.

What these catch: a status this code does not know making an agent look busy; "stuck" set for an agent that is
idle, offline or has just been heard from (it must clear on the next sign of life); and the threshold drifting
from the 30 minutes at which who() calls a status line stale."""

from __future__ import annotations

import pytest
from conftest import FakeClock

from switchboard.broker import agents_view as v
from switchboard.store import STATUS_LINE_STALE_S


@pytest.mark.parametrize(
    ("status", "want"),
    [
        ("busy", "working"),
        ("idle", "idle"),
        ("starting", "idle"),
        ("waiting-approval", "waiting"),
        ("offline", "offline"),
        ("surprise", "idle"),  # a value we don't know is never "working"
        ("", "idle"),
    ],
)
def test_the_engines_status_becomes_one_of_four_states(status: str, want: str) -> None:
    """Fails if an unknown status can show an agent as working (and so as possibly stuck)."""
    assert v.state_of(status) == want


def test_the_stuck_threshold_is_the_one_who_uses_for_a_stale_status_line() -> None:
    """One number for 'stale': fails if the overview's drifts from the store's."""
    assert v.STUCK_S == STATUS_LINE_STALE_S == 30 * 60


def test_a_worker_is_possibly_stuck_after_the_threshold_and_the_mark_clears_on_the_next_event() -> None:
    """A FakeClock steps through it: 29 minutes of silence is fine, 30 is stuck, and any sign of life (the
    activity time moving to now) clears it. Fails if the mark needs more than a sign of life to go, or the
    boundary is off."""
    clock = FakeClock()
    active = clock.now()
    assert not v.possibly_stuck("working", active, clock.advance(29 * 60))
    assert v.possibly_stuck("working", active, clock.advance(60))  # exactly 30 minutes
    assert v.possibly_stuck("working", active, clock.advance(3600))
    active = clock.now()  # a tool call, a hook, a say
    assert not v.possibly_stuck("working", active, clock.now())


@pytest.mark.parametrize("state", ["idle", "waiting", "offline"])
def test_only_a_working_agent_can_be_possibly_stuck(state: str) -> None:
    """An idle agent has nothing to report, one waiting for approval waits on a person, and an offline one is
    shown as offline. Fails if any of them is marked."""
    assert not v.possibly_stuck(state, 0.0, 10_000_000.0)


def test_an_agent_that_left_no_sign_of_life_is_not_marked_stuck() -> None:
    """With nothing to measure from, the mark is not guessed. Fails if a missing time counts as old."""
    assert not v.possibly_stuck("working", None, 10_000_000.0)


def test_the_newest_sign_of_life_decides() -> None:
    """Fails if an older field wins over a newer one, or a non-number is read as a time."""
    t = {"last_seen": 100.0, "last_say_at": 300.0, "hooks_seen_at": 200.0, "status_at": 50.0}
    assert v.last_active(t, 250.0) == 300.0
    assert v.last_active({"last_seen": "x"}, None) is None
    assert v.last_active({}, 7.0) == 7.0
