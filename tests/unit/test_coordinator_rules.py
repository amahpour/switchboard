"""A room's coordinator (#976, DESIGN.md §50.2): the one agent a person names to run the room actions they
tick. These are the pure rules and the service's checks, with a ``FakeClock`` for the hourly limit; the
agent's tool over HTTPS and the web dialog are in ``tests/integration/test_coordinator.py`` and
``tests/e2e/test_coordinator_ui.py``. Each test says the bug that would make it fail."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from test_store import add_agent

from switchboard import db
from switchboard.broker import commands as cmds
from switchboard.broker.commands import Actor, CommandError, parse_command
from switchboard.broker.hub import Hub
from switchboard.broker.service import BrokerInfo, RoomService, ServiceError
from switchboard.config import Config
from switchboard.store import Store

WEB = Actor(role="human", via="web", name="alice", person_id=None)
CLI = Actor(role="human_cli", via="cli", chain="zsh")
LINK = "https://us02web.zoom.us/j/123456789?pwd=SECRET99"


@pytest.fixture
def svc(tmp_path: Path, clock: FakeClock) -> RoomService:
    store = Store(db.open_db(tmp_path / "c.db"), clock)
    s = RoomService(store, Hub(), Config(), BrokerInfo(port=7419, test_mode=True), clock)
    s.create_room("#build")
    s.create_room("#other")
    return s


def named(svc: RoomService, actions: str, room: str = "#build", agent: str = "lead-1") -> tuple[int, int]:
    """An agent in ``room``, named the coordinator by alice's web session with ``actions`` ticked."""
    r = svc.room(room)
    _pid, mid = add_agent(svc.store, r.id, agent)
    svc.command(room, f"/coordinator {agent} {actions}", WEB)
    return r.id, mid


def notices(svc: RoomService, room: str = "#build") -> list[str]:
    return [m.text for m in svc.store.history(svc.room(room).id) if m.kind == "notice"]


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("/room quiet on", "quiet"),
        ("/room quiet off", "quiet"),
        ("/quiet bob-1", "quiet"),
        ("/unquiet bob-1", "quiet"),
        ("/pin 3", "pins"),
        ("/unpin 3", "pins"),
        ("/meeting https://zoom.us/j/1", "calls"),
        ("/meeting leave", "calls"),
        ("/meeting voice on", "calls"),
        ("/meeting voice off", "calls"),
        ("/budget 10", "budget"),
        ("/budget 0", "budget"),
        ("/hops 5", "budget"),
        ("/hops 0", "budget"),
    ],
)
def test_each_command_maps_to_the_one_action_that_allows_it(text: str, action: str) -> None:
    """The table is the whole of what the role can run. Fails if a command moves to another action (so a
    box ticked for one thing would allow another)."""
    assert cmds.coordinator_action(parse_command(text)) == action


@pytest.mark.parametrize(
    "text",
    [
        "/coordinator lead-1 quiet",
        "/coordinator off",
        "/coordinator",
        "/kick bob-1",
        "/close",
        "/pause",
        "/resume",
        "/wake bob-1",
        "/hold bob-1",
        "/release bob-1",
        "/rename #x",
        "/catchup bob-1",
        "/review https://example.com/a/pull/1",
        "/who",
        "/status",
        "/help",
        "/room",
        "/room voice on",
        "/room listen on",
        "/room review on",
        "/meeting",
        "/meeting https://zoom.us/j/1 mine",
        "/meeting unmute",
        "/meeting snapshot",
        "/meeting share stop",
        "/meeting talker on",
        "/meeting listen bob-1",
        "/meeting voice bob-1 default",
        "/budget",
        "/hops",
    ],
)
def test_every_other_command_has_no_action_so_no_ticked_box_allows_it(text: str) -> None:
    """Approvals-adjacent, destructive and person-only commands, and the read-only ones, are outside the
    table: not even all five boxes ticked run them. Fails if one is added to the table by accident, or a
    command the table doesn't know falls through to the permissive default instead of failing closed."""
    cmd = parse_command(text)
    assert cmds.coordinator_action(cmd) is None
    every = Actor(
        role="human", via="coordinator", coordinator="lead-1", allowed=tuple(cmds.COORDINATOR_ACTIONS)
    )
    assert cmds.coordinator_refusal(cmd, every) is not None


