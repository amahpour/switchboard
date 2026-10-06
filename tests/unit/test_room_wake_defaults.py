"""Per-person default wake budget and hop limit, and a room's own rate (#131, DESIGN.md §42).

Precedence for a new room: the creator's own default (Settings) if set, else ``[delivery]``
in config.toml, else the built-in default. Copy on create: later edits to a person's defaults
never change a room already made, and editing a room never changes that person's defaults
(the same model #105 uses for custom room rules). ``RoomService.set_room_wake`` is what the
room's own settings dialog calls: it can change the rate ``/budget <n>`` never touches, and
writes the same ``budget_set`` / ``hop_limit_set`` notices and events the commands do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.broker.hub import Hub
from switchboard.broker.service import BrokerInfo, RoomService
from switchboard.config import Config, DeliveryCfg
from switchboard.store import Store


@pytest.fixture
def svc(tmp_path: Path, clock: FakeClock) -> RoomService:
    store = Store(db.open_db(tmp_path / "y.db"), clock)
    return RoomService(store, Hub(), Config(), BrokerInfo(port=7419, test_mode=True), clock)


def notices(svc: RoomService, room: str = "#build") -> list[str]:
    return [m.text for m in svc.store.history(svc.room(room).id) if m.kind == "notice"]


def events(svc: RoomService, kind: str) -> list[dict[str, Any]]:
    return [e.data for e in svc.store.recent_events(kinds=[kind])]


def test_a_new_room_falls_back_to_the_built_in_default_with_nothing_set(svc: RoomService) -> None:
    """Nobody has a default and config.toml says nothing: the built-in numbers apply, and
    Settings would say so."""
    defaults = svc.wake_defaults(None)
    assert defaults["budget_per_hour"] == {"value": 60, "source": "built-in default"}
    assert defaults["hop_limit"] == {"value": 6, "source": "built-in default"}
    room = svc.create_room("#build")
    assert (room.budget_per_hour, room.hop_limit) == (60, 6)


def test_configtoml_wins_over_the_built_in_default(tmp_path: Path, clock: FakeClock) -> None:
    """A broker's own ``[delivery]`` values apply until the person sets their own."""
    cfg = Config(delivery=DeliveryCfg(budget_per_hour=300, hop_limit=30))
    store = Store(db.open_db(tmp_path / "y.db"), clock)
    svc = RoomService(store, Hub(), cfg, BrokerInfo(port=7419, test_mode=True), clock)
    defaults = svc.wake_defaults(None)
    assert defaults["budget_per_hour"] == {"value": 300, "source": "config.toml"}
    assert defaults["hop_limit"] == {"value": 30, "source": "config.toml"}
    room = svc.create_room("#build")
    assert (room.budget_per_hour, room.hop_limit) == (300, 30)


def test_your_own_default_wins_over_configtoml_and_the_built_in_one(tmp_path: Path, clock: FakeClock) -> None:
    """Setting your own default in Settings beats both config.toml and the built-in number."""
    cfg = Config(delivery=DeliveryCfg(budget_per_hour=300, hop_limit=30))
    store = Store(db.open_db(tmp_path / "y.db"), clock)
    svc = RoomService(store, Hub(), cfg, BrokerInfo(port=7419, test_mode=True), clock)
    store.set_preferences(None, {"budget_per_hour": 90, "hop_limit": 12})
    defaults = svc.wake_defaults(None)
    assert defaults["budget_per_hour"] == {"value": 90, "source": "your default"}
    assert defaults["hop_limit"] == {"value": 12, "source": "your default"}
    room = svc.create_room("#build")
    assert (room.budget_per_hour, room.hop_limit) == (90, 12)


def test_copy_on_create_a_later_default_edit_never_changes_an_existing_room(svc: RoomService) -> None:
    """Like #105's room rules: a room keeps the numbers it was made with, and a room edit
    never writes back to the person's own defaults."""
    svc.store.set_preferences(None, {"budget_per_hour": 90, "hop_limit": 12})
    first = svc.create_room("#first")
    assert (first.budget_per_hour, first.hop_limit) == (90, 12)
    svc.store.set_preferences(None, {"budget_per_hour": 500, "hop_limit": 40})
    assert svc.store.get_room("#first").budget_per_hour == 90  # unaffected by the later default edit
    second = svc.create_room("#second")
    assert (second.budget_per_hour, second.hop_limit) == (500, 40)  # the new default, not #first's
    svc.set_room_wake("#first", budget_per_hour=15, hop_limit=2, actor="alice")
    assert svc.store.preferences(None)["budget_per_hour"] == 500  # the room edit didn't touch it
    assert svc.store.preferences(None)["hop_limit"] == 40


def test_set_room_wake_sets_the_rate_and_what_is_left_this_hour_together(svc: RoomService) -> None:
    """The room dialog's one save sets ``budget_per_hour`` (the rate ``/budget n`` can't reach)
    and ``budget_remaining`` together, so it takes effect immediately, not at the next refill."""
    svc.create_room("#build")
    room = svc.room("#build")
    assert (room.budget_per_hour, room.budget_remaining) == (60, 60)
    room = svc.set_room_wake("#build", budget_per_hour=120, hop_limit=6, actor="alice")
    assert (room.budget_per_hour, room.budget_remaining) == (120, 120)
    assert "alice set the wake budget to 120/hour (was 60/hour)" in notices(svc)
    assert events(svc, "budget_set")[-1] == {"old": 60, "new": 120}


def test_set_room_wake_writes_the_same_hop_limit_notice_the_hops_command_writes(svc: RoomService) -> None:
    """Reuses ``commands.hop_limit_notice`` rather than restating the wording for each case."""
    svc.create_room("#build")
    svc.set_room_wake("#build", budget_per_hour=60, hop_limit=0, actor="alice")
    assert "alice turned the loop guard off (hop limit was 6)" in notices(svc)
    svc.set_room_wake("#build", budget_per_hour=60, hop_limit=10, actor="alice")
    assert "alice turned the loop guard on with a hop limit of 10" in notices(svc)
    # recent_events() is newest first
    assert events(svc, "hop_limit_set")[:2] == [{"old": 0, "new": 10}, {"old": 6, "new": 0}]


def test_set_room_wake_is_a_no_op_when_nothing_changed(svc: RoomService) -> None:
    """Saving the dialog with the same numbers posts nothing new and writes no event."""
    svc.create_room("#build")
    before = notices(svc)
    svc.set_room_wake("#build", budget_per_hour=60, hop_limit=6, actor="alice")
    assert notices(svc) == before
    assert events(svc, "budget_set") == []
    assert events(svc, "hop_limit_set") == []
