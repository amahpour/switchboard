"""One trial: a scenario played once against a world, then graded (DESIGN.md §12.7). Test-only.

A *world* is a throwaway broker with agents in it: the real one is Claude Code sessions in tmux
(``claude_world.py``), the one the tests use is scripted agents on an in-process broker
(``scripted.py``). ``run_trial`` is the same code for both: start the world, post the person's messages one
at a time and wait for the room to settle after each, read the record, grade it.

**Settled** is a state, not a delay: every agent is between turns, no priority message (a person's or an
@mention) is waiting or offered, and nothing in the room has moved for ``quiet_s`` (longer than the
broker's own 3 s quiet period). Chatter that addresses nobody is held by the engine for 270 s and is not
waited for.

**A harness error is not a failed check.** A timeout, a crash, a runaway stopped by the token cap or a
room paused by the loop guard says the run went wrong and not what the agents did, so the trial is
recorded as a ``harness_error`` and no check is graded; the results file counts them apart.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .graders import GRADERS
from .record import RunRecord, Tokens, quiet_state, read_record, sum_tokens
from .scenario import Scenario


class HarnessError(Exception):
    """The run went wrong, not the agents. ``kind``: ``timeout`` (a wait ran out), ``crash`` (a CLI ended),
    ``token_cap`` (a runaway), ``loop_guard`` (the room paused itself) or ``error`` (anything else)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Limits:
    join_s: float = 240.0  # for the agents to join (a world's own deadline)
    settle_s: float = 300.0  # for the room to settle after a message
    quiet_s: float = 8.0  # nothing moved for this long, with every agent idle
    poll_s: float = 0.5
    token_cap: int = 3_000_000  # all tokens, cache reads included, of every agent in the trial
    token_poll_s: float = 2.0
    budget_per_hour: int = 30  # the room's wake budget and loop guard: a runaway stops itself
    hop_limit: int = 12


class World(Protocol):
    """What a trial needs of a set of agents in a room. ``start`` raises ``HarnessError`` (or anything: it
    is recorded as one) if they can't all join."""

    db_path: Path
    human: str

    def start(self, scenario: Scenario, rules: str, limits: Limits) -> None: ...
    def say(self, text: str) -> int: ...
    def idle(self) -> bool: ...
    def tokens(self) -> dict[str, Tokens]: ...
    def stop(self) -> None: ...


@dataclass
class Trial:
    n: int
    status: str  # graded | harness_error
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: dict[str, str] | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"n": self.n, "status": self.status}
        if self.error:
            d["error"] = self.error
        if self.checks:
            d["checks"] = self.checks
        d["metrics"] = self.metrics
        return d


_HOME = re.compile(r"(/home|/Users)/[^/\s]+")


def scrub(text: str, limit: int = 500) -> str:
    """A world's error text for the results file: one paragraph, no home directory."""
    return _HOME.sub("~", " ".join(str(text).split()))[:limit]


class _Guard:
    """Polls the room and the token meter while a trial waits, and raises what makes it a harness error."""

    def __init__(self, world: World, scenario: Scenario, limits: Limits) -> None:
        self.world, self.scenario, self.limits = world, scenario, limits
        self._next_tokens = 0.0
        self.peak = Tokens()

    def tokens(self) -> dict[str, Tokens]:
        by_agent = self.world.tokens()
        self.peak = sum_tokens(by_agent.values())
        return by_agent

    def check(self) -> None:
        now = time.monotonic()
        if now >= self._next_tokens:
            self._next_tokens = now + self.limits.token_poll_s
            self.tokens()
            if self.peak.total > self.limits.token_cap:
                raise HarnessError(
                    "token_cap", f"{self.peak.total} tokens, over the cap of {self.limits.token_cap}: stopped"
                )

    def wait_settled(self, what: str) -> None:
        lim = self.limits
        deadline = time.monotonic() + lim.settle_s
        last_mark = None
        stable_since: float | None = None
        while True:
            self.check()
            q = quiet_state(self.world.db_path, self.scenario.room)
            if q.paused:
                raise HarnessError("loop_guard", f"the room paused itself ({q.paused}) {what}")
            now = time.monotonic()
            if self.world.idle() and q.open_priority == 0 and q.mark == last_mark:
                stable_since = stable_since if stable_since is not None else now
                if now - stable_since >= lim.quiet_s:
                    return
            else:
                stable_since = None
            last_mark = q.mark
            if now > deadline:
                raise HarnessError(
                    "timeout",
                    f"the room did not settle in {lim.settle_s:.0f}s {what}"
                    f" (priority deliveries left: {q.open_priority})",
                )
            time.sleep(lim.poll_s)