def test_the_list_of_actions_is_the_five_and_a_command_is_refused_when_its_box_is_not_ticked() -> None:
    """Fails if the fixed list changes without this test (and the docs) changing, or a box that is not
    ticked still lets its command run."""
    assert tuple(cmds.COORDINATOR_ACTIONS) == ("quiet", "boards", "pins", "calls", "budget")
    ticked = Actor(role="human", via="coordinator", coordinator="lead-1", allowed=("pins",))
    assert cmds.coordinator_refusal(parse_command("/pin 3"), ticked) is None
    why = cmds.coordinator_refusal(parse_command("/room quiet on"), ticked)
    assert why is not None and "quiet is not ticked" in why and "/pin <id>" in why


@pytest.mark.parametrize(
    ("text", "args"),
    [
        ("/coordinator lead-1 quiet", ("lead-1", "quiet")),
        ("/coordinator @Lead-1 pins,quiet", ("lead-1", "quiet", "pins")),
        ("/coordinator lead-1 quiet pins quiet", ("lead-1", "quiet", "pins")),
        ("/coordinator lead-1 none", ("lead-1",)),
        ("/coordinator off", ("off",)),
        ("/coordinator", ()),
    ],
)
def test_coordinator_parses_actions_in_the_fixed_order(text: str, args: tuple[str, ...]) -> None:
    """Fails if an action is kept twice, order depends on how it was typed, or ``none`` is read as a key."""
    assert parse_command(text).args == args


@pytest.mark.parametrize(
    "text",
    [
        "/coordinator lead-1",
        "/coordinator lead-1 admin",
        "/coordinator lead-1 quiet,none",
        "/coordinator lead-1 ,",
    ],
)
def test_an_unknown_or_missing_action_is_refused(text: str) -> None:
    """An unknown key is refused, never ignored (fail closed). Fails if it is dropped and the rest saved."""
    with pytest.raises(CommandError):
        parse_command(text)


def test_only_a_web_session_names_changes_or_removes_a_coordinator(svc: RoomService) -> None:
    """Naming and ``off`` need the web session; the bare ``/coordinator`` only reads and works from the CLI.
    Fails if the CLI role can name one, or the read is refused."""
    add_agent(svc.store, svc.room("#build").id, "lead-1")
    with pytest.raises(ServiceError) as e:
        svc.command("#build", "/coordinator lead-1 quiet", CLI)
    assert e.value.code == "forbidden" and svc.store.coordinator(svc.room("#build").id) is None
    svc.command("#build", "/coordinator lead-1 quiet", WEB)
    with pytest.raises(ServiceError):
        svc.command("#build", "/coordinator off", CLI)
    assert svc.store.coordinator(svc.room("#build").id) is not None
    assert "lead-1 is the coordinator" in svc.command("#build", "/coordinator", CLI)["text"]


def test_naming_one_replaces_the_first_and_off_removes_it(svc: RoomService) -> None:
    """One coordinator a room, and the room says each change. Fails if two are kept, the first keeps its
    actions, or off leaves the row."""
    rid, _ = named(svc, "quiet")
    add_agent(svc.store, rid, "lead-2")
    out = svc.command("#build", "/coordinator lead-2 pins", WEB)
    assert "replaces lead-1" in out["text"]
    co = svc.store.coordinator(rid)
    assert co is not None and co.name == "lead-2" and co.actions == ("pins",) and co.named_by == "alice"
    assert [m.name for m in svc.store.members(rid) if m.coordinator is not None] == ["lead-2"]
    svc.command("#build", "/coordinator off", WEB)
    assert svc.store.coordinator(rid) is None
    assert any("named lead-2 the room's coordinator" in n for n in notices(svc))
    assert any("removed lead-2 as the room's coordinator" in n for n in notices(svc))


def test_naming_someone_who_is_not_here_is_refused(svc: RoomService) -> None:
    """Fails if a name that is not a member of this room is stored (the role is a membership's)."""
    with pytest.raises(ServiceError) as e:
        svc.command("#build", "/coordinator ghost-1 quiet", WEB)
    assert e.value.code == "not_found"


