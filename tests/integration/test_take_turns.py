"""Taking turns on a person's message that names nobody (DESIGN.md §8.8, #507), through a real in-process
broker and scripted agents that are MCP clients of ``/mcp``.

The bug (#501 measured it with real Claude Code agents): a question that names nobody reaches every agent
as ``to_you=yes``, and all three answered it, 10 of 10 trials. Now the first agent to say() an answer takes
the reply. A later say() that is only a second answer is not posted: the agent is told who took it, shown
the answer, and may pass() or send its text again with ``since``. Nothing is silenced for good.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent, ids_in

from switchboard.config import Config

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)


@pytest.fixture
def broker(tmp_home: Path) -> Iterator[InProcBroker]:
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    r = web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers())
    assert r.status_code == 200
    b.web = web  # type: ignore[attr-defined]
    yield b
    web.close()
    b.stop()


def person_says(b: InProcBroker, text: str) -> int:
    r = b.web.post("/api/rooms/build/say", json={"text": text}, headers=b.write_headers())  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    return int(r.json()["id"])


def rows(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def posts(b: InProcBroker, sender: str) -> list[str]:
    return [r["text"] for r in rows(b, "SELECT text FROM messages WHERE sender_name=? AND kind='chat'", sender)]


async def room_of_three(b: InProcBroker, stack: Any) -> dict[str, FakeAgent]:
    agents = {}
    for i, name in enumerate(("alpha", "bravo", "charlie")):
        agent = await stack.enter_async_context(FakeAgent(b.home, f"k{i}"))
        await agent.join("#build", name)
        agents[name] = agent
    return agents


async def hears(agent: FakeAgent, question: int) -> None:
    """The agent read the room: the question is in its context, as after a wake."""
    assert question in ids_in((await agent.read("#build"))["text"])


@pytest.fixture
async def stack() -> Any:
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as s:
        yield s


async def test_the_first_answer_takes_the_reply_and_the_others_are_told(
    broker: InProcBroker, stack: Any
) -> None:
    """The scene of #501. Three agents hear a question that names nobody; alpha answers first. bravo's
    answer is not posted: the result names alpha, shows alpha's post and says how to post anyway."""
    ag = await room_of_three(broker, stack)
    q = person_says(broker, "Quick question for the room: what is 17 plus 25?")
    for a in ag.values():
        await hears(a, q)

    first = await ag["alpha"].say("#build", "17 + 25 = 42", reply_to=q)
    assert first["posted_id"] is not None

    held = await ag["bravo"].say("#build", "It is 42.", reply_to=q)
    assert held["ok"] is False and held["code"] == "taken" and held["posted_id"] is None
    assert ids_in(held["text"]) == [first["posted_id"]]  # alpha's answer, in full, from alpha
    assert "from=alpha" in held["text"] and "17 + 25 = 42" in held["text"]
    assert "alpha already answered" in held["error"] and "Yours was not posted" in held["error"]
    assert "pass()" in held["error"] and f"since={first['posted_id']}" in held["error"]
    assert held["next_since"] == first["posted_id"] and held["count"] == 1 and held["more"] is False
    assert "queued" not in held
    assert posts(broker, "bravo") == []  # not posted, not queued

    again = await ag["charlie"].say("#build", "Forty-two.")  # no reply_to: still only a second answer
    assert again["code"] == "taken" and ids_in(again["text"]) == [first["posted_id"]]
    assert posts(broker, "charlie") == []

    [ev1, ev2] = rows(broker, "SELECT data FROM events WHERE kind='reply_taken' ORDER BY id")
    assert '"by": "alpha"' in ev1["data"] and f'"question": {q}' in ev2["data"]


async def test_a_held_agent_can_pass_and_that_ends_its_turn(broker: InProcBroker, stack: Any) -> None:
    ag = await room_of_three(broker, stack)
    q = person_says(broker, "what is 17 plus 25?")
    for a in ag.values():
        await hears(a, q)
    await ag["alpha"].say("#build", "42", reply_to=q)
    held = await ag["bravo"].say("#build", "42!", reply_to=q)
    assert held["code"] == "taken"
    passed = await ag["bravo"].pass_("#build")
    assert passed["ok"] is True
    # the held say handled nothing; the pass did, and the question is closed for bravo
    [state] = rows(
        broker,
        "SELECT d.state FROM deliveries d JOIN memberships m ON m.id=d.membership_id"
        " WHERE m.screen_name='bravo' AND d.message_id=?",
        q,
    )
    assert state["state"] == "handled"
    assert posts(broker, "bravo") == []


async def test_an_agent_with_something_new_sends_it_again_with_since_and_it_posts(
    broker: InProcBroker, stack: Any
) -> None:
    """Agents keep the choice: switchboard informs, it doesn't silence. The send that names the id it was
    shown (``since``) is posted by the normal rules."""
    ag = await room_of_three(broker, stack)
    q = person_says(broker, "what is 17 plus 25?")
    for a in ag.values():
        await hears(a, q)
    first = await ag["alpha"].say("#build", "42", reply_to=q)
    held = await ag["bravo"].say("#build", "Also: 25 + 17 is the same sum.", reply_to=q)
    assert held["code"] == "taken"
    sent = await ag["bravo"].say(
        "#build", "Also: 25 + 17 is the same sum.", reply_to=q, since=held["next_since"]
    )
    assert sent["posted_id"] is not None and sent["posted_id"] > first["posted_id"]
    assert posts(broker, "bravo") == ["Also: 25 + 17 is the same sum."]


