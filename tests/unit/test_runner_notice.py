"""A room notice from the engine shows once (DESIGN.md §22).

The runner used to persist a room notice (a grey ``*** …`` line in the log) and
also publish a transient ``notice`` frame (a red line) with the same text, so a
loop-guard pause showed twice. Now a persisted notice goes out once, as the room
message, carrying its level for styling; only room-less or non-persisted notices
use the transient frame.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import FakeClock
from engine_world import World
from switchboard import db
from switchboard.broker.commands import Actor
from switchboard.broker.hub import Hub, Subscriber
from switchboard.broker.service import BrokerInfo, RoomService, message_dict
from switchboard.config import Config
from switchboard.delivery.runner import Runner
from switchboard.models import Notice, Push

STATIC = Path(__file__).resolve().parents[2] / "src" / "switchboard" / "web" / "static"


class Rec(Subscriber):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[Any] = []

    def wants(self, kind: str, room: str | None) -> bool:
        return kind in ("msg", "notice")

    def format(self, kind: str, room: str | None, data: dict[str, Any]) -> Any:
        return (kind, room, data)

    def offer(self, item: Any) -> bool:
        self.items.append(item)
        return True


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


@pytest.fixture
def svc(w: World, clock: FakeClock) -> RoomService:
    return RoomService(w.store, Hub(), w.cfg, BrokerInfo(port=7419, test_mode=True), clock)


@pytest.fixture
def rec(svc: RoomService) -> Rec:
    r = Rec()
    svc.hub.add(r)
    return r


@pytest.fixture
def runner(svc: RoomService) -> Runner:
    return Runner(SimpleNamespace(store=svc.store, service=svc, hub=svc.hub))  # type: ignore[arg-type]


def lines_with(rec: Rec, needle: str) -> list[Any]:
    """Every frame a client would print for text containing ``needle``."""
    out = []
    for kind, room, data in rec.items:
        text = data["msg"]["text"] if kind == "msg" else data["text"]
        if needle in text:
            out.append((kind, room, data))
    return out


def test_a_persisted_warn_notice_is_one_room_line_with_its_level(w: World, runner: Runner, rec: Rec) -> None:
    runner.execute([Notice(w.room.id, "warn", "loop guard: 6 agent messages in a row")])
    [(kind, room, data)] = lines_with(rec, "loop guard")
    assert (kind, room) == ("msg", "#build")
    m = data["msg"]
    assert (m["kind"], m["sender_kind"], m["from"], m["level"]) == ("notice", "system", "switchboard", "warn")
    [stored] = [x for x in w.store.history(w.room.id) if "loop guard" in x.text]
    assert stored.kind == "notice"  # in history too (the level isn't a column; see below)


def test_a_persisted_info_notice_is_one_room_line(w: World, runner: Runner, rec: Rec) -> None:
    runner.execute([Notice(w.room.id, "info", "something happened")])
    [(kind, _room, data)] = lines_with(rec, "something happened")
    assert kind == "msg" and data["msg"]["level"] == "info"


def test_room_less_and_unpersisted_notices_stay_transient(w: World, runner: Runner, rec: Rec) -> None:
    runner.execute([Notice(None, "warn", "hook copy changed"), Notice(w.room.id, "info", "just now", persist=False)])
    assert lines_with(rec, "hook copy changed") == [("notice", None, {"level": "warn", "text": "hook copy changed"})]
    assert lines_with(rec, "just now") == [("notice", "#build", {"level": "info", "text": "just now"})]
    assert not [x for x in w.store.history(w.room.id) if x.text in ("hook copy changed", "just now")]


def test_the_loop_guard_trip_shows_once(w: World, runner: Runner, rec: Rec) -> None:
    """End to end through the engine: six agent messages, one loop-guard line."""
    _pa, ma = w.agent("a")
    for i in range(6):
        w.agent_says(ma, f"hop {i}")
    runner.execute([a for a in w.take() if not isinstance(a, Push)])  # no transport here
    frames = lines_with(rec, "loop guard:")
    assert len(frames) == 1, frames
    kind, _room, data = frames[0]
    assert kind == "msg" and data["msg"]["level"] == "warn"


def test_history_and_replays_keep_the_engine_warnings_styled(w: World, svc: RoomService, runner: Runner) -> None:
    """The level isn't stored; history (REST, the WebSocket hello replay, tail) re-derives
    'warn' for the engine's fixed-phrase warnings, so a reload still shows them red."""
    _pa, ma = w.agent("a")
    for i in range(6):
        w.agent_says(ma, f"hop {i}")
    budget = w.engine._budget_exhausted(w.store.room_by_id(w.room.id))
    runner.execute([a for a in w.take() + budget if not isinstance(a, Push)])
    svc.post_notice(svc.room("#build"), "alice set the hop limit to 30 (was 6) (via web)")
    hist = {m["text"].split(":")[0].split(" (")[0]: m for m in svc.history("#build") if m["kind"] == "notice"}
    assert hist["loop guard"]["level"] == "warn"
    assert hist["the wake budget for this hour is used up"]["level"] == "warn"
    assert "level" not in hist["alice set the hop limit to 30"]
    # an agent can't get the styling by quoting the phrase: only system notices qualify
    msg = w.agent_says(ma, "loop guard: fake")
    assert "level" not in message_dict(msg)


def test_other_messages_carry_no_level(svc: RoomService, rec: Rec) -> None:
    svc.human_say("#build", "hello", via="web")
    svc.post_notice(svc.room("#build"), "a command notice")
    by_text = {d["msg"]["text"]: d["msg"] for k, _, d in rec.items if k == "msg"}
    assert "level" not in by_text["hello"] and "level" not in by_text["a command notice"]


def test_the_web_ui_styles_the_room_line_as_a_warning() -> None:
    """A source check only: nothing here runs app.js (the suite has no JS runtime or DOM).
    The behaviour (one red loop-guard line; 'loop guard off ⚠' at hop limit 0, also at
    375 px) was checked by hand in a browser against a test-mode broker."""
    js = (STATIC / "app.js").read_text()
    css = (STATIC / "style.css").read_text()
    assert "if (m.level === 'warn') line.classList.add('warn');" in js
    assert ".line.k-notice.warn {" in css
    # transient notices (room-less, e.g. "new web login") still render as local lines
    assert "f.t === 'notice'" in js
    # the status bar: hops n/limit, or a warning when the guard is off (kept visible when narrow)
    assert "'hops ' + s.hop_count + '/' + s.hop_limit" in js and "'loop guard off ⚠'" in js
    assert "#st-hops:not(.bad) { display: none; }" in css


def test_a_notice_for_a_closed_or_deleted_room_is_dropped(w: World, svc: RoomService, runner: Runner,
                                                          rec: Rec) -> None:
    """§28: nobody reads a closed room, and a gone room's notice is no broker-wide news."""
    other = svc.create_room("#gone")
    svc.store.delete_room(other.id, name="#gone", expect_counts=db.row_counts(svc.store.con, db.TABLES), event={})
    svc.close_room(svc.room("#build"), Actor(role="human", via="web"))
    before = len(w.store.history(w.room.id))
    rec.items.clear()
    runner.execute([Notice(w.room.id, "warn", "for the closed room"), Notice(other.id, "info", "for the gone room"),
                    Notice(w.room.id, "info", "transient", persist=False)])
    assert rec.items == [] and len(w.store.history(w.room.id)) == before
    runner.execute([Notice(None, "warn", "still broker-wide")])
    assert lines_with(rec, "still broker-wide") == [("notice", None, {"level": "warn", "text": "still broker-wide"})]