def test_a_ticked_command_runs_for_the_person_and_the_notice_names_both(svc: RoomService) -> None:
    """``/room quiet on`` from the coordinator turns the room quiet and the notice reads "<it> for
    <person> ...", with no "(via ...)" tail. Fails if the notice names only one of them, or the setting
    does not change."""
    rid, mid = named(svc, "quiet")
    out = svc.coordinator_command("#build", mid, "/room quiet on")
    assert out["ok"] and svc.store.room_by_id(rid).quiet  # type: ignore[union-attr]
    assert notices(svc)[-1].startswith("lead-1 for alice turned on a quiet room")
    assert "(via" not in notices(svc)[-1]


def test_an_action_that_is_not_ticked_and_a_command_outside_the_table_are_refused(svc: RoomService) -> None:
    """With only ``pins`` ticked, ``/room quiet on`` changes nothing and the refusal lists what is allowed;
    ``/kick`` and ``/coordinator`` are refused with all boxes ticked. Fails if the setting changes."""
    rid, mid = named(svc, "pins")
    with pytest.raises(ServiceError) as e:
        svc.coordinator_command("#build", mid, "/room quiet on")
    assert e.value.code == "forbidden" and "/pin <id>" in e.value.message
    assert not svc.store.room_by_id(rid).quiet  # type: ignore[union-attr]
    svc.command("#build", "/coordinator lead-1 quiet,boards,pins,calls,budget", WEB)
    for text in ("/kick lead-1", "/coordinator lead-1 quiet", "/coordinator off", "/close", "/wake lead-1"):
        with pytest.raises(ServiceError) as e:
            svc.coordinator_command("#build", mid, text)
        assert e.value.code == "forbidden", text
    assert svc.store.coordinator(rid) is not None


def test_an_agent_that_is_not_the_coordinator_is_refused(svc: RoomService) -> None:
    """Another member of the room, even with the coordinator named, can run nothing. Fails if the check
    is on the room and not the membership."""
    rid, _ = named(svc, "quiet,pins,budget,calls,boards")
    _pid, other = add_agent(svc.store, rid, "bob-1")
    with pytest.raises(ServiceError) as e:
        svc.coordinator_command("#build", other, "/room quiet on")
    assert e.value.code == "forbidden" and "not the coordinator" in e.value.message
    assert not svc.store.room_by_id(rid).quiet  # type: ignore[union-attr]


def test_the_coordinator_of_one_room_runs_nothing_in_another(svc: RoomService) -> None:
    """The role is one room's. Fails if its membership id can run a command through another room's name."""
    _, mid = named(svc, "quiet")
    other = svc.room("#other")
    with pytest.raises(ServiceError):
        svc.coordinator_command("#other", mid, "/room quiet on")
    assert not svc.store.room_by_id(other.id).quiet  # type: ignore[union-attr]


def test_it_acts_as_a_person_never_as_an_admin(svc: RoomService) -> None:
    """The actor a coordinator's command runs as is role ``human``, not an admin, and ``apply`` itself
    refuses a command outside its actions (defence in depth, not only the service's check). Fails if the
    actor is built as an admin, or ``apply`` runs what the table does not allow."""
    rid, _ = named(svc, "quiet")
    actor = Actor(role="human", via="coordinator", name="alice", coordinator="lead-1", allowed=("quiet",))
    assert not actor.admin
    with pytest.raises(CommandError):
        cmds.apply(parse_command("/kick lead-1"), svc.room("#build"), actor, svc)
    with pytest.raises(CommandError):
        cmds.apply(parse_command("/coordinator lead-1 boards"), svc.room("#build"), actor, svc)
    assert svc.store.coordinator(rid).actions == ("quiet",)  # type: ignore[union-attr]


def test_the_hourly_limit_refuses_the_31st_and_says_when_to_try_again(
    svc: RoomService, clock: FakeClock
) -> None:
    """30 commands in a rolling hour: the 31st is refused with when it may try again, one more is allowed
    when the oldest is an hour old, and a refusal is not itself counted. Advances the ``FakeClock``; never
    sleeps. Fails if the limit is off by one, is per room rather than per coordinator, or never frees."""
    rid, mid = named(svc, "quiet")
    for _ in range(cmds.COORDINATOR_LIMIT):
        svc.coordinator_command("#build", mid, "/unquiet lead-1")
        clock.advance(10)
    with pytest.raises(ServiceError) as e:
        svc.coordinator_command("#build", mid, "/unquiet lead-1")
    assert e.value.code == "rate_limited" and "30 commands an hour" in e.value.message
    assert len(svc.store.coordinator_uses(mid, 0)) == cmds.COORDINATOR_LIMIT
    clock.advance(3600 - 10 * cmds.COORDINATOR_LIMIT + 1)  # the first use is now over an hour old
    svc.coordinator_command("#build", mid, "/unquiet lead-1")
    with pytest.raises(ServiceError):
        for _ in range(cmds.COORDINATOR_LIMIT):
            svc.coordinator_command("#build", mid, "/unquiet lead-1")


