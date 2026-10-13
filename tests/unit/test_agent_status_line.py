"""An agent's one-line update is shared by its memberships and ages on the broker clock (#292)."""

from __future__ import annotations

from pathlib import Path

from conftest import FakeClock
from engine_world import World

from switchboard.config import Config
from switchboard.store import STATUS_LINE_STALE_S


def test_status_line_becomes_stale_at_the_boundary(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock, Config())
    p, _m = w.agent("worker")
    w.store.update_participant(p.id, status_line="CI 31/39 green", status_line_at=clock.now())

    def row():
        return w.store.members(w.room.id)[0]

    assert row().status_line == "CI 31/39 green"
    assert not row().status_line_stale
    assert row().status_line_remaining_s == STATUS_LINE_STALE_S
    clock.advance(STATUS_LINE_STALE_S - 1)
    assert not row().status_line_stale
    assert row().status_line_remaining_s == 1
    clock.advance(1)
    assert row().status_line_stale
    assert row().status_line_remaining_s == 0
    w.store.update_participant(p.id, status_line_at=clock.now())
    assert not row().status_line_stale
    assert row().status_line_remaining_s == STATUS_LINE_STALE_S


def test_missing_or_cleared_status_has_no_stale_marker(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock, Config())
    p, _m = w.agent("worker")
    assert w.store.members(w.room.id)[0].status_line is None
    w.store.update_participant(p.id, status_line="done", status_line_at=clock.now())
    w.store.update_participant(p.id, status_line=None, status_line_at=None)
    clock.advance(STATUS_LINE_STALE_S + 1)
    row = w.store.members(w.room.id)[0]
    assert row.status_line is None
    assert not row.status_line_stale
    assert row.status_line_remaining_s is None
