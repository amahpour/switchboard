"""An empty read() through a real broker names what is still addressed to the agent (#542).

The engine tests cover the rule and the SessionStart reminder. This one checks the tool result
an agent actually gets: the second read(), after the first was confirmed, lists the question by
id, sender and age, and a history page does not.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard.config import Config

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
SECRET = "zebra-ticket-text"


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


async def test_an_empty_read_names_the_unanswered_question_and_a_history_page_does_not(
    broker: InProcBroker,
) -> None:
    """The question the agent was shown stays on it until pass(). The empty read() names the id,
    who sent it and how long ago, and not the text. ``read(before=)`` is history, so it stays a
    history page. Fails if the second read() is only "no new messages."."""
    earlier = person_says(broker, "before anyone joined")
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "tester")
        q = person_says(broker, SECRET)
        first = await agent.read("#build")
        assert first["count"] == 1 and SECRET in first["text"]
        second = await agent.read("#build")
        assert second["count"] == 0 and second["batch_id"] is None
        assert second["text"].startswith("[switchboard] #build: no new messages. Still on you: ")
        assert f"{q} from alice" in second["text"]
        assert "just now" in second["text"] or "min ago" in second["text"]
        assert SECRET not in second["text"]
        history = await agent.read("#build", before=q)
        assert history["batch_id"] is None and history["count"] == 1
        assert history["text"].startswith("[switchboard] #build: 1 message, oldest first.")
        assert "before anyone joined" in history["text"] and "Still on you" not in history["text"]
        assert earlier < q
        passed = await agent.pass_("#build")
        assert passed["ok"] is True
        cleared = await agent.read("#build")
        assert cleared["text"] == "[switchboard] #build: no new messages."