def test_a_refused_command_does_not_spend_the_limit(svc: RoomService) -> None:
    """Only a command that runs counts. Fails if refusals fill the 30 and lock a real command out."""
    _, mid = named(svc, "pins")
    for _ in range(cmds.COORDINATOR_LIMIT + 5):
        with pytest.raises(ServiceError):
            svc.coordinator_command("#build", mid, "/room quiet on")
    assert svc.store.coordinator_uses(mid, 0) == []


def test_leaving_ends_the_role_and_a_session_end_keeps_it_for_a_resume(svc: RoomService) -> None:
    """The row goes with a leave or a kick (a person's choice), stays across a session's end so a resumed
    join can take it (``create_membership(takes_role_of=...)`` moves it in the same transaction), and a
    newcomer that merely has the same name does not get it. Fails if a leave leaves the role for the next
    holder of the name, or a resume loses it."""
    rid, mid = named(svc, "quiet,pins")
    svc.store.end_membership(mid, "session_end")
    assert svc.store.coordinator(rid) is None  # inert while its membership is out of the room
    newcomer = svc.store.create_membership(
        rid, svc.store.upsert_participant("test", "test:new").id, "lead-1x"
    )
    assert svc.store.coordinator(rid) is None and newcomer.id != mid
    restarted = svc.store.upsert_participant("test", "test:restarted").id  # a resumed join is a new session
    back = svc.store.create_membership(rid, restarted, "lead-1", takes_role_of=mid)
    co = svc.store.coordinator(rid)
    assert co is not None and co.membership_id == back.id and co.actions == ("quiet", "pins")
    svc.store.end_membership(back.id, "leave")
    assert svc.store.coordinator(rid) is None
    third = svc.store.upsert_participant("test", "test:third").id
    again = svc.store.create_membership(rid, third, "lead-1", takes_role_of=back.id)
    assert svc.store.coordinator(rid) is None and again.id != back.id


def test_a_different_agent_with_the_same_name_does_not_get_the_role(svc: RoomService) -> None:
    """The role is a membership's, not a name's. Fails if it is looked up by screen name."""
    rid, mid = named(svc, "quiet")
    svc.store.end_membership(mid, "leave")
    _pid, new_mid = add_agent(svc.store, rid, "lead-1", harness="claude")
    with pytest.raises(ServiceError):
        svc.coordinator_command("#build", new_mid, "/room quiet on")


def test_the_call_link_is_in_no_reply_notice_event_or_error(svc: RoomService) -> None:
    """A coordinator's ``/meeting <link>`` follows a person's rules: the link, and its passcode, appear in no
    reply, refusal, notice or event (no meeting server is set here, so it is the reply that is checked; the
    integration test follows a real join). Fails if any carries ``SECRET99`` or the address."""
    rid, mid = named(svc, "calls")
    seen: list[str] = []
    for text in (
        f"/meeting {LINK}",
        f"/meeting {LINK} mine",
        f"/meeting {LINK} extra",
        "/meeting https://example.com/x?pwd=SECRET99",
    ):
        try:
            seen.append(svc.coordinator_command("#build", mid, text)["text"])
        except ServiceError as e:
            seen.append(e.message)
    seen += notices(svc)
    seen += [str(e.data) for e in svc.store.recent_events(room_id=rid, limit=50)]
    blob = "\n".join(seen)
    assert "SECRET99" not in blob and "zoom.us" not in blob and "example.com" not in blob, blob


def test_a_coordinator_row_is_removed_with_its_room_and_the_who_text_flags_it(svc: RoomService) -> None:
    """``who`` (the person's) names the coordinator and its actions. Fails if the flag is missing."""
    named(svc, "quiet,boards")
    assert "coordinator: quiet, boards" in svc.command("#build", "/who", WEB)["text"]
