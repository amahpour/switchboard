"""The evals' checks and runner, against scripted agents: no model (DESIGN.md §12.7).

``tests/evals`` plays a scripted person against real Claude Code agents and grades each run from the
broker's database. Here the same runner, the same scenario files and the same graders run against
``FakeAgent``s that follow a policy, on an in-process broker, so that:

- **every check is seen to fail and to pass.** For each check, one scripted room that fails it and one
  that passes it (``test_every_check_passes_and_fails``). A check that can't fail, or that never passes, is
  found here and not after a paid run. ``test_every_grader_has_a_scripted_room`` fails when a check is added
  without its pair.
- **a harness error is not a failed check**, and **the token cap stops a runaway**: a timeout, a crash, a
  runaway or a room the loop guard paused is recorded apart, with no check graded.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1] / "evals"
sys.path.insert(0, str(EVALS))

from evalkit import graders, results, scenario  # noqa: E402
from evalkit.scripted import Item, Move, Policy, ScriptedWorld  # noqa: E402
from evalkit.trial import Limits, run_trial  # noqa: E402

SCENARIOS = scenario.load_all()
# quick: a scripted room settles in under a second, and nothing here waits on a model
FAST = Limits(settle_s=40.0, quiet_s=0.6, poll_s=0.05, token_poll_s=0.05, budget_per_hour=1000, hop_limit=0)


# ---------------------------------------------------------------- policies
def _person(items: list[Item]) -> Item | None:
    return next((i for i in items if i.kind == "human"), None)


def passes(name: str, items: list[Item]) -> Move:
    return Move(passes=True)


def answers(text: str, when: str = "always") -> Policy:
    """Answer the person's message with ``text``; ``when="to_you"`` only if it is addressed to this agent,
    ``"first"`` only if this agent's name sorts first. Anything else is a pass."""

    def policy(name: str, items: list[Item]) -> Move:
        p = _person(items)
        if p is None or (when == "to_you" and not p.to_you) or (when == "first" and name != "alpha"):
            return Move(passes=True)
        return Move(say=text, reply_to=p.id)

    return policy


def alpha_delegates(name: str, items: list[Item]) -> Move:
    """Asked by the person, asks bravo for a number; when bravo answers, tells the person."""
    from_bravo = [i for i in items if i.sender == "bravo" and i.to_you]
    if from_bravo:
        return Move(say="bravo picked 7", reply_to=from_bravo[-1].id)
    p = _person(items)
    if p is not None and p.to_you:
        return Move(say="@bravo pick a number from 1 to 9", reply_to=p.id)
    return Move(passes=True)


def bravo_trades(name: str, items: list[Item]) -> Move:
    asks = [i for i in items if i.sender == "alpha" and i.to_you]
    if asks:
        return Move(say="@alpha 7", reply_to=asks[-1].id)
    return Move(passes=True)


def play(name: str, policies: dict[str, Policy], rules: str = "", limits: Limits = FAST):
    sc = SCENARIOS[name]
    return run_trial(ScriptedWorld(policies), sc, rules, limits)


def verdicts(trial) -> dict[str, bool]:
    assert trial.status == "graded", trial.error
    return {cid: r["ok"] for cid, r in trial.checks.items()}


# (scenario, the room's policies, what each check says). Each check of each scenario passes in one row and
# fails in another; the pass rows pass every check of their scenario.
ROOMS = {
    "nobody named, one answers": (
        "nobody-named",
        {"alpha": answers("42"), "bravo": passes, "charlie": passes},
        {"exactly_one_answers": True, "the_answer_is_right": True},
    ),
    "nobody named, all three answer": (
        "nobody-named",
        {n: answers("42") for n in ("alpha", "bravo", "charlie")},
        {"exactly_one_answers": False, "the_answer_is_right": True},
    ),
    "nobody named, nobody answers": (
        "nobody-named",
        {n: passes for n in ("alpha", "bravo", "charlie")},
        {"exactly_one_answers": False, "the_answer_is_right": False},
    ),
    "nobody named, the one answer is wrong": (
        "nobody-named",
        {"alpha": answers("no idea"), "bravo": passes, "charlie": passes},
        {"exactly_one_answers": True, "the_answer_is_right": False},
    ),
    "one named, only that agent answers": (
        "one-named",
        {n: answers("144", when="to_you") for n in ("alpha", "bravo", "charlie")},
        {"named_agent_answers": True, "others_do_not_post": True},
    ),
    "one named, it passes": (
        "one-named",
        {n: passes for n in ("alpha", "bravo", "charlie")},
        {"named_agent_answers": False, "others_do_not_post": True},
    ),
    "one named, everyone answers": (
        "one-named",
        {n: answers("144") for n in ("alpha", "bravo", "charlie")},
        {"named_agent_answers": True, "others_do_not_post": False},
    ),
    "bystander, alpha and bravo trade": (
        "bystander",
        {"alpha": alpha_delegates, "bravo": bravo_trades, "charlie": passes},
        {"alpha_and_bravo_trade": True, "charlie_posts_nothing": True},
    ),
    "bystander, nobody trades": (
        "bystander",
        {n: passes for n in ("alpha", "bravo", "charlie")},
        {"alpha_and_bravo_trade": False, "charlie_posts_nothing": True},
    ),
    "bystander, charlie joins in": (
        "bystander",
        {"alpha": alpha_delegates, "bravo": bravo_trades, "charlie": answers("I can help")},
        {"alpha_and_bravo_trade": True, "charlie_posts_nothing": False},
    ),
}


