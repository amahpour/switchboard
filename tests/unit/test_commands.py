"""commands: parsing, per-command roles, effects on an agent-less room (DESIGN.md §10)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock
from test_store import add_agent
from switchboard import db
from switchboard.broker.commands import Actor, CommandError, HELP_TEXT, parse_command, required_role
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
    hub = Hub()
    s = RoomService(store, hub, Config(), BrokerInfo(port=7419, test_mode=True), clock)
    s.create_room("#build")
    return s


@pytest.fixture
def rec(svc: RoomService) -> Rec:
    r = Rec()
    svc.hub.add(r)
    return r


def notices(svc: RoomService) -> list[str]:
    room = svc.room("#build")
    return [m.text for m in svc.store.history(room.id) if m.kind == "notice"]


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    "text,name,args",
    [
        ("/pause", "pause", ()),
        ("  /PAUSE  ", "pause", ()),
        ("/resume", "resume", ()),
        ("/budget", "budget", ()),
        ("/budget 12", "budget", ("12",)),
        ("/budget 0", "budget", ("0",)),
        ("/hold claude-1", "hold", ("claude-1",)),
        ("/release @Claude-1", "release", ("claude-1",)),
        ("/kick codex_2", "kick", ("codex_2",)),
        ("/who", "who", ()),
        ("/status", "status", ()),
        ("/help", "help", ()),
    ],
)
def test_parse_ok(text: str, name: str, args: tuple[str, ...]) -> None:
    c = parse_command(text)
    assert (c.name, c.args) == (name, args)


@pytest.mark.parametrize(
    "text",
    ["pause", "//pause", "/", "/mode chatty", "/pause now", "/budget -1", "/budget x", "/budget 1 2",
     "/budget 99999999", "/kick", "/kick a b", "/hold 1abc", "/who me"],
)
def test_parse_errors(text: str) -> None:
    with pytest.raises(CommandError) as e:
        parse_command(text)
    assert e.value.code == "bad_request"


def test_required_roles(svc: RoomService) -> None:
    room = svc.room("#build")  # budget 60
    need = {t: required_role(parse_command(t), room) for t in
            ["/pause", "/resume", "/kick a", "/budget", "/budget 10", "/budget 60", "/budget 61",
             "/hold a", "/release a", "/who", "/status", "/help"]}
    assert need == {
        "/pause": "human_cli", "/resume": "human", "/kick a": "human_cli", "/budget": "human_cli",
        "/budget 10": "human_cli", "/budget 60": "human_cli", "/budget 61": "human",
        "/hold a": "human_cli", "/release a": "human", "/who": "human_cli", "/status": "human_cli",
        "/help": "human_cli",
    }


# ---------------------------------------------------------------- effects
def test_pause_resume(svc: RoomService, rec: Rec) -> None:
    res = svc.command("#build", "/pause", CLI)
    assert res["ok"] and "paused" in res["text"]
    room = svc.room("#build")
    assert room.paused and room.paused_reason == "paused by alice"
    assert any(k == "room" and d["settings"]["paused"] for k, _, d in rec.items)
    assert svc.command("#build", "/pause", CLI)["text"].startswith("#build is already paused")
    # /resume raises activity: the CLI role can't
    with pytest.raises(ServiceError) as e:
        svc.command("#build", "/resume", CLI)
    assert e.value.code == "forbidden" and "web" in e.value.message
    svc.store.insert_message(room.id, sender_name="a", sender_kind="agent", via="mcp", text="x")
    assert svc.room("#build").hop_count == 1
    res = svc.command("#build", "/resume", WEB)
    room = svc.room("#build")
    assert not room.paused and room.paused_reason is None and room.hop_count == 0
    kinds = [e.kind for e in svc.store.recent_events(room_id=room.id, kinds=["pause", "resume"])]
    assert kinds == ["resume", "pause"]
    ns = notices(svc)
    assert "alice paused the room; no agent wakes until /resume (via cli: zsh ← Terminal)" in ns
    assert "alice resumed the room; loop guard reset (via web)" in ns


def test_budget(svc: RoomService, clock: FakeClock) -> None:
    res = svc.command("#build", "/budget", CLI)
    assert "60/60 wakes left" in res["text"]
    assert svc.command("#build", "/budget 5", CLI)["ok"]
    assert svc.room("#build").budget_remaining == 5
    with pytest.raises(ServiceError) as e:
        svc.command("#build", "/budget 6", CLI)  # raising needs the web session
    assert e.value.code == "forbidden"
    assert svc.command("#build", "/budget 500", WEB)["ok"]
    assert svc.room("#build").budget_remaining == 500
    ev = svc.store.recent_events(kinds=["budget_set"])[0]
    assert ev.data == {"via": "web", "old": 5, "new": 500}
    clock.advance(3601)
    assert "60/60" in svc.command("#build", "/budget", WEB)["text"]


def test_budget_role_uses_the_refilled_window(svc: RoomService, clock: FakeClock) -> None:
    """Raise vs lower is judged against what is really left after the hourly refill."""
    assert svc.command("#build", "/budget 0", CLI)["ok"]
    clock.advance(3700)  # the window rolled over: 60 left again
    assert svc.command("#build", "/budget 30", CLI)["ok"]  # a lowering, so the CLI may
    assert svc.room("#build").budget_remaining == 30
    # web raised it above the hourly budget; after a rollover only 60 are left,
    # so setting the old value again from the CLI is a raise
    assert svc.command("#build", "/budget 500", WEB)["ok"]
    clock.advance(3601)
    with pytest.raises(ServiceError) as e:
        svc.command("#build", "/budget 500", CLI)
    assert e.value.code == "forbidden"
    st = {r["name"]: r for r in svc.status()["rooms"]}["#build"]
    assert st["budget_remaining"] == 60


def test_member_commands_on_agent_less_room(svc: RoomService) -> None:
    for t in ["/kick claude-1", "/hold claude-1", "/release claude-1"]:
        with pytest.raises(ServiceError) as e:
            svc.command("#build", t, WEB)
        assert e.value.code == "not_found" and "no such member" in e.value.message


def test_release_needs_web(svc: RoomService) -> None:
    with pytest.raises(ServiceError) as e:
        svc.command("#build", "/release claude-1", CLI)
    assert e.value.code == "forbidden"


def test_hold_release_kick_with_a_member(svc: RoomService, rec: Rec) -> None:
    room = svc.room("#build")
    _, mid = add_agent(svc.store, room.id, "claude-1", harness="claude")
    assert "holding" in svc.command("#build", "/hold claude-1", CLI)["text"]
    assert svc.store.members(room.id)[0].held
    assert "already held" in svc.command("#build", "/hold claude-1", CLI)["text"]
    assert svc.command("#build", "/release claude-1", WEB)["ok"]
    assert not svc.store.members(room.id)[0].held
    assert any(k == "members" for k, _, _ in rec.items)
    svc.command("#build", "/kick claude-1", CLI)
    assert svc.store.members(room.id) == []
    *_, leave, audit = svc.store.history(room.id)
    assert (leave.kind, leave.sender_name, leave.text) == ("leave", "claude-1", "was kicked by alice")
    assert (audit.kind, audit.text) == ("notice", "/kick by alice (via cli: zsh ← Terminal)")
    kinds = [e.kind for e in svc.store.recent_events(room_id=room.id, kinds=["hold", "release", "kick"])]
    assert kinds == ["kick", "release", "hold"]


def test_who_status_help(svc: RoomService) -> None:
    who = svc.command("#build", "/who", WEB)["text"]
    assert "alice (you, human) + 0 agent(s)" in who and "no agents" in who
    st = svc.command("#build", "/status", WEB)["text"]
    assert "#build: active" in st and "budget 60/60" in st and "hops 0/6" in st
    assert "agents: none" in st and "TEST MODE" in st and "codex link" in st and "hooks:" in st
    assert svc.command("#build", "/help", WEB)["text"] == HELP_TEXT
    room = svc.room("#build")
    add_agent(svc.store, room.id, "codex-1", harness="codex")
    who = svc.command("#build", "/who", WEB)["text"]
    assert "codex-1  codex  idle" in who and "? approval mode unknown" in who
    svc.command("#build", "/pause", WEB)
    st = svc.command("#build", "/status", WEB)["text"]
    assert "paused (paused by alice)" in st and "codex-1: idle, tier -, queued 0" in st and "] pause" in st


def test_cli_commands_leave_an_audit_notice(svc: RoomService) -> None:
    svc.command("#build", "/who", CLI)
    svc.command("#build", "/who", WEB)
    ns = notices(svc)
    assert ns.count("/who by alice (via cli: zsh ← Terminal)") == 1
    assert not any("via web" in n and "/who" in n for n in ns)


def test_unknown_command_and_room(svc: RoomService) -> None:
    with pytest.raises(ServiceError) as e:
        svc.command("#build", "/mode quiet", WEB)
    assert e.value.code == "bad_request"
    with pytest.raises(ServiceError) as e:
        svc.command("#nope", "/pause", WEB)
    assert e.value.code == "not_found"


def test_human_say_is_literal(svc: RoomService, rec: Rec) -> None:
    m = svc.human_say("#build", "/pause", via="cli")
    assert m.text == "/pause" and m.kind == "chat" and m.via == "cli"
    assert not svc.room("#build").paused
    assert any(k == "msg" and d["msg"]["text"] == "/pause" for k, _, d in rec.items)
    with pytest.raises(ServiceError):
        svc.human_say("#build", "   ", via="web")
    with pytest.raises(ServiceError) as e:  # nothing left once cleaned
        svc.human_say("#build", "\x1b\x07\u200b\u202e ", via="web")
    assert e.value.code == "bad_request"
    with pytest.raises(ServiceError):
        svc.human_say("#build", "x" * 4001, via="web")


def test_mentions_cover_members_and_the_human(svc: RoomService) -> None:
    room = svc.room("#build")
    add_agent(svc.store, room.id, "claude-1")
    m = svc.human_say("#build", "@claude-1 and @ALICE, not @nobody or a@claude-1", via="web")
    assert m.mentions == ["alice", "claude-1"]
    rows = svc.store.con.execute("SELECT prio, mentioned FROM deliveries WHERE message_id=?", (m.id,)).fetchall()
    assert [tuple(r) for r in rows] == [(2, 1)]  # human message: prio 2 regardless
