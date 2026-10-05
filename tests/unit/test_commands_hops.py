"""/hops: show or set a room's loop-guard limit live (DESIGN.md §10, §8.5).

``/hops`` shows the count and the limit. ``/hops <n>`` (0..1000, 0 = guard off) sets
the room's ``hop_limit``. Raising it or turning the guard off raises activity, so it
needs the web session; lowering it (or turning the guard back on) works from the CLI.
A new limit never un-pauses a room: the human still uses /resume.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.broker.commands import (
    HELP_TEXT,
    MAX_BUDGET,
    MAX_HOPS,
    Actor,
    CommandError,
    apply,
    hops_raise,
    parse_command,
    required_role,
)
from switchboard.broker.hub import Hub, Subscriber
from switchboard.broker.service import BrokerInfo, RoomService, ServiceError
from switchboard.config import Config
from switchboard.store import Store

WEB = Actor(role="human", via="web")
CLI = Actor(role="human_cli", via="cli", chain="zsh ← Terminal")


class Rec(Subscriber):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[Any] = []

    def wants(self, kind: str, room: str | None) -> bool:
        return True

    def format(self, kind: str, room: str | None, data: dict[str, Any]) -> Any:
        return (kind, room, data)

    def offer(self, item: Any) -> bool:
        self.items.append(item)
        return True


@pytest.fixture
def svc(tmp_path: Path, clock: FakeClock) -> RoomService:
    store = Store(db.open_db(tmp_path / "y.db"), clock)
    s = RoomService(store, Hub(), Config(), BrokerInfo(port=7419, test_mode=True), clock)
    s.create_room("#build")  # hop limit 6, the default
    return s


@pytest.fixture
def rec(svc: RoomService) -> Rec:
    r = Rec()
    svc.hub.add(r)
    return r


def notices(svc: RoomService) -> list[str]:
    return [m.text for m in svc.store.history(svc.room("#build").id) if m.kind == "notice"]


def hop_events(svc: RoomService) -> list[dict[str, Any]]:
    return [e.data for e in svc.store.recent_events(kinds=["hop_limit_set"])]


def agent_msgs(svc: RoomService, n: int) -> None:
    room = svc.room("#build")
    for i in range(n):
        svc.store.insert_message(room.id, sender_name="a", sender_kind="agent", via="mcp", text=f"hop {i}")


def cmd(svc: RoomService, text: str, actor: Actor = WEB) -> str:
    res = svc.command("#build", text, actor)
    assert res["ok"], res
    return res["text"]


def forbidden(svc: RoomService, text: str, actor: Actor = CLI) -> str:
    with pytest.raises(ServiceError) as e:
        svc.command("#build", text, actor)
    assert e.value.code == "forbidden", e.value.message
    return e.value.message


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    "text,args",
    [
        ("/hops", ()),
        ("/HOPS 30", ("30",)),
        ("/hops 0", ("0",)),
        ("/hops 007", ("7",)),
        (f"/hops {MAX_HOPS}", (str(MAX_HOPS),)),
        ("  /hops   12 ", ("12",)),
    ],
)
def test_parse(text: str, args: tuple[str, ...]) -> None:
    c = parse_command(text)
    assert (c.name, c.args) == ("hops", args)


@pytest.mark.parametrize(
    "text",
    [
        "/hops -1",
        f"/hops {MAX_HOPS + 1}",
        "/hops x",
        "/hops 1 2",
        "/hops 1.5",
        "/hops +5",
        "/hops 1_0",
        "/hops ²",
        "/hops ３",
        "/hops 1e3",
        "/hops 99999999999999999999",
    ],
)
def test_parse_errors(text: str) -> None:
    with pytest.raises(CommandError) as e:
        parse_command(text)
    assert e.value.code == "bad_request"
    assert "/hops" in e.value.message


@pytest.mark.parametrize("name,top", [("hops", MAX_HOPS), ("budget", MAX_BUDGET)])
def test_huge_numbers_are_a_bad_request_not_a_crash(svc: RoomService, name: str, top: int) -> None:
    """int() refuses strings over 4300 digits with a ValueError; that must not escape as a 500."""
    for text in (f"/{name} " + "9" * 5000, f"/{name} 1" + "0" * 4400):
        with pytest.raises(CommandError) as e:
            parse_command(text)
        assert (e.value.code, e.value.message) == ("bad_request", f"/{name} must be between 0 and {top}")
        with pytest.raises(ServiceError) as se:
            svc.command("#build", text, WEB)
        assert se.value.code == "bad_request"
    # leading zeros don't count towards the length
    assert parse_command(f"/{name} " + "0" * 5000 + "7").args == ("7",)
    assert parse_command(f"/{name} " + "0" * 5000).args == ("0",)


def test_bounds_through_the_service(svc: RoomService) -> None:
    for text in ("/hops 1001", "/hops -3", "/hops lots"):
        with pytest.raises(ServiceError) as e:
            svc.command("#build", text, WEB)
        assert e.value.code == "bad_request" and "0 and 1000" in e.value.message
    assert svc.room("#build").hop_limit == 6 and hop_events(svc) == []
    cmd(svc, "/hops 1000")
    assert svc.room("#build").hop_limit == 1000


# ------------------------------------------------------------------ roles
def test_hops_raise() -> None:
    assert not hops_raise(6, 6) and not hops_raise(0, 0)
    assert hops_raise(7, 6) and hops_raise(1000, 6)
    assert not hops_raise(5, 6) and not hops_raise(1, 6)
    assert hops_raise(0, 6)  # no limit at all
    assert not hops_raise(30, 0) and not hops_raise(1, 0)  # turning the guard back on


def test_required_roles(svc: RoomService) -> None:
    room = svc.room("#build")  # limit 6
    need = {
        t: required_role(parse_command(t), room)
        for t in ["/hops", "/hops 6", "/hops 5", "/hops 1", "/hops 7", "/hops 1000", "/hops 0"]
    }
    assert need == {
        "/hops": "human_cli",
        "/hops 6": "human_cli",
        "/hops 5": "human_cli",
        "/hops 1": "human_cli",
        "/hops 7": "human",
        "/hops 1000": "human",
        "/hops 0": "human",
    }
    room = svc.store.set_hop_limit(room.id, 0)
    need = {t: required_role(parse_command(t), room) for t in ["/hops", "/hops 0", "/hops 1000"]}
    assert need == {"/hops": "human_cli", "/hops 0": "human_cli", "/hops 1000": "human_cli"}


def test_the_cli_may_lower_it_but_not_raise_it_or_turn_it_off(svc: RoomService) -> None:
    msg = forbidden(svc, "/hops 7")
    assert msg == "raising the hop limit needs your web session: type it in the switchboard web UI"
    msg = forbidden(svc, "/hops 0")
    assert msg == "turning the loop guard off needs your web session: type it in the switchboard web UI"
    assert (
        svc.room("#build").hop_limit == 6
        and hop_events(svc) == []
        and notices(svc) == ["#build created by alice"]
    )
    assert cmd(svc, "/hops 4", CLI) == "#build: hop limit set to 4 (was 6); hops 0/4"
    assert svc.room("#build").hop_limit == 4
    assert hop_events(svc) == [{"via": "cli", "old": 6, "new": 4}]
    assert "alice set the hop limit to 4 (was 6) (via cli: zsh ← Terminal)" in notices(svc)
    # the same value again changes nothing (and is not a raise)
    assert cmd(svc, "/hops 4", CLI) == "#build: the hop limit is already 4; hops 0/4"
    assert len(hop_events(svc)) == 1
    # back up to 6 is a raise now
    forbidden(svc, "/hops 6")
    assert cmd(svc, "/hops 6", WEB).startswith("#build: hop limit set to 6 (was 4)")


def test_apply_refuses_a_low_role_cleanly(svc: RoomService) -> None:
    """Defence in depth: apply() re-checks the role, with a clean 'forbidden' for every form."""
    anon = Actor(role="anon", via="cli")
    room = svc.room("#build")
    for text, what in (
        ("/hops", "/hops"),
        ("/hops 0", "turning the loop guard off"),
        ("/hops 7", "raising the hop limit"),
        ("/hops 3", "/hops"),
    ):
        with pytest.raises(CommandError) as e:
            apply(parse_command(text), room, anon, svc)
        assert e.value.code == "forbidden" and e.value.message.startswith(what + " needs your web session")
    assert svc.room("#build").hop_limit == 6 and hop_events(svc) == []


# ---------------------------------------------------------------- effects
def test_show(svc: RoomService) -> None:
    assert cmd(svc, "/hops", CLI) == (
        "#build: hops 0/6 (the room pauses after 6 agent messages in a row with none from alice)"
    )
    agent_msgs(svc, 3)
    assert cmd(svc, "/hops").startswith("#build: hops 3/6 (")
    assert hop_events(svc) == []  # showing changes nothing
    ns = notices(svc)
    assert ns.count("/hops by alice (via cli: zsh ← Terminal)") == 1  # the CLI audit line only
    assert not any("via web" in n for n in ns)


def test_raise_from_the_web_persists_broadcasts_and_notices(svc: RoomService, rec: Rec) -> None:
    agent_msgs(svc, 2)
    rec.items.clear()
    assert cmd(svc, "/hops 30") == "#build: hop limit set to 30 (was 6); hops 2/30"
    room = svc.room("#build")
    assert room.hop_limit == 30 and room.hop_count == 2 and not room.paused
    assert svc.store.get_room("#build").hop_limit == 30  # persisted in the rooms table
    assert hop_events(svc) == [{"via": "web", "old": 6, "new": 30}]
    assert notices(svc)[-1] == "alice set the hop limit to 30 (was 6) (via web)"
    frames = [d for k, r, d in rec.items if k == "room" and r == "#build"]
    assert frames and frames[-1]["settings"]["hop_limit"] == 30 and frames[-1]["settings"]["hop_count"] == 2
    assert any(
        k == "msg" and d["msg"]["text"].startswith("alice set the hop limit to 30") for k, _, d in rec.items
    )
    assert svc.settings(room)["hop_limit"] == 30
    st = cmd(svc, "/status")
    assert "hops 2/30" in st and "] hop_limit_set" in st
    assert {r["name"]: r for r in svc.status()["rooms"]}["#build"]["hop_limit"] == 30


def test_zero_turns_the_guard_off_and_the_cli_may_turn_it_back_on(svc: RoomService, rec: Rec) -> None:
    agent_msgs(svc, 3)
    assert cmd(svc, "/hops 0") == (
        "#build: loop guard off (hop limit 0, was 6); agents may message each other without limit"
    )
    assert svc.room("#build").hop_limit == 0
    assert notices(svc)[-1] == "alice turned the loop guard off (hop limit was 6) (via web)"
    frames = [d for k, _, d in rec.items if k == "room"]
    assert frames[-1]["settings"]["hop_limit"] == 0
    assert cmd(svc, "/hops", CLI).startswith("#build: hops 3, loop guard off (agents may message each other")
    assert "hops 3, loop guard off" in cmd(svc, "/status")
    assert cmd(svc, "/hops 12", CLI) == "#build: loop guard on, hop limit 12; hops 3/12"
    assert svc.room("#build").hop_limit == 12
    assert "alice turned the loop guard on with a hop limit of 12 (via cli: zsh ← Terminal)" in notices(svc)
    assert hop_events(svc) == [{"via": "cli", "old": 0, "new": 12}, {"via": "web", "old": 6, "new": 0}]


def test_a_loop_guard_pause_is_never_lifted_by_a_new_limit(svc: RoomService) -> None:
    room = svc.room("#build")
    agent_msgs(svc, 7)
    svc.store.set_paused(room.id, True, "loop guard")
    assert "still paused by the loop guard: /resume to continue" in cmd(svc, "/hops", CLI)
    assert cmd(svc, "/hops 30") == "#build: hop limit set to 30 (was 6); now 7/30; /resume to continue"
    room = svc.room("#build")
    assert room.paused and room.paused_reason == "loop guard" and room.hop_count == 7
    # lowered below the count: still paused, and /resume is still the way out
    assert cmd(svc, "/hops 5", CLI) == (
        "#build: hop limit set to 5 (was 30); hops 7/5; still paused by the"
        " loop guard: /resume to continue (it resets the count)"
    )
    assert cmd(svc, "/hops 0") == (
        "#build: loop guard off (hop limit 0, was 5); agents may message each other"
        " without limit; the room is still paused by the loop guard: /resume to continue"
    )
    assert svc.room("#build").paused
    cmd(svc, "/resume")
    room = svc.room("#build")
    assert not room.paused and room.hop_count == 0 and room.hop_limit == 0


def test_a_human_pause_is_reported_and_kept(svc: RoomService) -> None:
    cmd(svc, "/pause")
    assert cmd(svc, "/hops 30") == (
        "#build: hop limit set to 30 (was 6); hops 0/30; the room is paused (paused by alice) until /resume"
    )
    assert svc.room("#build").paused


def test_lowering_below_the_count_says_the_next_agent_message_pauses(svc: RoomService) -> None:
    cmd(svc, "/hops 30")
    agent_msgs(svc, 10)
    assert cmd(svc, "/hops 5", CLI) == (
        "#build: hop limit set to 5 (was 30); hops 10/5; the next agent message pauses the room"
    )
    assert not svc.room("#build").paused  # not at once: on the next agent message (engine)


def test_help_lists_hops(svc: RoomService) -> None:
    text = cmd(svc, "/help")
    assert text == HELP_TEXT
    assert "/hops               show" in text and "/hops <n>" in text and "0 turns it off" in text