@pytest.mark.parametrize("room", ROOMS)
def test_every_check_passes_and_fails(room: str) -> None:
    """Each scenario's checks give the verdict the scripted room earns them, from the database alone. Fails
    if a check passes a room it should fail (a double answer, an answer from nobody, a wrong one, a
    bystander that spoke, a trade that never happened) or fails one it should pass; see the next test for
    the proof that every check is in a failing room and a passing one."""
    name, policies, expected = ROOMS[room]
    assert verdicts(play(name, policies)) == expected


def test_every_grader_has_a_scripted_room_that_fails_it_and_one_that_passes_it() -> None:
    """Adding a check means adding the rooms that show it: this fails for a grader a scenario uses that no
    row of ``ROOMS`` both fails and passes. (A grader no scenario uses is found by its own test below.)"""
    outcomes: dict[str, set[bool]] = {g: set() for g in graders.GRADERS}
    for name, _policies, expected in ROOMS.values():
        for c in SCENARIOS[name].checks:
            outcomes[c.grader].add(expected[c.id])
    assert outcomes == {g: {True, False} for g in graders.GRADERS}, outcomes
    used = {c.grader for sc in SCENARIOS.values() for c in sc.checks}
    assert used == set(graders.GRADERS), "a grader that no shipped scenario uses"


# ----------------------------------------------------------------- the run
def test_a_graded_trial_carries_its_numbers() -> None:
    """The trial has what the results file needs besides the verdicts: tokens after the person's message,
    the wakes and the wakes that ended in pass(), posts and time. Fails if a number is missing, or if a
    wake that ended in a pass() is not counted (a scripted agent waits in wait(), so it is also woken by
    the answer, which the real Claude Code agents' held Stop hook would hold for 270 s)."""
    t = play("nobody-named", {"alpha": answers("42"), "bravo": passes, "charlie": passes})
    assert t.status == "graded"
    m = t.metrics
    assert m["posts"] == 1 and m["passes"] >= 2, m
    assert m["wakes"] >= 3 and m["wakes_ended_in_pass"] >= 2, m
    assert m["tokens"]["total"] > 0 and m["tokens_after_ask"]["total"] == m["tokens"]["total"], m
    by = m["by_agent"]
    assert by["alpha"]["posts"] == 1 and by["alpha"]["wakes_ended_in_pass"] == 0
    assert by["bravo"]["posts"] == 0 and by["bravo"]["wakes_ended_in_pass"] >= 1
    assert m["seconds"] > 0 and m["seconds_total"] >= m["seconds"]


def test_the_rule_and_the_limits_reach_the_room() -> None:
    """The arm's custom room rule (#105) is set before the agents join, and the budget and the loop guard
    are on (``Limits``): the trial's own guard against a runaway. Fails if either isn't in the room."""
    seen: dict[str, object] = {}

    class Peek(ScriptedWorld):
        def stop(self) -> None:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                seen["row"] = con.execute(
                    "SELECT rules_text, budget_per_hour, hop_limit FROM rooms WHERE name = '#build'"
                ).fetchone()
            finally:
                con.close()
            super().stop()

    rule = "When nobody is named, only the agent whose name sorts first answers."
    lim = Limits(settle_s=40.0, quiet_s=0.6, poll_s=0.05, token_poll_s=0.05, budget_per_hour=77, hop_limit=9)
    world = Peek({"alpha": answers("42"), "bravo": passes, "charlie": passes})
    assert run_trial(world, SCENARIOS["nobody-named"], rule, lim).status == "graded"
    assert seen["row"] == (rule, 77, 9)


def test_what_the_world_saw_on_its_way_out_is_in_the_trial() -> None:
    """A world's notes (prompts it declined, processes left behind) are kept with the trial, graded or not.
    Fails if they are dropped on either path."""

    class Noted(ScriptedWorld):
        notes = {"stray_prompts_declined": 2, "leftover_processes": []}

    graded = run_trial(
        Noted({n: passes for n in ("alpha", "bravo", "charlie")}), SCENARIOS["nobody-named"], "", FAST
    )
    assert graded.status == "graded" and graded.metrics["world"] == Noted.notes
    broke = run_trial(Noted({}, fail_to_join=True), SCENARIOS["nobody-named"], "", FAST)
    assert broke.status == "harness_error" and broke.metrics["world"] == Noted.notes


