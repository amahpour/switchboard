"""SPIKE #706 (not for merge): the voice-pacing replay runs, and shows the two facts the findings rest on."""

from __future__ import annotations

from pathlib import Path

from voice_replay import POLICIES, replay, slot_line

from switchboard.meeting.turns import INTERVAL_S


def run(name: str, fixture: str, tmp: Path, **kw):
    slot_line.clear()
    return replay(POLICIES[name], fixture, tmp=tmp, **kw)


def test_today_never_starts_two_lines_less_than_30_s_apart(tmp_path: Path) -> None:
    """The per-call interval is what makes agents' quick replies wait or get refused."""
    r = run("A today: 30 s per call", "three_way.json", tmp_path)
    starts = sorted(ln.start for ln in r.lines if ln.start is not None)
    assert all(b - a >= INTERVAL_S - 1e-6 for a, b in zip(starts, starts[1:], strict=False))
    assert r.outcomes("busy") > 0


def test_a_chain_cap_silences_a_polite_loop_but_not_its_wakes(tmp_path: Path) -> None:
    """A cap on agent lines in a row stops the speaking; agents are still woken until the budget is used."""
    r = run("D 30 s + at most 4 agent lines in a row", "polite_loop.json", tmp_path, tail_s=1200.0)
    assert r.outcomes("spoken") + r.outcomes("cut") <= 4
    assert sum(r.wakes.values()) >= 50
