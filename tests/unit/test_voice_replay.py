"""The voice slot against a scripted three-way call and a polite loop (§52.2, #713).

Both replays use the real delivery engine and the real ``Turns`` with a ``FakeClock``
(``tests/voice_replay.py``). They keep the spike's (#706) headline numbers, so a later change cannot
quietly bring back the lost lines or loosen the loop stop.
"""

from __future__ import annotations

from pathlib import Path

from voice_replay import replay, slot_line

from switchboard.meeting.turns import CHAIN_MAX, INTERVAL_S, Turns


def run(fixture: str, tmp: Path, **kw):
    slot_line.clear()
    return replay(Turns, fixture, tmp=tmp, **kw)


def test_in_a_three_way_call_no_agent_line_is_lost_and_none_waits(tmp_path: Path) -> None:
    """Today's slot refused 3 of 10 lines (the next agent replied while the last was still waiting out the
    call's 30 s). With the answer rule, a line queued behind a spoken one and a per-agent interval, all ten
    are said (one is cut off when alice talks over it) and none waits a second. Fails if any line is
    refused, dropped as stale, or held back."""
    r = run("three_way.json", tmp_path)
    assert len(r.lines) == 10
    assert r.outcomes("spoken") + r.outcomes("cut") == 10
    assert r.outcomes("busy") == r.outcomes("stale") == r.outcomes("chain") == 0
    assert max(r.waits()) < 1.0


def test_a_slower_and_a_faster_agent_also_lose_no_line(tmp_path: Path) -> None:
    """The turn length of the agents (5 s and 20 s, against 10 s) must not bring the loss back."""
    for turn_s in (5.0, 20.0):
        (tmp_path / str(turn_s)).mkdir()
        r = run("three_way.json", tmp_path / str(turn_s), turn_s=turn_s)
        assert r.outcomes("spoken") + r.outcomes("cut") == 10, turn_s


def test_a_polite_loop_is_spoken_for_four_lines_and_then_stops(tmp_path: Path) -> None:
    """Two agents thanking each other forever. The run of agent lines in a row is capped, so only
    CHAIN_MAX lines are spoken. The waking goes on until the room's hourly budget (60) is gone; that is
    the engine's rule, in phase 2. Fails if the speaking is unbounded."""
    r = run("polite_loop.json", tmp_path, tail_s=1200.0)
    assert CHAIN_MAX == 4
    assert r.outcomes("spoken") + r.outcomes("cut") == 4
    assert r.outcomes("chain") > 0


def test_no_agent_starts_two_of_its_own_unmarked_lines_less_than_thirty_seconds_apart() -> None:
    """The 30 s stays for a line that answers no one: it is the agent's own interval (unit-level, no
    replay), so a chatty agent still cannot speak more than twice a minute."""
    t = Turns()
    t.offer("one", 0, 1)
    t.finish(t.take(0).id)
    t.offer("two", 1, 1)
    assert t.take(INTERVAL_S - 0.01) is None
    assert t.take(INTERVAL_S).text == "two"


def test_a_polite_loop_stops_waking_agents_after_eight_messages(tmp_path: Path) -> None:
    """Without the engine rule (#713, phase 2) the loop woke the two agents 60 times in 10 minutes, until the
    room's hourly budget was gone, a paid model turn each. Now the 8th agent message in a row stops it: 8
    wakes, 52 of the 60 left. Fails if the loop runs on, or if the rule ever stops a normal call (the
    three-way test above keeps its 10 lines)."""
    r = run("polite_loop.json", tmp_path, tail_s=1200.0)
    assert sum(r.wakes.values()) == 8
    assert r.min_budget == 52
    assert r.paused_at is None  # it holds the room's agents, it does not pause the room