def metrics_of(
    rec: RunRecord, tokens_before_ask: Tokens, seconds: float, extra: dict[str, Any]
) -> dict[str, Any]:
    """The numbers every trial carries, whether or not a check failed: tokens (all of the trial's, and
    those after the person's first message), wakes and the wakes that ended in a ``pass()`` (from
    ``switchboard report``), posts, and time to settle."""
    agents = rec.report["agents"]
    total = sum_tokens(rec.tokens.values())
    per_agent = {}
    for a in agents:
        if a["name"] not in rec.agents:
            continue
        per_agent[a["name"]] = {
            "posts": a["posts"],
            "passes": a["passes"],
            "wakes": a["wakes"]["confirmed"],
            "wakes_ended_in_pass": a["passes_after_wake"],
            "wakes_unaddressed": a["wakes"]["unaddressed"],
            "tokens": rec.tokens[a["name"]].total if a["name"] in rec.tokens else 0,
        }
    first = rec.asked[0] if rec.asked else 0
    m: dict[str, Any] = {
        "seconds": round(seconds, 1),
        "tokens": total.as_dict(),
        "tokens_after_ask": (total - tokens_before_ask).as_dict(),
        "wakes": sum(v["wakes"] for v in per_agent.values()),
        "wakes_ended_in_pass": sum(v["wakes_ended_in_pass"] for v in per_agent.values()),
        "posts": len(rec.posts_after(first)),
        "passes": len(rec.passes_after(first)) if rec.asked else 0,
        "by_agent": per_agent,
    }
    m.update(extra)
    return m


def run_trial(
    world: World, scenario: Scenario, rules: str = "", limits: Limits | None = None, n: int = 1
) -> Trial:
    """Play ``scenario`` once. The world is always stopped; a harness error is returned, not raised."""
    lim = limits or Limits()
    started = time.monotonic()
    guard = _Guard(world, scenario, lim)
    asked: list[int] = []
    before = Tokens()
    settle_s = 0.0
    trial: Trial | None = None
    rec: RunRecord | None = None
    try:
        try:
            world.start(scenario, rules, lim)
            guard.wait_settled("after the agents joined")
            before = sum_tokens(guard.tokens().values())
            ask_started = time.monotonic()
            for i, step in enumerate(scenario.steps):
                asked.append(world.say(step.say))
                guard.wait_settled(f"after step {i + 1}")
            settle_s = time.monotonic() - ask_started
            final = guard.tokens()
            rec = read_record(world.db_path, scenario.room, scenario.agents, world.human, tuple(asked), final)
        except HarnessError as e:
            trial = _harness_error(n, e.kind, str(e), started, guard)
        except Exception as e:  # the live driver's waits raise AssertionError: any of it is the run's
            kind = "timeout" if "timed out" in str(e) else "error"
            trial = _harness_error(n, kind, f"{type(e).__name__}: {e}", started, guard)
    finally:
        try:
            world.stop()
        except Exception as e:  # a failed teardown must not hide the trial's own result
            print(f"evals: stopping the world failed: {type(e).__name__}: {scrub(str(e))}")
    if trial is None:
        assert rec is not None
        results = {}
        for c in scenario.checks:
            r = GRADERS[c.grader](rec, asked[c.step], **c.params)
            results[c.id] = {"ok": r.ok, "detail": r.detail}
        metrics = metrics_of(rec, before, settle_s, {"seconds_total": round(time.monotonic() - started, 1)})
        trial = Trial(n, "graded", checks=results, metrics=metrics)
    notes = getattr(world, "notes", None)  # what the world saw on its way out: prompts declined, leftovers
    if notes:
        trial.metrics["world"] = notes
    return trial


def _harness_error(n: int, kind: str, message: str, started: float, guard: _Guard) -> Trial:
    return Trial(
        n,
        "harness_error",
        error={"kind": kind, "message": scrub(message)},
        metrics={
            "seconds_total": round(time.monotonic() - started, 1),
            "tokens": guard.peak.as_dict(),
        },
    )