# ------------------------------------------------------------ harness errors
def test_a_harness_error_is_counted_apart_and_grades_nothing() -> None:
    """A world that can't start (a CLI that never joined) is a harness error: no check is graded, and
    ``summarize`` leaves it out of every pass rate. Fails if a timeout is counted as a failed check."""
    t = run_trial(ScriptedWorld({}, fail_to_join=True), SCENARIOS["nobody-named"], "", FAST)
    assert t.status == "harness_error" and t.error is not None and t.error["kind"] == "timeout"
    assert t.checks == {}
    failed = play("nobody-named", {n: passes for n in ("alpha", "bravo", "charlie")})
    s = results.summarize([failed.as_dict(), t.as_dict()])
    assert s["trials"] == 2 and s["graded"] == 1 and s["harness_errors"] == 1
    assert s["checks"]["exactly_one_answers"] == {"passed": 0, "graded": 1}, "the error was graded"
    assert s["harness_error_kinds"] == {"timeout": 1}


def test_an_agent_that_crashes_is_a_harness_error() -> None:
    """An agent that ends mid-trial (here a policy that raises) is the run's fault, not a check's: kind
    ``crash``, nothing graded."""

    def boom(name: str, items: list[Item]) -> Move:
        raise RuntimeError("the CLI exited")

    t = play("nobody-named", {"alpha": boom, "bravo": passes, "charlie": passes})
    assert t.status == "harness_error" and t.error is not None
    assert t.error["kind"] == "crash" and "the CLI exited" in t.error["message"]
    assert t.checks == {}


# ---------------------------------------------------- a runaway, and its cap
PING_PONG = scenario.parse(
    {
        "name": "ping-pong",
        "room": "#build",
        "agents": ["alpha", "bravo"],
        "steps": [{"say": "@alpha say hello to @bravo"}],
        "checks": [{"id": "none_posts", "grader": "agents_do_not_post", "agents": ["alpha", "bravo"]}],
    }
)


def chatter(other: str) -> Policy:
    """Answer anything addressed to this agent by addressing the other one: two agents that never stop."""

    def policy(name: str, items: list[Item]) -> Move:
        asks = [i for i in items if i.to_you]
        return Move(say=f"@{other} and again", reply_to=asks[-1].id) if asks else Move(passes=True)

    return policy


def runaway(limits: Limits) -> tuple:
    started = time.monotonic()
    world = ScriptedWorld({"alpha": chatter("bravo"), "bravo": chatter("alpha")}, tokens_per_batch=1_000)
    t = run_trial(world, PING_PONG, "", limits)
    return t, time.monotonic() - started, world


def test_the_token_cap_stops_a_runaway() -> None:
    """Two agents that answer each other without end are stopped by the cap on tokens, as a harness error
    of kind ``token_cap`` with what they had used. Fails if the cap isn't enforced: the trial would run to
    the settle timeout and be called a ``timeout``, spending until then."""
    lim = Limits(settle_s=60.0, quiet_s=0.6, poll_s=0.05, token_poll_s=0.05, token_cap=8_000,
                 budget_per_hour=100_000, hop_limit=0)  # fmt: skip
    t, took, world = runaway(lim)
    assert t.status == "harness_error" and t.error is not None
    assert t.error["kind"] == "token_cap", t.error
    assert t.checks == {}
    assert t.metrics["tokens"]["total"] > 8_000, t.metrics["tokens"]
    assert world.stopped and took < 30, "the world was left running"


def test_a_room_the_loop_guard_paused_is_a_harness_error() -> None:
    """The room's own hop limit stops the same runaway first: it pauses the room, which the trial reports
    as ``loop_guard`` at once and does not wait out the settle timeout."""
    lim = Limits(settle_s=60.0, quiet_s=0.6, poll_s=0.05, token_poll_s=0.05, token_cap=10**9,
                 budget_per_hour=100_000, hop_limit=4)  # fmt: skip
    t, took, _world = runaway(lim)
    assert t.status == "harness_error" and t.error is not None and t.error["kind"] == "loop_guard", t.error
    assert took < 30


def test_a_room_that_never_settles_times_out() -> None:
    """With no cap and no loop guard a runaway ends at the settle deadline: kind ``timeout``, ungraded."""
    lim = Limits(settle_s=3.0, quiet_s=0.6, poll_s=0.05, token_poll_s=0.05, token_cap=10**9,
                 budget_per_hour=100_000, hop_limit=0)  # fmt: skip
    t, _took, _world = runaway(lim)
    assert t.status == "harness_error" and t.error is not None and t.error["kind"] == "timeout", t.error
    assert t.checks == {}
