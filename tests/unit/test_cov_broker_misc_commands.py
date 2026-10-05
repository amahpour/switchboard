"""commands: the corners the main command tests leave out (DESIGN.md §10).

- ``parse_command`` refuses anything that is not text (the web body is JSON, so a
  number or a list can reach it);
- ``/hops`` on a room the loop guard paused, once the limit is above the count, says
  the room is still paused but not that /resume resets the count;
- ``/release`` of a member that is not held is a no-op that says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from test_store import add_agent

from switchboard import db
from switchboard.broker.commands import Actor, CommandError, parse_command
from switchboard.broker.hub import Hub
from switchboard.broker.service import BrokerInfo, RoomService
from switchboard.config import Config
from switchboard.store import Store

WEB = Actor(role="human", via="web")
CLI = Actor(role="human_cli", via="cli", chain="zsh")


@pytest.fixture
def svc(tmp_path: Path, clock: FakeClock) -> RoomService:
    store = Store(db.open_db(tmp_path / "y.db"), clock)
    s = RoomService(store, Hub(), Config(), BrokerInfo(port=7419, test_mode=True), clock)
    s.create_room("#build")  # hop limit 6, the default
    return s


def cmd(svc: RoomService, text: str, actor: Actor = WEB) -> str:
    res = svc.command("#build", text, actor)
    assert res["ok"], res
    return res["text"]


def agent_msgs(svc: RoomService, n: int) -> None:
    room = svc.room("#build")
    for i in range(n):
        svc.store.insert_message(room.id, sender_name="a", sender_kind="agent", via="mcp", text=f"hop {i}")


@pytest.mark.parametrize("value", [None, 7, ["/pause"], {"text": "/pause"}, b"/pause"])
def test_a_command_that_is_not_text_is_a_bad_request(value: object) -> None:
    with pytest.raises(CommandError) as e:
        parse_command(value)  # type: ignore[arg-type]
    assert e.value.code == "bad_request"
    assert e.value.message == "command must be text"


def test_hops_on_a_loop_guard_pause_below_the_new_limit(svc: RoomService) -> None:
    room = svc.room("#build")
    agent_msgs(svc, 7)
    svc.store.set_paused(room.id, True, "loop guard")
    # at or over the limit: /resume is the way out, and it resets the count
    assert cmd(svc, "/hops", CLI).endswith(
        "still paused by the loop guard: /resume to continue (it resets the count)"
    )
    cmd(svc, "/hops 30")
    # now under the limit: still paused, and nothing to say about the count
    text = cmd(svc, "/hops", CLI)
    assert text == (
        "#build: hops 7/30 (the room pauses after 30 agent messages in a row with none from alice)"
        "; still paused by the loop guard: /resume to continue"
    )
    # setting the same limit again reports the same state and changes nothing
    same = cmd(svc, "/hops 30", CLI)
    assert same.endswith("; still paused by the loop guard: /resume to continue")
    room = svc.room("#build")
    assert room.paused and room.paused_reason == "loop guard" and room.hop_limit == 30 and room.hop_count == 7


def test_hops_with_the_guard_off_on_a_loop_guard_pause(svc: RoomService) -> None:
    room = svc.room("#build")
    agent_msgs(svc, 7)
    svc.store.set_paused(room.id, True, "loop guard")
    cmd(svc, "/hops 0")
    text = cmd(svc, "/hops", CLI)
    assert text.startswith("#build: hops 7, loop guard off (agents may message each other without limit;")
    assert text.endswith("; still paused by the loop guard: /resume to continue")


def test_release_of_a_member_that_is_not_held(svc: RoomService) -> None:
    room = svc.room("#build")
    add_agent(svc.store, room.id, "claude-1")
    before = len(svc.store.recent_events(room_id=room.id, kinds=["release"]))
    res = svc.command("#build", "/release claude-1", WEB)
    assert res["ok"] and res["text"] == "claude-1 is not held"
    # a no-op: no event, no notice, still not held
    assert len(svc.store.recent_events(room_id=room.id, kinds=["release"])) == before
    assert not [m for m in svc.store.history(room.id) if m.kind == "notice" and "released" in m.text]
    assert svc.store.find_member(room.id, "claude-1").held is False
