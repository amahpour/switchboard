"""commands: parsing, per-command roles, effects on an agent-less room (DESIGN.md §10)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from test_store import add_agent

from switchboard import db
from switchboard.broker.commands import (
    HELP_TEXT,
    Actor,
    CommandError,
    help_text,
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


def make_service(tmp_path: Path, clock: FakeClock, cfg: Config) -> RoomService:
    store = Store(db.open_db(tmp_path / "y.db"), clock)
    s = RoomService(store, Hub(), cfg, BrokerInfo(port=7419, test_mode=True), clock)
    s.create_room("#build")
    return s


@pytest.fixture
def svc(tmp_path: Path, clock: FakeClock) -> RoomService:
    """A room with a wake budget of 60 an hour: the built-in default is no limit (#965), and most of
    these tests are about what a budget does."""
    return make_service(tmp_path, clock, Config().with_delivery(budget_per_hour=60))


@pytest.fixture
def free(tmp_path: Path, clock: FakeClock) -> RoomService:
    """A room as the built-in defaults make it: no wake budget, no hop limit."""
    return make_service(tmp_path, clock, Config())


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
        ("/quiet claude-1", "quiet", ("claude-1",)),
        ("/unquiet @Claude-1", "unquiet", ("claude-1",)),
        ("/release @Claude-1", "release", ("claude-1",)),
        ("/kick codex_2", "kick", ("codex_2",)),
        ("/who", "who", ()),
        ("/status", "status", ()),
        ("/help", "help", ()),
        ("/meeting", "meeting", ()),
        ("/meeting LEAVE", "meeting", ("leave",)),
        ("/meeting snapshot", "meeting", ("snapshot",)),
        ("/meeting unmute", "meeting", ("unmute",)),
        ("/meeting UNMUTE", "meeting", ("unmute",)),
        ("/meeting share plan.png", "meeting", ("share", "plan.png")),
        ("/meeting SHARE  release   plan.png", "meeting", ("share", "release plan.png")),
        ("/meeting share STOP", "meeting", ("share", "stop")),
        ("/meeting voice on", "meeting", ("voice", "on")),
        ("/meeting Voice OFF", "meeting", ("voice", "off")),
        ("/meeting https://meet.google.com/a-b-c", "meeting", ("https://meet.google.com/a-b-c",)),
    ],
)
def test_parse_ok(text: str, name: str, args: tuple[str, ...]) -> None:
    c = parse_command(text)
    assert (c.name, c.args) == (name, args)


@pytest.mark.parametrize(
    "text",
    [
        "pause",
        "//pause",
        "/",
        "/mode chatty",
        "/pause now",
        "/budget -1",
        "/budget x",
        "/budget 1 2",
        "/budget 99999999",
        "/kick",
        "/kick a b",
        "/hold 1abc",
        "/who me",
        "/meeting voice",
        "/meeting voice loud",
        "/meeting voice on now",
        "/meeting voice claude-1",
        "/meeting voice claude-1 Bad!",
        "/meeting voice claude-1 ../x",
        "/meeting voice claude-1 am-adam",
        "/meeting voice claude-1 " + "x" * 33,
        "/meeting voice claude-1 am_adam extra",
        "/meeting voice bad*name am_adam",
        "/meeting voice off am_adam",
        "/meeting snapshot now",
        "/meeting unmute now",
        "/meeting share",  # which file?
        "/meeting share " + "x" * 201,
        "/meeting leave now",
        "/meeting a b",
    ],
)
def test_parse_errors(text: str) -> None:
    with pytest.raises(CommandError) as e:
        parse_command(text)
    assert e.value.code == "bad_request"


def test_required_roles(svc: RoomService) -> None:
    room = svc.room("#build")  # budget 60
    need = {
        t: required_role(parse_command(t), room)
        for t in [
            "/pause",
            "/resume",
            "/kick a",
            "/budget",
            "/budget 10",
            "/budget 60",
            "/budget 61",
            "/budget 0",
            "/hold a",
            "/quiet a",
            "/unquiet a",
            "/release a",
            "/wake a",
            "/who",
            "/status",
            "/help",
            "/close",
            "/meeting",
            "/meeting leave",
            "/meeting voice on",
            "/meeting voice off",
            "/meeting snapshot",
            "/meeting unmute",
            "/meeting share plan.png",
            "/meeting share stop",
        ]
    }
    assert need == {
        "/pause": "human_cli",
        "/resume": "human",
        "/kick a": "human_cli",
        "/budget": "human_cli",
        "/budget 10": "human_cli",
        "/budget 60": "human_cli",
        "/budget 61": "human",
        "/budget 0": "human",  # no limit is the loosest there is, as /hops 0 is
        "/hold a": "human_cli",
        "/quiet a": "human_cli",  # fewer wakes (#548)
        "/unquiet a": "human",  # more wakes again, like /release
        "/release a": "human",
        "/wake a": "human",  # a wake now raises activity, like /release (#137)
        "/who": "human_cli",
        "/status": "human_cli",
        "/help": "human_cli",
        "/close": "human_cli",
        "/meeting": "human",
        "/meeting leave": "human",
        # agents speaking in the call, and a picture of its screen: a person's web session, like the call
        "/meeting voice on": "human",
        "/meeting voice off": "human",
        "/meeting snapshot": "human",
        "/meeting unmute": "human",
        # showing a picture in the call is the person's own web session too (§52.9)
        "/meeting share plan.png": "human",
        "/meeting share stop": "human",
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
    room = svc.room("#build")
    assert (room.budget_per_hour, room.budget_remaining) == (5, 5)  # the rate, and what is left with it
    with pytest.raises(ServiceError) as e:
        svc.command("#build", "/budget 6", CLI)  # raising needs the web session
    assert e.value.code == "forbidden"
    assert svc.command("#build", "/budget 500", WEB)["ok"]
    room = svc.room("#build")
    assert (room.budget_per_hour, room.budget_remaining) == (500, 500)
    ev = svc.store.recent_events(kinds=["budget_set"])[0]
    assert ev.data == {"via": "web", "old": 5, "new": 500, "per_hour": True}
    clock.advance(3601)
    assert "500/500" in svc.command("#build", "/budget", WEB)["text"]


def test_budget_sets_the_rate_so_the_header_never_reads_above_it(svc: RoomService) -> None:
    """#965: ``/budget 20`` sets the hourly rate with what is left, as the room dialog's save does.
    Before, it set only what was left, so a room could read 99995/60. Fails if the rate stays 60."""
    assert svc.command("#build", "/budget 20", CLI)["ok"]
    s = svc.settings(svc.room("#build"))
    assert (s["budget_remaining"], s["budget_per_hour"]) == (20, 20)
    assert "20/20" in svc.command("#build", "/status", CLI)["text"]


def test_budget_role_is_judged_on_the_rate_and_what_is_left_after_a_refill(
    svc: RoomService, clock: FakeClock
) -> None:
    """No limit is a loosening, and so is any number above what is really left, after the hourly
    refill: ``/budget 60`` hands a spent room its wakes back, so the CLI may not do it until the hour
    has rolled over and they are back anyway. Fails if the role ignores 0, or the refill."""
    with pytest.raises(ServiceError) as e:
        svc.command("#build", "/budget 0", CLI)
    assert e.value.code == "forbidden" and "turning the wake budget off" in e.value.message
    svc.store.set_budget(svc.room("#build").id, 0)  # this hour's wakes are all used
    with pytest.raises(ServiceError):
        svc.command("#build", "/budget 60", CLI)
    clock.advance(3700)  # the window rolled over: 60 are left again
    assert svc.command("#build", "/budget 60", CLI)["ok"]
    assert svc.command("#build", "/budget 30", CLI)["ok"]  # a lowering, so the CLI may
    assert svc.command("#build", "/budget 0", WEB)["ok"]
    room = svc.room("#build")
    assert (room.budget_per_hour, room.budget_remaining) == (0, 0)


def test_a_room_with_no_budget_says_so_and_a_number_only_tightens_it(free: RoomService) -> None:
    """#965: the built-in rate is 0, no limit. ``/budget`` and ``/status`` say "no limit" and never
    "0/0", and setting a number is a tightening the CLI may do. Fails if a rate of 0 reads as an
    empty budget or setting one needs the web."""
    room = free.room("#build")
    assert not room.budget_on and room.budget_per_hour == 0
    assert "no wake limit" in free.command("#build", "/budget", CLI)["text"]
    st = free.command("#build", "/status", CLI)["text"]
    assert "no wake limit" in st and "0/0" not in st
    assert free.command("#build", "/budget 20", CLI)["ok"]
    assert free.room("#build").budget_on
    assert "20/20" in free.command("#build", "/budget", CLI)["text"]
    assert free.command("#build", "/budget 0", WEB)["ok"]
    assert "no wake limit" in free.command("#build", "/budget", CLI)["text"]


def test_member_commands_on_agent_less_room(svc: RoomService) -> None:
    for t in [
        "/kick claude-1",
        "/hold claude-1",
        "/release claude-1",
        "/wake claude-1",
        "/quiet claude-1",
        "/unquiet claude-1",
    ]:
        with pytest.raises(ServiceError) as e:
            svc.command("#build", t, WEB)
        assert e.value.code == "not_found" and "no such member" in e.value.message


def test_release_needs_web(svc: RoomService) -> None:
    with pytest.raises(ServiceError) as e:
        svc.command("#build", "/release claude-1", CLI)
    assert e.value.code == "forbidden"


def test_unquiet_needs_web(svc: RoomService) -> None:
    """Turning quiet off raises what wakes the agent, so the CLI role cannot. ``/quiet``
    reduces it, so the CLI can. Fails if the roles are swapped or missing."""
    room = svc.room("#build")
    add_agent(svc.store, room.id, "claude-1", harness="claude")
    assert svc.command("#build", "/quiet claude-1", CLI)["ok"]
    with pytest.raises(ServiceError) as e:
        svc.command("#build", "/unquiet claude-1", CLI)
    assert e.value.code == "forbidden" and "web" in e.value.message


def test_quiet_and_unquiet_with_a_member(svc: RoomService) -> None:
    """``/quiet`` stores the flag and says so; ``/unquiet`` clears it. An agent's say is
    not this path. Fails if the column stays 0, or if ``/unquiet`` is accepted from the CLI."""
    room = svc.room("#build")
    add_agent(svc.store, room.id, "claude-1", harness="claude")
    res = svc.command("#build", "/quiet claude-1", CLI)
    assert "quieted claude-1" in res["text"] and "addressed" in res["text"]
    assert svc.store.members(room.id)[0].quiet
    assert "already quiet" in svc.command("#build", "/quiet claude-1", CLI)["text"]
    who = svc.command("#build", "/who", WEB)["text"]
    assert "quiet" in who
    res = svc.command("#build", "/unquiet claude-1", WEB)
    assert "unquieted claude-1" in res["text"]
    assert not svc.store.members(room.id)[0].quiet
    assert "not quiet" in svc.command("#build", "/unquiet claude-1", WEB)["text"]
    kinds = [e.kind for e in svc.store.recent_events(room_id=room.id, kinds=["quiet", "unquiet"])]
    assert kinds == ["unquiet", "quiet"]


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


class _Talker:
    """A stand-in for the broker's talker: only whether the operator set its key."""

    def __init__(self, key: bool) -> None:
        self.key = key

    def available(self) -> bool:
        return self.key


def test_help_lists_the_talker_only_when_it_has_a_key(svc: RoomService) -> None:
    """/help shows /meeting talker only when the server can run one (#854: the talker is parked).

    Without a key /meeting talker on can only refuse, so listing it sends people to a dead end. The
    bug this catches: the line shown on a server with no talker key, or hidden on one that has it."""
    line = "/meeting talker on|off"
    assert line in HELP_TEXT  # the full list keeps it, for a server with a key
    assert line not in svc.command("#build", "/help", WEB)["text"]  # no meeting server: no talker
    svc.talker = _Talker(key=False)
    shown = svc.command("#build", "/help", WEB)["text"]
    assert line not in shown and "/meeting voice on" in shown and "/meeting leave" in shown
    assert shown.count("\n") == HELP_TEXT.count("\n") - 1  # only that one line went
    svc.talker = _Talker(key=True)
    assert svc.command("#build", "/help", WEB)["text"] == HELP_TEXT


def test_who_status_help(svc: RoomService) -> None:
    who = svc.command("#build", "/who", WEB)["text"]
    assert "alice (you, human) + 0 agent(s)" in who and "no agents" in who
    st = svc.command("#build", "/status", WEB)["text"]
    assert "#build: active" in st and "budget 60/60" in st
    assert "hops 0, loop guard off" in st  # the guard ships off (#406); it showed "hops 0/6"
    assert "agents: none" in st and "TEST MODE" in st
    assert "codex link" not in st and "hooks:" not in st  # the local path's lines went with it (#385)
    assert svc.command("#build", "/help", WEB)["text"] == help_text(False)  # no meeting server here
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
    rows = svc.store.con.execute(
        "SELECT prio, mentioned FROM deliveries WHERE message_id=?", (m.id,)
    ).fetchall()
    assert [tuple(r) for r in rows] == [(2, 1)]  # human message: prio 2 regardless


def test_a_voice_for_a_member_parses_to_its_name_and_a_lowercase_voice() -> None:
    """``/meeting voice <name> <voice>`` (#713): the name is a screen name, the voice a-z, 0-9 and _, or
    ``default``. Case is folded; ``on`` and ``off`` stay the switch."""
    cmd = parse_command("/meeting voice @Claude-1 AM_Adam")
    assert (cmd.name, cmd.args) == ("meeting", ("voice", "claude-1", "am_adam"))
    assert parse_command("/meeting voice claude-1 default").args == ("voice", "claude-1", "default")
    assert parse_command("/meeting voice on").args == ("voice", "on")


def test_pinning_a_voice_is_a_persons_command_on_the_web_and_never_the_cli_or_an_agents() -> None:
    """The role is ``human`` (the web), like ``/meeting voice on``. Fails if the command line, which a script
    can reach, may pin a voice."""
    from switchboard.broker.commands import ROLE_RANK, required_role

    class _Room:
        budget_remaining = 60

    need = required_role(parse_command("/meeting voice claude-1 am_adam"), _Room())  # type: ignore[arg-type]
    assert need == "human" and ROLE_RANK[CLI.role] < ROLE_RANK[need] <= ROLE_RANK[WEB.role]
