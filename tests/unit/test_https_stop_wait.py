"""A Stop wait that its own side ended, for a session over HTTPS (DESIGN.md §49.4, #449), with a
FakeClock: Codex gives up on the call of its Stop hook for Esc and for its ``tool_timeout_sec``, and
nothing can wake it until its next hook. The adapter says what Members shows (``stop_wait_lost``); the
engine, which names no harness, keeps it until then."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard.adapters import HTTPS_CLAUDE, HTTPS_CODEX, HTTPS_CURSOR
from switchboard.adapters.https import _held
from switchboard.config import Config
from switchboard.models import HookEvent


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def https_session(w: World, harness: str, name: str, key: str) -> tuple[Any, Any]:
    """An idle session over HTTPS (§49.3): its adapter is picked by the session's device."""
    p, m = w.agent(name, harness=harness, status="idle", hooks=True, host="laptop")
    assert w.engine.adapter(p) is w.engine.adapters[key]
    return p, m


def stop_wait(w: World, p: Any, conn_id: int) -> Any:
    """The park a Stop hook that may wait about a day opens (``wait_s`` 86390, less the margin)."""
    ev = HookEvent(harness=p.harness, event="Stop", max_wait_s=86390)
    sink, acts = w.engine.open_park(w.p(p), ev, 86385, conn_id=conn_id)
    w.actions += acts
    return sink


def test_a_codex_whose_stop_wait_ended_early_is_not_listening_until_its_next_hook(
    w: World, clock: FakeClock
) -> None:
    """Esc, or Codex's ``tool_timeout_sec`` (5 minutes unless the setup raises it: #449), ends the call
    of the Stop hook. Members must then say the session is not listening, with how long the wait
    lasted, even with nothing waiting for it, and stop saying so at its next hook. Fails if a closed
    Stop wait leaves an idle Codex looking as if it listens, or if the reason outlives the next hook."""
    p, m = https_session(w, "codex", "codex-1", HTTPS_CODEX)
    sink = stop_wait(w, p, conn_id=7)
    assert w.engine.parked_reason(m.id) is None  # waiting: listening

    clock.advance(301)
    w.actions += w.engine.close_conn_sinks(7)
    assert not w.engine.sinks.get(sink.id).open
    why = w.engine.parked_reason(m.id) or ""
    assert why.startswith("not listening: its Stop hook ended after 5 min"), why
    assert "tool_timeout_sec" in why and "Esc" in why

    msg = w.human("@codex-1 are you there?")
    assert w.engine.parked_reason(m.id) == why  # the better reason, not "no Stop hook waiting"
    assert w.states(m)[msg.id] == "pending"

    w.hook(p, "UserPromptSubmit")
    assert w.engine.parked_reason(m.id) != why  # its next hook of any kind


def test_a_stop_hook_that_waits_again_is_listening_whatever_was_left_behind(w: World) -> None:
    """The reason is for a session with no wait open. Fails if a Codex that waits again, after
    a cancelled wait, is still shown as not listening."""
    p, m = https_session(w, "codex", "codex-1", HTTPS_CODEX)
    stop_wait(w, p, conn_id=7)
    w.actions += w.engine.close_conn_sinks(7)
    assert "not listening" in (w.engine.parked_reason(m.id) or "")
    stop_wait(w, p, conn_id=8)  # the park of a new Stop call, with nothing yet said by a hook
    assert w.engine.parked_reason(m.id) is None


def test_a_subagents_hook_does_not_bring_a_not_listening_codex_back(w: World) -> None:
    """Codex runs a subagent's tool hooks with the session's id (#446, #465), and the engine ignores
    them. A late one of those must not end the reason: the session has not heard anything. Fails if
    any hook with the session's id clears it, not only the session's own."""
    p, m = https_session(w, "codex", "codex-1", HTTPS_CODEX)
    w.store.update_participant(p.id, gen="turn-1")
    stop_wait(w, p, conn_id=7)
    w.actions += w.engine.close_conn_sinks(7)
    why = w.engine.parked_reason(m.id)
    assert why and "not listening" in why
    w.hook(p, "PostToolUse", gen="turn-of-a-subagent", tool="Bash", tool_use_id="call_1")
    assert w.engine.parked_reason(m.id) == why  # the subagent's, ignored
    w.hook(p, "UserPromptSubmit", gen="turn-2")
    assert w.engine.parked_reason(m.id) != why  # the session's own


def test_a_session_that_moved_on_before_its_wait_closed_is_not_marked(w: World, clock: FakeClock) -> None:
    """A wait cancelled after the session's next turn began (the person typed) says nothing: the
    session is busy, and an old reason must not come back at its next Stop. Fails if a busy
    session is remembered as not listening."""
    p, m = https_session(w, "codex", "codex-1", HTTPS_CODEX)
    stop_wait(w, p, conn_id=7)
    w.store.set_status(p.id, "busy", "test")
    w.actions += w.engine.close_conn_sinks(7)
    assert w.engine.lost_stops == {}
    w.store.set_status(p.id, "idle", "test")
    assert w.engine.parked_reason(m.id) is None


@pytest.mark.parametrize(("harness", "key"), [("claude", HTTPS_CLAUDE), ("cursor", HTTPS_CURSOR)])
def test_only_codex_over_https_is_marked_not_listening_for_a_closed_stop_wait(
    w: World, harness: str, key: str
) -> None:
    """Claude Code's listener comes back by itself after a dropped round (§49.4), and a Cursor session
    is followed up at its stop hook, so a closed wait means nothing for them. Fails if the rule
    leaks past the adapter that asks for it (the engine names no harness)."""
    p, m = https_session(w, harness, f"{harness}-1", key)
    stop_wait(w, p, conn_id=7)
    w.actions += w.engine.close_conn_sinks(7)
    assert w.engine.lost_stops == {} and w.engine.parked_reason(m.id) is None


def test_leaving_the_last_room_forgets_it(w: World) -> None:
    """The reason is memory only and belongs to a session in a room. Fails if it is kept for a
    session that left, to come back with it."""
    p, m = https_session(w, "codex", "codex-1", HTTPS_CODEX)
    stop_wait(w, p, conn_id=7)
    w.actions += w.engine.close_conn_sinks(7)
    assert w.engine.lost_stops
    w.store.end_membership(m.id, "leave", kicked=False)
    w.actions += w.engine.on_membership_ended(m.id, "leave")
    assert w.engine.lost_stops == {}


@pytest.mark.parametrize(
    ("secs", "text"),
    [
        (0.2, "1 s"),
        (40, "40 s"),
        (89, "89 s"),
        (90, "2 min"),
        (301, "5 min"),
        (5399, "90 min"),
        (5400, "2 h"),
        (86385, "24 h"),
    ],
)
def test_how_long_a_wait_lasted_is_said_in_a_word_or_two(secs: float, text: str) -> None:
    assert _held(secs) == text
