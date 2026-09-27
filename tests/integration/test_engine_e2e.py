"""Three scripted agents on a real broker: the loop guard pauses at 6, the budget is
enforced, and no wake is lost between turns (DESIGN.md §8, §12.2)."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from conftest import InProcBroker
from fakes.fake_agent import FakeAgent, ids_in
from switchboard.config import Config

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
LINE = re.compile(r"^- id=(\d+) .*?from=(\S+) kind=(\S+) .*?to_you=(yes|no)", re.M)


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


def say(b: InProcBroker, text: str) -> int:
    return b.web.post("/api/rooms/build/say", json={"text": text}, headers=b.write_headers()).json()["id"]


def q(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


async def run_agent(a: FakeAgent, name: str, policy: str, stop: asyncio.Event, log: list[str]) -> None:
    """always: reply to anything; mentions: reply only when @mentioned; pass: never speak."""
    while not stop.is_set():
        r = await a.wait("#build", 3)
        if r.get("status") == "paused":
            log.append(f"{name}:paused")
            return
        if r.get("status") != "messages":
            continue
        lines = LINE.findall(r["text"])
        mine = [(int(i), frm, to) for i, frm, _k, to in lines if frm != name]
        target = mine[-1] if mine else None
        speak = target is not None and (policy == "always" or (policy == "mentions" and any(t == "yes" for *_, t in mine)))
        if speak:
            other = "beta" if name == "alpha" else "alpha"
            res = await a.say("#build", f"@{other} my take on #{target[0]}", reply_to=target[0])
            log.append(f"{name}:say:{res.get('posted_id') or res.get('reason')}")
        else:
            await a.pass_("#build")
            log.append(f"{name}:pass")


async def test_loop_guard_stops_two_chatty_agents_at_six(broker: InProcBroker) -> None:
    log: list[str] = []
    stop = asyncio.Event()
    async with FakeAgent(broker.home, "a") as a, FakeAgent(broker.home, "b") as b, FakeAgent(broker.home, "c") as c:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        await c.join("#build", "gamma")
        tasks = [asyncio.create_task(run_agent(a, "alpha", "always", stop, log)),
                 asyncio.create_task(run_agent(b, "beta", "mentions", stop, log)),
                 asyncio.create_task(run_agent(c, "gamma", "pass", stop, log))]
        await asyncio.sleep(0.5)
        human = say(broker, "@alpha @beta please discuss the plan")
        done, pending = await asyncio.wait(tasks, timeout=30)
        stop.set()
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    [(paused, reason, hops)] = q(broker, "SELECT paused, paused_reason, hop_count FROM rooms WHERE name='#build'")
    assert paused == 1 and reason == "loop guard" and hops >= 6
    agent_msgs = q(broker, "SELECT sender_name FROM messages WHERE kind='chat' AND sender_kind='agent' AND id>?", human)
    assert len(agent_msgs) == 6, log
    assert {r[0] for r in agent_msgs} <= {"alpha", "beta"}  # gamma always passed
    assert q(broker, "SELECT COUNT(*) FROM events WHERE kind='loop_guard'")[0][0] == 1
    assert q(broker, "SELECT COUNT(*) FROM events WHERE kind='pass'")[0][0] >= 1
    assert any(x.endswith(":paused") for x in log)
    notices = [m for m in broker.web.get("/api/rooms/build/messages").json()["messages"] if m["kind"] == "notice"]
    assert any("loop guard" in m["text"] for m in notices)


async def test_budget_holds_chatter_but_never_the_human(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "a") as a, FakeAgent(broker.home, "b") as b:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        r = broker.web.post("/api/rooms/build/command", json={"text": "/budget 0"}, headers=broker.write_headers())
        assert r.json()["ok"]
        await b.say("#build", "chatter nobody asked for")
        assert (await a.wait("#build", 1))["status"] == "timeout"  # chatter: no wake at budget 0
        t = a.wait_task("#build", 10)
        await asyncio.sleep(0.3)
        mid = say(broker, "a human still gets through")
        res = await asyncio.wait_for(t, 5)
        assert res["status"] == "messages" and mid in ids_in(res["text"])
    assert q(broker, "SELECT COUNT(*) FROM events WHERE kind='budget_exhausted'")[0][0] == 1


async def test_no_wake_is_lost_between_turns(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "a") as a:
        await a.join("#build", "alpha")
        # the agent is busy (not waiting) when these arrive...
        ids = [say(broker, f"while you were busy {i}") for i in range(3)]
        await asyncio.sleep(0.3)
        # ...and its next wait() gets them at once, human first
        res = await asyncio.wait_for(a.wait("#build", 10), 5)
        assert res["status"] == "messages" and ids_in(res["text"]) == ids


async def test_the_watchdog_reminds_then_tells_the_human(tmp_home: Path) -> None:
    """End to end on a real broker (the runner's 1 s tick): an @mention the agent
    takes but never answers comes back once as a reminder, then the human gets a
    warn notice in the room, and the agent isn't woken for it again."""
    cfg = FAST.with_delivery(watchdog_s=1.0, watchdog_max=1)
    b = InProcBroker(tmp_home, cfg).start()
    web = b.web_client()
    try:
        assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
        b.web = web
        async with FakeAgent(b.home, "a") as a:
            await a.join("#build", "alpha")
            t = a.wait_task("#build", 10)
            await asyncio.sleep(0.3)
            mid = say(b, "@alpha what's the status?")
            first = await asyncio.wait_for(t, 5)
            assert first["status"] == "messages" and ids_in(first["text"]) == [mid]
            assert "reminder=yes" not in first["text"]
            # its next call (another wait) confirms the first; unanswered, it comes back
            again = await asyncio.wait_for(a.wait("#build", 10), 8)
            assert again["status"] == "messages" and ids_in(again["text"]) == [mid]
            assert "reminder=yes" in again["text"] and "haven't answered" in again["text"]
            last = await asyncio.wait_for(a.wait("#build", 4), 8)
            assert last["status"] == "timeout"  # escalated: no more wakes for it
            notices = [m for m in web.get("/api/rooms/build/messages").json()["messages"] if m["kind"] == "notice"]
            assert any(f"alpha hasn't answered @mention #{mid}" in m["text"] for m in notices), notices
            read = await a.read("#build")
            assert mid in ids_in(read["text"])  # still readable
        assert q(b, "SELECT COUNT(*) FROM events WHERE kind='watchdog_remind'")[0][0] == 1
        assert q(b, "SELECT COUNT(*) FROM events WHERE kind='watchdog_escalate'")[0][0] == 1
    finally:
        web.close()
        b.stop()


# ------------------------------------------------ the M6 property test (§13)
# 200 seeded random interleavings of status, message, sink and command events
# against the real engine and adapters (FakeClock, tests/engine_sim.py), then
# quiescence. Invariant: no delivery of an active membership is offered, or
# pending and wake-eligible.
@pytest.mark.parametrize("seed", range(200))
def test_property_nothing_is_stuck_after_quiescence(tmp_path: Path, seed: int) -> None:
    from engine_sim import Sim

    sim = Sim(tmp_path, seed)
    for _ in range(sim.rng.randint(40, 160)):
        sim.step()
    sim.quiesce()
    bad = sim.violations()
    assert not bad, sim.describe() + "\n" + "\n".join(bad[:20])


def test_property_simulation_exercises_every_path_and_rule(tmp_path: Path) -> None:
    """The property test is only as good as its interleavings: check that a slice of
    the seeds reaches every harness, delivery path and rule."""
    import json

    from engine_sim import Sim

    kinds: set[str] = set()
    paths: set[str] = set()
    reasons: set[str] = set()
    events: set[str] = set()
    for seed in range(30):
        (tmp_path / str(seed)).mkdir()
        sim = Sim(tmp_path / str(seed), seed)
        for _ in range(sim.rng.randint(40, 160)):
            sim.step()
        sim.quiesce()
        con = sim.store.con
        kinds |= {m.kind for m in sim.members}
        paths |= {r[0] for r in con.execute("SELECT DISTINCT path FROM batches")}
        reasons |= {r[0] for r in con.execute("SELECT DISTINCT expire_reason FROM batches") if r[0]}
        for kind, data in con.execute("SELECT kind, data FROM events"):
            events.add(kind)
            if kind == "watchdog_escalate":
                events.add(f"watchdog_escalate:{json.loads(data)['why']}")
    assert kinds == {"test", "devin", "claude", "cursor", "codex"}
    assert paths == {"wait", "read", "say", "inbox", "turn_start", "steer", "hook_ctx", "hook_ups",
                     "stop_followup", "stop_block"}
    assert {"pause", "no_ack", "no_confirm", "idle_no_token", "reroute", "send_error", "superseded",
            "unwait", "newer_read", "human_prompt", "loop_reset"} <= reasons
    assert {"loop_guard", "budget_exhausted", "rate_limited", "rearm", "requeue", "park", "watchdog_remind",
            "watchdog_escalate:unanswered", "watchdog_escalate:parked", "watchdog_escalate:not_idle",
            "cancel", "pass_refused"} <= events