async def test_a_held_say_costs_nothing_and_counts_for_nothing(broker: InProcBroker, stack: Any) -> None:
    """A refusal is not a post: no rate limit clock starts for it (an immediate resend is not 'queued'), the
    loop guard's count does not move, and no wake is spent."""
    ag = await room_of_three(broker, stack)
    q = person_says(broker, "what is 17 plus 25?")
    for a in ag.values():
        await hears(a, q)
    await ag["alpha"].say("#build", "42", reply_to=q)
    [before] = rows(broker, "SELECT hop_count, budget_remaining FROM rooms WHERE name='#build'")
    held = await ag["bravo"].say("#build", "42", reply_to=q)
    assert held["code"] == "taken"
    [after] = rows(broker, "SELECT hop_count, budget_remaining FROM rooms WHERE name='#build'")
    assert tuple(after) == tuple(before)
    [part] = rows(broker, "SELECT last_say_at FROM participants WHERE session_key LIKE '%k1%'")
    assert part["last_say_at"] is None
    assert rows(broker, "SELECT COUNT(*) AS n FROM events WHERE kind='rate_limited'")[0]["n"] == 0


async def test_a_question_that_names_someone_is_never_held(broker: InProcBroker, stack: Any) -> None:
    """The scenario 'one named': only the agent named answers. If another says something anyway, it is not
    a second answer to a question for nobody, so it goes through."""
    ag = await room_of_three(broker, stack)
    q = person_says(broker, "@bravo what is 12 times 12?")
    for a in ag.values():
        await hears(a, q)
    assert (await ag["bravo"].say("#build", "144", reply_to=q))["posted_id"] is not None
    assert (await ag["alpha"].say("#build", "I can check that too: 144."))["posted_id"] is not None


async def test_everyone_named_by_a_person_answers(broker: InProcBroker, stack: Any) -> None:
    """``@everyone`` names every agent, so it asks each of them: nobody is made to take turns."""
    ag = await room_of_three(broker, stack)
    q = person_says(broker, "@everyone what do you each think of the plan?")
    for a in ag.values():
        await hears(a, q)
    for name, a in ag.items():
        assert (await a.say("#build", f"{name}: fine", reply_to=q))["posted_id"] is not None, name


async def test_a_reply_to_a_peer_goes_through_after_an_answer(broker: InProcBroker, stack: Any) -> None:
    """bravo disagrees with alpha's answer: a reply to alpha's message, not a second answer to the person."""
    ag = await room_of_three(broker, stack)
    q = person_says(broker, "what is 17 plus 25?")
    for a in ag.values():
        await hears(a, q)
    first = await ag["alpha"].say("#build", "41", reply_to=q)
    reply = await ag["bravo"].say("#build", "No, 42.", reply_to=first["posted_id"])
    assert reply["posted_id"] is not None


async def test_a_say_with_since_keeps_the_stale_rule_and_skips_the_hold(
    broker: InProcBroker, stack: Any
) -> None:
    """An agent that sends ``since`` is shown the newer chat by the stale rule (#297) and nothing else
    changes for it: with a current ``since`` it has seen the answer and posts."""
    ag = await room_of_three(broker, stack)
    q = person_says(broker, "what is 17 plus 25?")
    for a in ag.values():
        await hears(a, q)
    first = await ag["alpha"].say("#build", "42", reply_to=q)
    stale = await ag["bravo"].say("#build", "42", reply_to=q, since=q)
    assert stale["code"] == "stale" and ids_in(stale["text"]) == [first["posted_id"]]
    fresh = await ag["bravo"].say("#build", "42, checked", reply_to=q, since=first["posted_id"])
    assert fresh["posted_id"] is not None


async def test_an_agent_that_joined_after_the_question_is_not_held(broker: InProcBroker, stack: Any) -> None:
    """No delivery of the question, no open question: a late joiner's first say is its own."""
    ag = await room_of_three(broker, stack)
    q = person_says(broker, "what is 17 plus 25?")
    await hears(ag["alpha"], q)
    await ag["alpha"].say("#build", "42", reply_to=q)
    late = await stack.enter_async_context(FakeAgent(broker.home, "k9"))
    await late.join("#build", "delta")
    assert (await late.say("#build", "hello, I just joined"))["posted_id"] is not None


async def test_the_report_counts_the_says_that_were_held(broker: InProcBroker, stack: Any) -> None:
    """`switchboard report` lists the rule with how many says it held, and which agents were held."""
    from switchboard import report

    ag = await room_of_three(broker, stack)
    q = person_says(broker, "what is 17 plus 25?")
    for a in ag.values():
        await hears(a, q)
    await ag["alpha"].say("#build", "42", reply_to=q)
    for name in ("bravo", "charlie"):
        assert (await ag[name].say("#build", "42", reply_to=q))["code"] == "taken"
    con = report.open_ro(broker.paths.db)
    try:
        rep = report.build(con, "#build")
    finally:
        con.close()
    assert rep["rules"]["reply_taken"] == 2
    held = {a["name"]: a["says_held"] for a in rep["agents"]}
    assert held == {"alpha": 0, "bravo": 1, "charlie": 1}
    assert "reply taken" in report.render_markdown(rep)
