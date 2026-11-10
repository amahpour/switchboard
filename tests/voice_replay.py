"""SPIKE #706, not for merge: agents answering each other by voice, replayed through the real engine.

A scripted call (``fixtures/calls/three_way.json``: alice, a person, and two listening agents, alpha and
beta, about 10 minutes) goes through the real delivery engine with a ``FakeClock`` (``World``), as in
``listen_replay.py`` (#694). New here: when an agent is woken it answers from a script (``reactions``), the
answer is posted to the room at once, as ``say()`` does, then offered to the voice slot, which decides when
it is spoken. The slot is the real ``meeting.turns.Turns`` for today's rules, and a subclass for each variant.
The person's lines are fixed in time; they do not wait for the agents. A spoken agent line that overlaps
one of them is cut off, as joinly does when it hears speech.

Run ``uv run python tests/voice_replay.py`` (through ``sb-suite``) to print the tables.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from conftest import FakeClock
from engine_agents import result, waiting
from engine_world import World

from switchboard.config import Config
from switchboard.meeting.turns import GAP_S, INTERVAL_S, STALE_S, Line, Turns

CALLS = Path(__file__).resolve().parent / "fixtures" / "calls"
WORDS_PER_S = 2.2
SPEECH_START_S = 0.7  # synthesis before the first sound
LAG_S = 4.0  # from a person stopping to their line being in the room
TICK_S = 1.0
CUT_S = 0.5  # joinly stops a line this long after it hears speech


# --------------------------------------------------------------------------- the voice slot, and variants
@dataclass(frozen=True)
class VLine(Line):
    interval_s: float = INTERVAL_S


@dataclass
class Variant(Turns):
    """Today's slot plus the knobs under test. With every knob at its default it behaves as ``Turns``."""

    interval_s: float = INTERVAL_S
    answer_interval_s: float | None = None  # B: a line answering a question put to its speaker
    per_agent: bool = False  # C: the interval is each agent's own, not the call's
    queue_behind_active: bool = False  # C: a line may wait while another is spoken
    chain_cap: int | None = None  # D: agent lines in a row (since a person last spoke)
    chain: int = 0
    starts: dict[int | None, float] = field(default_factory=dict)

    def hear(self, now: float) -> None:
        super().hear(now)
        self.chain = 0

    def offer(  # type: ignore[override]
        self, text: str, now: float, membership_id: int | None = None, *, answering: bool = False
    ) -> bool:
        self._release_lost(now)
        if self.chain_cap is not None and self.chain >= self.chain_cap:
            self.refusal = "chain"
            return False
        if self.pending is not None or (self.active is not None and not self.queue_behind_active):
            self.refusal = "busy"
            return False
        self.sequence += 1
        ival = self.answer_interval_s if answering and self.answer_interval_s is not None else self.interval_s
        self.pending = VLine(self.sequence, text, now, membership_id, ival)
        self.chain += 1
        return True

    refusal: str = "busy"

    def ready_at(self) -> float | None:
        p = self.pending
        if p is None:
            return None
        last = self.starts.get(p.membership_id) if self.per_agent else self.last_started
        return max(
            p.queued_at,
            self.last_heard + GAP_S if self.last_heard is not None else p.queued_at,
            last + p.interval_s if last is not None else p.queued_at,  # type: ignore[attr-defined]
        )

    def take(self, now: float) -> Line | None:
        if self.active is not None:
            return None
        at = self.ready_at()
        if at is None or now < at or self.pending is None or now - self.pending.queued_at >= STALE_S:
            return None
        self.active = self.clear_pending()
        self.last_started = now
        self.starts[self.active.membership_id] = now
        return self.active


class Today:
    """The real, unchanged ``Turns`` behind the same call shape."""

    def __init__(self) -> None:
        self.t = Turns()
        self.refusal = "busy"

    def __getattr__(self, k: str):  # hear, ready_at, expire, take, finish, pending, active
        return getattr(self.t, k)

    def offer(
        self, text: str, now: float, membership_id: int | None = None, *, answering: bool = False
    ) -> bool:
        return self.t.offer(text, now, membership_id)


POLICIES = {
    "A today: 30 s per call": lambda: Today(),
    "A2 15 s per call": lambda: Variant(interval_s=15.0),
    "A3 10 s per call": lambda: Variant(interval_s=10.0),
    "A4 5 s per call": lambda: Variant(interval_s=5.0),
    "B 30 s, 2 s for an answer": lambda: Variant(answer_interval_s=2.0),
    "C 30 s per agent, line may queue": lambda: Variant(per_agent=True, queue_behind_active=True),
    "D 30 s + at most 4 agent lines in a row": lambda: Variant(chain_cap=4),
    "E B + C + D(4)": lambda: Variant(
        answer_interval_s=2.0, per_agent=True, queue_behind_active=True, chain_cap=4
    ),
    "E3 B + C + D(3)": lambda: Variant(
        answer_interval_s=2.0, per_agent=True, queue_behind_active=True, chain_cap=3
    ),
    "E6 B + C + D(6)": lambda: Variant(
        answer_interval_s=2.0, per_agent=True, queue_behind_active=True, chain_cap=6
    ),
}


