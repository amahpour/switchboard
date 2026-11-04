"""A claim is also what its holder watches (DESIGN.md §8.1, §47, #541), through a real in-process broker
and scripted agents that are MCP clients of ``/mcp``.

claude-1 claims ``owner/repo#12``. A message from another agent that names it wakes claude-1 as an
@mention would: ``to_you=yes``, ``prio=mention``, at once. The same text with another number, after the
claim is released, or from claude-1 itself does not.

Nothing here sleeps to mean "by now". The broker's quiet period and holds are an hour (and the say rate
limit is off, so one agent can post twice), so chatter wakes nobody in the time a test takes, and every
wake asserted is the one an awaited ``wait()`` returned.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent, ids_in

from switchboard.config import Config

HOUR = 3600.0
QUIET = Config(human_name="alice").with_delivery(
    quiet_s=HOUR, max_hold_s=HOUR, unaddressed_hold_s=HOUR, rate_limit_s=0.0
)
KEY = "owner/repo#12"


@pytest.fixture
def broker(tmp_home: Path) -> Iterator[InProcBroker]:
    b = InProcBroker(tmp_home, QUIET).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web  # type: ignore[attr-defined]
    yield b
    web.close()
    b.stop()


def classified(b: InProcBroker, name: str, message: int) -> tuple[int, int] | None:
    """(prio, mentioned) of the delivery row ``name`` got for ``message``; None when it got no row."""
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    try:
        r = con.execute(
            "SELECT d.prio, d.mentioned FROM deliveries d JOIN memberships m ON m.id=d.membership_id"
            " WHERE m.screen_name=? AND d.message_id=?",
            (name, message),
        ).fetchone()
    finally:
        con.close()
    return (r[0], r[1]) if r else None


def person_says(b: InProcBroker, text: str, room: str = "build") -> int:
    r = b.web.post(f"/api/rooms/{room}/say", json={"text": text}, headers=b.write_headers())  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    return int(r.json()["id"])


async def test_a_message_naming_a_claim_wakes_the_claimer_and_only_while_it_is_held(
    broker: InProcBroker,
) -> None:
    """claude-1 claims owner/repo#12 and waits. codex-1 first writes owner/repo#123 (another pull
    request): nothing for claude-1 but chatter. Then it writes owner/repo#12: claude-1's wait() returns
    with that line as ``to_you=yes prio=mention`` and the earlier one as the chatter it was. After
    the release the same text is chatter again. Fails if a longer number matches, the holder is left
    to the hold, the claimer's own words come back to it, or a released claim still watches."""
    async with (
        FakeAgent(broker.home, "claim-holder") as claude,
        FakeAgent(broker.home, "claim-sayer") as codex,
        FakeAgent(broker.home, "claim-bystander") as bravo,
    ):
        await claude.join("#build", "claude-1")
        await codex.join("#build", "codex-1")
        await bravo.join("#build", "bravo-1")
        assert (await claude.call("claim", room="#build", key=KEY, ttl=600))["ok"]
        waiting = claude.wait_task("#build", timeout_s=30)

        other = (await codex.say("#build", "pushed the fix to owner/repo#123 too"))["posted_id"]
        assert classified(broker, "claude-1", other) == (0, 0)

        named = (await codex.say("#build", f"CI is green on {KEY}."))["posted_id"]
        assert classified(broker, "claude-1", named) == (1, 1)
        assert classified(broker, "bravo-1", named) == (0, 0)  # only the holder is addressed
        got = await waiting
        assert got["ok"] and ids_in(got["text"]) == [named, other]  # the earlier line rides along, after it
        lines = {int(ln.split()[1][3:]): ln for ln in got["text"].splitlines() if ln.startswith("- id=")}
        assert "to_you=yes" in lines[named] and "prio=mention" in lines[named]
        assert "to_you=no" in lines[other] and "prio=chatter" in lines[other]

        own = (await claude.say("#build", f"taking {KEY}"))["posted_id"]
        assert classified(broker, "claude-1", own) is None  # nothing comes back to its author
        assert classified(broker, "codex-1", own) == (0, 0)

        released = await claude.call("claim", room="#build", key=KEY, ttl=0)
        assert released["ok"] and released["released"]
        after = (await codex.say("#build", f"{KEY} again"))["posted_id"]
        assert classified(broker, "claude-1", after) == (0, 0)


async def test_a_person_naming_a_claim_is_still_priority_2_and_the_holder_is_marked(
    broker: InProcBroker,
) -> None:
    """A person's message reaches everyone at priority 2 (§8.1), as before; the holder of the claim it
    names is marked ``mentioned`` as an @mention of it would mark it, and the bystander is not. Fails
    if a person's message is demoted or marks the whole room."""
    async with FakeAgent(broker.home, "claim-holder") as claude, FakeAgent(broker.home, "claim-by") as bravo:
        await claude.join("#build", "claude-1")
        await bravo.join("#build", "bravo-1")
        assert (await claude.call("claim", room="#build", key=KEY, ttl=600))["ok"]
        said = person_says(broker, f"what is the state of {KEY}?")
        assert classified(broker, "claude-1", said) == (2, 1)
        assert classified(broker, "bravo-1", said) == (2, 0)
