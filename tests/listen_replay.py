"""Listening: a scripted status meeting replayed through the real engine (DESIGN.md §52.1, #697).

``tests/fixtures/calls/status_meeting.json`` is a 22 minute call between test people (alice, bob and
carol): who spoke, what they said, and the silence before they started. Each utterance becomes one call line
the way the bridge posts it (§52): it is posted ``LAG_S`` after the speaker stopped (the 2.0 s pause that
closes a line, plus the 3 s poll, less luck), as a ``meeting`` message that names nobody.

``replay`` plays that into a ``World`` (store, engine, ``FakeClock``; no broker, no I/O) with one agent that
sits in ``wait()``. The engine is ticked once a second, as the broker's runner does. When the agent is woken
it is busy for ``turn_s`` (a model turn: read, think, pass), then it passes and waits again. Nothing sleeps:
the clock jumps from event to event.

Reused from the #694 spike; tests measure the stored switch, its own timing and the per-call cap.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from conftest import FakeClock
from engine_agents import result, waiting
from engine_world import World

from switchboard.config import Config

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "calls" / "status_meeting.json"
WORDS_PER_S = 2.2  # 130 words a minute, with the ums of a real call
LAG_S = 4.0  # from the speaker stopping to the line being in the room
TICK_S = 1.0  # the runner's tick (delivery/runner.py)
START_S = 5.0


@dataclass(frozen=True)
class Utterance:
    index: int
    speaker: str
    text: str
    tag: str
    start: float  # seconds into the call
    end: float
    posted: float  # when the line reaches the room


def script(lag_s: float = LAG_S) -> list[Utterance]:
    rows = json.loads(FIXTURE.read_text())
    out: list[Utterance] = []
    t = START_S
    for i, r in enumerate(rows):
        start = t + float(r["gap_s"])
        end = start + len(r["text"].split()) / WORDS_PER_S
        out.append(Utterance(i, r["speaker"], r["text"], r.get("tag", ""), start, end, end + lag_s))
        t = end
    return out


@dataclass
class Wake:
    at: float  # seconds into the call
    lines: list[int]  # utterance indexes in the batch
    oldest_wait_s: float  # how long the oldest line in it had waited


@dataclass
class Replay:
    quiet_s: float
    max_hold_s: float
    listening: bool
    turn_s: float
    wakes: list[Wake] = field(default_factory=list)
    duration_s: float = 0.0
    min_budget: int = 0
    key_posted: float = 0.0
    date_posted: float = 0.0

    def wake_with(self, index: int) -> Wake | None:
        return next((w for w in self.wakes if index in w.lines), None)

    def delay_for(self, tag: str) -> float | None:
        """Seconds from the tagged line reaching the room to the wake that carries it."""
        u = next(u for u in script() if u.tag == tag)
        w = self.wake_with(u.index)
        return None if w is None else w.at - u.posted

    def lines_before_wake(self, tag: str) -> int | None:
        """Lines posted after the tagged one and before its wake: how far the talk had gone on."""
        u = next(u for u in script() if u.tag == tag)
        w = self.wake_with(u.index)
        return None if w is None else max(i for i in w.lines) - u.index

    def line_delays(self) -> list[float]:
        """For every line a wake carried: seconds from reaching the room to that wake."""
        lines = script()
        return sorted(w.at - lines[i].posted for w in self.wakes for i in w.lines)

    def delay_pct(self, q: float) -> float:
        d = self.line_delays()
        return d[min(len(d) - 1, int(q * len(d)))] if d else float("nan")

    @property
    def n_wakes(self) -> int:
        return len(self.wakes)

    @property
    def mean_batch(self) -> float:
        return sum(len(w.lines) for w in self.wakes) / len(self.wakes) if self.wakes else 0.0


def replay(
    quiet_s: float,
    max_hold_s: float,
    *,
    listening: bool = True,
    turn_s: float = 10.0,
    lag_s: float = LAG_S,
    budget_per_hour: int = 60,
    listen_max_wakes: int = 40,
    tmp: Path,
) -> Replay:
    lines = script(lag_s)
    clock = FakeClock()
    t0 = clock.now()
    root = tmp
    cfg = Config(human_name="alice").with_delivery(
        listen_quiet_s=quiet_s,
        listen_hold_s=max_hold_s,
        budget_per_hour=budget_per_hour,
        listen_max_wakes=listen_max_wakes,
    )
    w = World(root, clock, cfg)
    p, m, sink = waiting(w, "alpha")
    call = w.store.insert_meeting(w.room.id, started_by="alice", platform="zoom")
    w.store.set_meeting_live(call.id)
    if listening:
        w.store.set_listen(m.id, True, call.id, listen_max_wakes)
    out = Replay(quiet_s, max_hold_s, listening, turn_s, min_budget=budget_per_hour)
    ids: dict[int, int] = {}  # message id -> utterance index
    posts = sorted(lines, key=lambda u: u.posted)
    nxt = 0
    next_tick = TICK_S
    back_at: float | None = None  # when the agent's turn ends
    end_of_call = posts[-1].posted
    horizon = end_of_call + 300.0  # let the tail drain after the call
    now = 0.0
    while True:
        cands = [next_tick]
        if nxt < len(posts):
            cands.append(posts[nxt].posted)
        if back_at is not None:
            cands.append(back_at)
        now = min(cands)
        if now > horizon:
            break
        clock.t = t0 + now
        if nxt < len(posts) and posts[nxt].posted <= now:
            u = posts[nxt]
            nxt += 1
            msg = w.store.insert_message(
                w.room.id,
                sender_name=u.speaker,
                sender_kind="meeting",
                via="meeting",
                text=u.text,
                meeting_id=call.id,
            )
            ids[msg.id] = u.index
            w.actions += w.engine.on_message(msg.id)
        if back_at is not None and back_at <= now:
            back_at = None
            w.actions += w.engine.before_call(w.p(p))  # its first call confirms the batch
            w.store.mark_handled(m.id)  # pass()
            sink, acts = w.engine.open_wait(w.p(p), w.m(m), f"w-{now}", 3600.0)
            w.actions += acts
        if next_tick <= now:
            next_tick += TICK_S
            w.actions += w.engine.tick()
        r = result(w, sink)
        if back_at is None and r is not None and r.get("status") == "messages":
            got = [ids[int(i)] for i in re.findall(r"^- id=(\d+) ", r["text"], re.M) if int(i) in ids]
            oldest = min((lines[i].posted for i in got), default=now)
            out.wakes.append(Wake(now, sorted(got), now - oldest))
            back_at = now + turn_s
        room = w.store.room_by_id(w.room.id)
        assert room is not None
        out.min_budget = min(out.min_budget, room.budget_remaining) if out.wakes else out.min_budget
        if nxt >= len(posts) and back_at is None and not w.store.pending_items(m.id) and now > end_of_call:
            break
        if nxt >= len(posts) and now > end_of_call + 120 and back_at is None:
            break
    out.duration_s = end_of_call
    out.key_posted = next(u.posted for u in lines if u.tag == "key")
    out.date_posted = next(u.posted for u in lines if u.tag == "date")
    room = w.store.room_by_id(w.room.id)
    assert room is not None
    out.min_budget = min(out.min_budget, room.budget_remaining)
    return out


def table(rows: list[Replay]) -> str:
    head = (
        "| quiet_s | max_hold_s | wakes | lines per wake | key line to wake | talk past the key line |"
        " date line to wake | median line delay | p90 line delay | lines never woken for | budget left |"
    )
    sep = "|---|---|---|---|---|---|---|---|---|---|---|"
    body = []
    for r in rows:
        kd, kl, dd = r.delay_for("key"), r.lines_before_wake("key"), r.delay_for("date")
        body.append(
            f"| {r.quiet_s:g} | {r.max_hold_s:g} | {r.n_wakes} | {r.mean_batch:.1f} | "
            f"{'-' if kd is None else f'{kd:.0f} s'} | {'-' if kl is None else f'{kl} lines'} | "
            f"{'-' if dd is None else f'{dd:.0f} s'} | {r.delay_pct(0.5):.0f} s | {r.delay_pct(0.9):.0f} s | "
            f"{len(script()) - len(r.line_delays())} | {r.min_budget} |"
        )
    return "\n".join([head, sep, *body])