# ------------------------------------------------------------------------------------------ the script
@dataclass(frozen=True)
class Utt:
    index: int
    text: str
    tag: str
    to: str
    start: float
    end: float
    posted: float


def load(name: str) -> tuple[list[Utt], list[dict]]:
    d = json.loads((CALLS / name).read_text())
    utts = []
    for i, r in enumerate(d["lines"]):
        end = r["at"] + len(r["text"].split()) / WORDS_PER_S
        utts.append(Utt(i, r["text"], r["tag"], r["to"], float(r["at"]), end, end + LAG_S))
    return utts, d["reactions"]


@dataclass
class AgentLine:
    agent: str
    tag: str
    said: float
    queued: bool = False
    outcome: str = ""  # spoken | cut | busy | chain | stale
    start: float | None = None
    end: float | None = None


@dataclass
class Result:
    name: str
    lines: list[AgentLine] = field(default_factory=list)
    wakes: dict[str, int] = field(default_factory=dict)
    said_wakes: int = 0
    min_budget: int = 0
    paused_at: float | None = None
    last_wake: float = 0.0
    utts: list[Utt] = field(default_factory=list)

    def outcomes(self, o: str) -> int:
        return sum(1 for ln in self.lines if ln.outcome == o)

    def waits(self) -> list[float]:
        return [ln.start - ln.said for ln in self.lines if ln.start is not None]

    def scene(self, qtag: str, chain: list[str]) -> tuple[float | None, int]:
        """Seconds from the question ending to the last spoken (or cut off) line of its chain ending, and how
        many lines of the chain were never spoken."""
        q = next(u for u in self.utts if u.tag == qtag)
        ls = [ln for ln in self.lines if ln.tag in chain]
        done = [ln.end for ln in ls if ln.end is not None]
        missing = len(chain) - len(done)
        return (max(done) - q.end if done else None), missing  # type: ignore[type-var]


SCENES = {"q1": ["r1", "r2", "r3", "r4"], "q2": ["s1", "s2", "s3"], "q3": ["t1", "t2"], "q4": ["u1"]}


def replay(
    make_slot,
    fixture: str = "three_way.json",
    *,
    turn_s: float = 10.0,
    tail_s: float = 120.0,
    hop_limit: int = 0,
    budget: int = 60,
    tmp: Path,
) -> Result:
    utts, reactions = load(fixture)
    clock = FakeClock()
    t0 = clock.now()
    cfg = Config(human_name="alice").with_delivery(budget_per_hour=budget, hop_limit=hop_limit)
    w = World(tmp, clock, cfg)
    call = w.store.insert_meeting(w.room.id, started_by="alice", platform="zoom")
    w.store.set_meeting_live(call.id)
    ag: dict[str, dict] = {}
    for n in ("alpha", "beta"):
        p, m, sink = waiting(w, n)
        w.store.set_listen(m.id, True, call.id, 40)
        ag[n] = {"p": p, "m": m, "sink": sink, "back": None, "batch": []}
    mid_of = {n: ag[n]["m"].id for n in ag}
    slot = make_slot()
    out = Result("", utts=utts, min_budget=budget)
    out.wakes = {n: 0 for n in ag}
    tags: dict[int, tuple[str, str, str]] = {}  # message id -> (tag, to, sender)
    posts = sorted(utts, key=lambda u: u.posted)
    ends = sorted(u.end for u in utts)
    nxt, ne = 0, 0
    next_tick = TICK_S
    active: tuple[AgentLine, float, float] | None = None  # line, start, end
    end_of_script = max(u.posted for u in utts)
    horizon = end_of_script + tail_s
    now = 0.0
    while True:
        cands = [next_tick]
        if nxt < len(posts):
            cands.append(posts[nxt].posted)
        if ne < len(ends):
            cands.append(ends[ne])
        for a in ag.values():
            if a["back"] is not None:
                cands.append(a["back"])
        if slot.pending is not None:
            r = slot.ready_at()
            if r is not None and r > now:
                cands.append(r)
            cands.append(slot.pending.queued_at + STALE_S)
        if active is not None:
            cands.append(active[2])
        now = min(cands)
        if now > horizon:
            break
        clock.t = t0 + now
        # the person's speech reaches the bridge's clock
        while ne < len(ends) and ends[ne] <= now:
            slot.hear(now)
            ne += 1
        # the speech in progress ends
        if active is not None and active[2] <= now:
            ln, s, e = active
            slot.finish(slot.active.id)
            active = None
        # a person's line reaches the room
        while nxt < len(posts) and posts[nxt].posted <= now:
            u = posts[nxt]
            nxt += 1
            msg = w.store.insert_message(
                w.room.id,
                sender_name="alice",
                sender_kind="meeting",
                via="meeting",
                text=u.text,
                meeting_id=call.id,
                mentions=[u.to] if u.to else [],
            )
            tags[msg.id] = (u.tag, u.to, "alice")
            w.actions += w.engine.on_message(msg.id)
        # agents finish their turns
        for n, a in ag.items():
            if a["back"] is not None and a["back"] <= now:
                a["back"] = None
                w.actions += w.engine.before_call(w.p(a["p"]))
                for tag, to, sender in a["batch"]:
                    rx = next((x for x in reactions if x["agent"] == n and x["on"] == tag and to == n), None)
                    if rx is None or sender == n:
                        continue
                    msg = w.store.insert_message(
                        w.room.id,
                        sender_name=n,
                        sender_kind="agent",
                        via="mcp",
                        text=rx["say"],
                        sender_membership_id=mid_of[n],
                        sender_harness="test",
                        mentions=[rx["to"]] if rx["to"] else [],
                    )
                    tags[msg.id] = (rx["tag"], rx["to"], n)
                    w.actions += w.engine.on_message(msg.id)
                    ln = AgentLine(n, rx["tag"], now)
                    out.lines.append(ln)
                    if slot.offer(rx["say"], now, mid_of[n], answering=True):
                        ln.queued = True
                        slot_line[slot.pending.id] = ln
                    else:
                        ln.outcome = slot.refusal
                    out.said_wakes += 1
                    break
                w.store.mark_handled(a["m"].id)
                a["sink"], acts = w.engine.open_wait(w.p(a["p"]), w.m(a["m"]), f"w-{n}-{now}", 3600.0)
                w.actions += acts
        # the bridge's worker: drop a stale line, start a ready one
        dropped = slot.expire(now)
        if dropped is not None:
            slot_line.pop(dropped.id).outcome = "stale"
        taken = slot.take(now)
        if taken is not None:
            ln = slot_line.pop(taken.id)
            ln.start = now
            dur = SPEECH_START_S + len(taken.text.split()) / WORDS_PER_S
            cut = None
            for u in utts:
                if u.start < now + dur and u.end > now:
                    cut = max(now, u.start) + CUT_S
                    break
            ln.outcome = "cut" if cut else "spoken"
            ln.end = cut if cut else now + dur
            active = (ln, now, ln.end)
        if next_tick <= now:
            next_tick += TICK_S
            w.actions += w.engine.tick()
        for n, a in ag.items():
            r = result(w, a["sink"])
            if a["back"] is None and r is not None and r.get("status") == "messages":
                ids = [int(i) for i in re.findall(r"^- id=(\d+) ", r["text"], re.M)]
                a["batch"] = [tags[i] for i in ids if i in tags]
                a["back"] = now + turn_s
                out.wakes[n] += 1
                out.last_wake = now
        room = w.store.room_by_id(w.room.id)
        out.min_budget = min(out.min_budget, room.budget_remaining)
        if room.paused and out.paused_at is None:
            out.paused_at = now
        if (
            nxt >= len(posts)
            and slot.pending is None
            and active is None
            and all(a["back"] is None for a in ag.values())
            and now > end_of_script
            and not any(w.store.pending_items(a["m"].id) for a in ag.values())
        ):
            break
    return out


