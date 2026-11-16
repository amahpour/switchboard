"""The board's independent burst budget (DESIGN.md §56.3, #750), stepped by a FakeClock."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import FakeClock

from switchboard.broker.room_board import RoomBoard
from switchboard.delivery import rules


def test_five_changes_then_a_wait_without_spending_on_refusal(clock: FakeClock) -> None:
    """A short editing burst passes; the sixth waits two seconds and retries do not move the deadline."""
    full_at = None
    for _ in range(5):
        full_at, retry = rules.board_budget(full_at, clock.now())
        assert retry is None
    after, retry = rules.board_budget(full_at, clock.now())
    assert after == full_at and retry == 2.0
    clock.advance(0.5)
    assert rules.board_budget(full_at, clock.now()) == (full_at, 1.5)
    clock.advance(1.5)
    full_at, retry = rules.board_budget(full_at, clock.now())
    assert retry is None
    assert rules.board_budget(full_at, clock.now()) == (full_at, 2.0)


def test_idle_refills_only_five_changes(clock: FakeClock) -> None:
    """An idle agent regains its full burst, but cannot bank an unbounded allowance."""
    full_at, _ = rules.board_budget(None, clock.now())
    clock.advance(100)
    for _ in range(5):
        full_at, retry = rules.board_budget(full_at, clock.now())
        assert retry is None
    assert rules.board_budget(full_at, clock.now()) == (full_at, 2.0)


def test_fractional_refill_never_admits_a_change_early(clock: FakeClock) -> None:
    """A refusal keeps a positive wait even just before the boundary; backward time adds no capacity."""
    full_at = clock.now() + 10
    clock.advance(1.99)
    assert rules.board_budget(full_at, clock.now())[1] == pytest.approx(0.01)
    clock.advance(-2)
    assert rules.board_budget(full_at, clock.now())[1] == pytest.approx(2.01)


def test_old_budget_entries_are_pruned_without_resetting_active_agents(clock: FakeClock) -> None:
    """Long-lived brokers forget fully refilled sessions while preserving outstanding debt."""
    board = RoomBoard(SimpleNamespace(clock=clock))
    board._budget_full_at = dict.fromkeys(range(1000), clock.now())
    board._budget_full_at[1001] = clock.now() + 10
    assert board.check_budget(1001) == 2.0
    assert board._budget_full_at == {1001: clock.now() + 10}
    assert board.check_budget(0) is None
