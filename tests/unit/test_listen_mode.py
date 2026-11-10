"""Listening is opt-in, bounded per call, and keeps peer stubs (DESIGN.md §52.1, #697)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_agents import listener, result, waiting, woke
from engine_world import World
from test_commands import CLI, WEB
from test_commands import svc as svc
from test_store import add_agent

from switchboard.broker.service import RoomService, ServiceError
from switchboard.config import Config
from switchboard.models import Message


def call_world(tmp_path: Path, clock: FakeClock, **delivery) -> tuple[World, int]:
    w = World(tmp_path, clock, Config().with_delivery(**delivery))
    call = w.store.insert_meeting(w.room.id, started_by="alice", platform="zoom")
    w.store.set_meeting_live(call.id)
    return w, call.id


def line(w: World, call_id: int, text: str = "alice: The upload limit matters.") -> Message:
    msg = w.store.insert_message(
        w.room.id,
        sender_name="call",
        sender_kind="meeting",
        via="meeting",
        text=text,
        meeting_id=call_id,
    )
    w.actions += w.engine.on_message(msg.id)
    return msg


@pytest.mark.parametrize("quiet", [False, True])
def test_only_a_listener_wakes_after_its_own_quiet_period(
    tmp_path: Path, clock: FakeClock, quiet: bool
) -> None:
    """Call chatter uses 15 s even in wait(), and quiet keeps ordinary chatter held."""
    w, call = call_world(tmp_path, clock, quiet_s=0, max_hold_s=1)
    _, m, sink = waiting(w, "listener")
    _, _, other = waiting(w, "other")
    w.store.set_quiet(m.id, quiet)
    w.store.set_listen(m.id, True, call)
    line(w, call)
    assert w.engine.wakes_at(m.id) == clock.now() + 15
    clock.advance(14)
    w.actions += w.engine.tick()
    assert not woke(w, sink)
    clock.advance(1)
    w.actions += w.engine.tick()
    assert woke(w, sink) and not woke(w, other)
    assert "upload limit" in result(w, sink)["text"]


def test_continuous_talk_wakes_at_listen_hold(tmp_path: Path, clock: FakeClock) -> None:
    """A new line every 10 s prevents a quiet gap, but the first cannot wait beyond 40 s."""
    w, call = call_world(tmp_path, clock)
    _, m, sink = waiting(w)
    w.store.set_listen(m.id, True, call)
    line(w, call)
    for _ in range(3):
        clock.advance(10)
        line(w, call)
        assert not woke(w, sink)
    clock.advance(10)
    line(w, call)
    assert woke(w, sink)


def test_stop_nudge_keeps_untrusted_call_text_out_of_the_followup(tmp_path: Path, clock: FakeClock) -> None:
    """Claude gets an explicit read nudge, still only stubs, fitted within the hook limit."""
    w, call = call_world(tmp_path, clock)
    _, m, park = listener(w)
    w.store.set_listen(m.id, True, call)
    msg = line(w, call, "alice: untrusted call words")
    clock.advance(15)
    w.actions += w.engine.tick()
    text = result(w, park)["text"]
    assert '1 call lines: call read("#build") to hear them.' in text.splitlines()[:3]
    assert "untrusted call words" not in text and "not shown here" in text
    assert len(text) <= w.cfg.delivery.batch_max_chars
    assert w.states(m)[msg.id] == "offered"


def test_cap_cannot_be_reset_by_toggling_and_a_new_call_gets_a_new_cap(
    tmp_path: Path, clock: FakeClock
) -> None:
    """A last allowed listening wake turns it off; toggling cannot buy more turns on this call."""
    w, call = call_world(tmp_path, clock, listen_max_wakes=1)
    _, m, sink = waiting(w)
    w.store.set_listen(m.id, True, call)
    line(w, call)
    clock.advance(15)
    w.actions += w.engine.tick()
    assert woke(w, sink) and not w.m(m).listen
    assert w.m(m).listen_wakes == 1
    assert any("listening wake cap" in getattr(a, "text", "") for a in w.actions)
    w.store.set_listen(m.id, True, call, limit=1)
    assert not w.m(m).listen
    w.store.end_meeting(call, None)
    next_call = w.store.insert_meeting(w.room.id, started_by="alice", platform="zoom")
    w.store.set_listen(m.id, True, next_call.id)
    assert w.m(m).listen and w.m(m).listen_wakes == 0


def test_ending_a_call_turns_off_its_listeners(tmp_path: Path, clock: FakeClock) -> None:
    """The store owns every end path, including hosted calls, failures and broker recovery."""
    w, call = call_world(tmp_path, clock)
    _, m, sink = waiting(w)
    w.store.set_listen(m.id, True, call)
    line(w, call)
    w.store.end_meeting(call, None)
    assert not w.m(m).listen
    clock.advance(100)
    w.actions += w.engine.tick()
    assert not woke(w, sink) and w.engine.wakes_at(m.id) is None


def test_a_new_call_does_not_wake_for_an_old_calls_pending_lines(tmp_path: Path, clock: FakeClock) -> None:
    w, old = call_world(tmp_path, clock)
    _, m, sink = waiting(w)
    line(w, old)
    w.store.end_meeting(old, None)
    new = w.store.insert_meeting(w.room.id, started_by="alice", platform="zoom")
    w.store.set_listen(m.id, True, new.id)
    clock.advance(100)
    w.actions += w.engine.tick()
    assert not woke(w, sink)


@pytest.mark.parametrize("status", ["waiting-approval", "busy", "offline"])
def test_listening_does_not_bypass_status_gates(tmp_path: Path, clock: FakeClock, status: str) -> None:
    w, call = call_world(tmp_path, clock)
    p, m = w.agent("listener", status=status)
    w.store.set_listen(m.id, True, call)
    line(w, call)
    clock.advance(100)
    w.actions += w.engine.tick()
    assert not w.engine.wakes_at(m.id)
    assert not w.store.inflight_offer(m.id)


@pytest.mark.parametrize("mode", ["prompting", "bypass", "unknown"])
def test_command_roles_notices_and_idempotence(svc: RoomService, mode: str) -> None:
    """Only the web may increase wakes; CLI can stop them; warnings describe approvals."""
    room = svc.room("#build")
    pid, mid = add_agent(svc.store, room.id, "alpha")
    svc.store.update_participant(pid, approval_mode=mode)
    svc.store.insert_meeting(room.id, started_by="alice", platform="zoom")
    with pytest.raises(ServiceError, match="web"):
        svc.command(room.name, "/meeting listen alpha", CLI)
    svc.command(room.name, "/meeting listen @alpha", WEB)
    assert svc.store.get_membership(mid).listen
    history = svc.store.history(room.id)
    notices = "\n".join(m.text for m in history)
    assert "listening" in notices
    assert ("approvals off" in notices) == (mode != "prompting")
    svc.command(room.name, "/meeting listen alpha", WEB)
    assert len(svc.store.history(room.id)) == len(history)
    svc.command(room.name, "/meeting listen alpha off", CLI)
    assert not svc.store.get_membership(mid).listen


def test_listening_command_requires_a_call_and_a_member(svc: RoomService) -> None:
    add_agent(svc.store, svc.room("#build").id, "alpha")
    with pytest.raises(ServiceError, match="no call is being transcribed"):
        svc.command("#build", "/meeting listen alpha", WEB)
    svc.store.insert_meeting(svc.room("#build").id, started_by="alice", platform="zoom")
    with pytest.raises(ServiceError, match="no such member"):
        svc.command("#build", "/meeting listen absent", WEB)


def test_listening_still_needs_the_room_budget(tmp_path: Path, clock: FakeClock) -> None:
    """A private cap supplements the shared budget; exhaustion holds call chatter but not the person."""
    w, call = call_world(tmp_path, clock, budget_per_hour=0)
    _, m, sink = waiting(w)
    w.store.set_listen(m.id, True, call)
    line(w, call)
    clock.advance(100)
    w.actions += w.engine.tick()
    assert not woke(w, sink) and w.m(m).listen_wakes == 0
    w.human("Please answer now")
    assert woke(w, sink) and w.m(m).listen_wakes == 0


@pytest.mark.parametrize("gate", ["held", "paused"])
def test_listening_respects_room_and_membership_holds(tmp_path: Path, clock: FakeClock, gate: str) -> None:
    w, call = call_world(tmp_path, clock)
    _, m, sink = waiting(w)
    w.store.set_listen(m.id, True, call)
    if gate == "held":
        w.store.set_held(m.id, True)
    else:
        w.store.set_paused(w.room.id, True)
    line(w, call)
    clock.advance(100)
    w.actions += w.engine.tick()
    assert not woke(w, sink) and w.m(m).listen_wakes == 0


def test_the_private_cap_is_per_agent_and_the_flags_are_stored(tmp_path: Path, clock: FakeClock) -> None:
    """A second listener keeps its allowance, and reading through a second connection preserves state."""
    from switchboard import db
    from switchboard.store import Store

    w, call = call_world(tmp_path, clock, listen_max_wakes=1)
    _, a, sink = waiting(w, "alpha")
    _, b = w.agent("beta", status="idle")
    w.store.set_listen(a.id, True, call)
    w.store.set_listen(b.id, True, call)
    line(w, call)
    clock.advance(15)
    w.actions += w.engine.tick()
    assert woke(w, sink)
    other = Store(db.open_db(tmp_path / "y.db"), clock)
    assert not other.get_membership(a.id).listen
    assert other.get_membership(a.id).listen_wakes == 1
    assert other.get_membership(b.id).listen and other.get_membership(b.id).listen_wakes == 0
    other.con.close()


@pytest.mark.parametrize(
    "setting,value",
    [
        ("listen_quiet_s", 0),
        ("listen_quiet_s", float("nan")),
        ("listen_hold_s", -1),
        ("listen_hold_s", float("inf")),
        ("listen_ask_s", 0),
        ("listen_ask_s", float("nan")),
        ("listen_ask_s", 3601),
        ("listen_ask_s", True),
        ("listen_max_wakes", True),
        ("listen_max_wakes", 0),
    ],
)
def test_invalid_listening_settings_are_refused(setting: str, value: object) -> None:
    """A malformed timing or cap must never silently remove the bound."""
    from switchboard.config import ConfigError

    with pytest.raises(ConfigError):
        Config().with_delivery(**{setting: value})


@pytest.mark.parametrize("listening", [False, True])
def test_replayed_status_meeting_obeys_the_per_call_cap(tmp_path: Path, listening: bool) -> None:
    """Replay all 123 spike lines: listening hears the key topic without spending past its cap."""
    from listen_replay import replay

    replayed = replay(15, 40, listening=listening, tmp=tmp_path)
    if listening:
        assert 0 < replayed.n_wakes <= 40
        assert replayed.delay_for("key") is not None
        assert replayed.min_budget == 60 - replayed.n_wakes
    else:
        assert replayed.n_wakes == 0


def test_a_listening_offer_counts_even_if_the_call_line_does_not_fit(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The private cap counts the cause of a wake, even when earlier agent chatter fills the batch."""
    w, call = call_world(tmp_path, clock, batch_max_msgs=1, listen_max_wakes=1)
    _, m, sink = listener(w)
    _, source = w.agent("source")
    w.store.set_listen(m.id, True, call)
    w.agent_says(source, "earlier unaddressed chatter")
    line(w, call)
    clock.advance(15)
    w.actions += w.engine.tick()
    assert woke(w, sink)
    assert "kind=agent" in result(w, sink)["text"]
    assert "upload limit" not in result(w, sink)["text"]
    assert not w.m(m).listen and w.m(m).listen_wakes == 1


def test_ordinary_chatter_does_not_spend_the_listening_cap(tmp_path: Path, clock: FakeClock) -> None:
    """An ordinary wake can carry call lines early without using their separate allowance."""
    w, call = call_world(tmp_path, clock, unaddressed_hold_s=1)
    _, m, sink = waiting(w)
    _, source = w.agent("source")
    w.store.set_listen(m.id, True, call)
    line(w, call)
    w.agent_says(source, "ordinary chatter")
    clock.advance(3)
    w.actions += w.engine.tick()
    assert woke(w, sink) and w.m(m).listen_wakes == 0


def test_an_agent_named_off_still_needs_the_web_to_enable_listening(svc: RoomService) -> None:
    """The target name 'off' cannot be confused with the optional third argument."""
    room = svc.room("#build")
    _, mid = add_agent(svc.store, room.id, "off")
    svc.store.insert_meeting(room.id, started_by="alice", platform="zoom")
    with pytest.raises(ServiceError, match="web"):
        svc.command(room.name, "/meeting listen off", CLI)
    svc.command(room.name, "/meeting listen off", WEB)
    assert svc.store.get_membership(mid).listen
    svc.command(room.name, "/meeting listen off off", CLI)
    assert not svc.store.get_membership(mid).listen