slot_line: dict[int, AgentLine] = {}


def fmt(x: float | None) -> str:
    return "never" if x is None else f"{x:.0f} s"


def run_one(make, fixture="three_way.json", **kw) -> Result:
    with tempfile.TemporaryDirectory() as d:
        slot_line.clear()
        return replay(make, fixture, tmp=Path(d), **kw)


def main_row(name: str, r: Result) -> str:
    ws = r.waits()
    sc = {q: r.scene(q, c) for q, c in SCENES.items()}
    cells = " / ".join(f"{fmt(t)}" + (f" (-{m})" if m else "") for t, m in sc.values())
    return (
        f"{name:42} scenes {cells} | line wait med {statistics.median(ws):.0f} max {max(ws):.0f} | "
        f"spoken {r.outcomes('spoken')} cut {r.outcomes('cut')} not spoken "
        f"{r.outcomes('busy') + r.outcomes('stale') + r.outcomes('chain')} "
        f"(busy {r.outcomes('busy')} stale {r.outcomes('stale')} chain {r.outcomes('chain')}) | wakes "
        f"{sum(r.wakes.values())} said {r.said_wakes} | budget left {r.min_budget}"
    )


def report() -> str:
    lines = []
    for turn_s in (10.0, 5.0, 20.0):
        lines.append(f"agent turn {turn_s:g} s")
        for name, make in POLICIES.items():
            lines.append(main_row(name, run_one(make, turn_s=turn_s)))
        lines.append("")
    lines.append("runaway: a polite loop, 20 minutes after a person starts it (hop_limit off / 6)")
    for hop in (0, 6):
        for name, make in POLICIES.items():
            with tempfile.TemporaryDirectory() as d:
                slot_line.clear()
                r = replay(make, "polite_loop.json", tail_s=1200.0, hop_limit=hop, tmp=Path(d))
            lines.append(
                f"hop {hop} {name:42} posted {len(r.lines)} "
                f"spoken {r.outcomes('spoken') + r.outcomes('cut')} "
                f"not spoken {r.outcomes('busy') + r.outcomes('stale') + r.outcomes('chain')} wakes "
                f"{sum(r.wakes.values())} budget left {r.min_budget} last wake {r.last_wake:.0f} s "
                f"paused {fmt(r.paused_at)}"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
    sys.exit(0)
