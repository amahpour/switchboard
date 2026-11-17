"""Retrying a say after a lost answer (DESIGN.md §8.4, §15)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FakeClock, InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard.config import Config


@pytest.fixture
def build(tmp_home: Path, clock: FakeClock, request: pytest.FixtureRequest) -> Iterator[InProcBroker]:
    rate = getattr(request, "param", 0)
    broker = InProcBroker(tmp_home, Config().with_delivery(rate_limit_s=rate), clock=clock).start()
    try:
        broker.call("room.create", {"name": "#build"})
        yield broker
    finally:
        broker.stop()


def history(broker: InProcBroker) -> list[str]:
    web = broker.web_client()
    try:
        messages = web.get("/api/rooms/build/messages").json()["messages"]
        return [m["text"] for m in messages if m["kind"] == "chat" and m["sender_kind"] == "agent"]
    finally:
        web.close()


async def test_same_key_returns_the_original_post_even_after_restart(build: InProcBroker) -> None:
    """A successful post whose reply the caller lost is still a single room line."""
    async with FakeAgent(build.home, "writer") as a:
        assert (await a.join("#build", "writer"))["ok"]
        original = await a.call("say", room="#build", text="sent once", key="send-1")
        assert original["posted_id"]
        assert await a.call("say", room="#build", text="sent once", key="send-1") == original
    await asyncio.to_thread(build.restart)
    async with FakeAgent(build.home, "writer") as a:
        assert await a.call("say", room="#build", text="sent once", key="send-1") == original
    assert history(build) == ["sent once"]


async def test_distinct_keys_and_members_are_independent(build: InProcBroker) -> None:
    """The scope is one member, and two intentional copies need two different keys."""
    async with FakeAgent(build.home, "writer") as a, FakeAgent(build.home, "peer") as peer:
        await a.join("#build", "writer")
        await peer.join("#build", "peer")
        first = await a.call("say", room="#build", text="same words", key="one")
        second = await a.call("say", room="#build", text="same words", key="two")
        other = await peer.call("say", room="#build", text="same words", key="one")
        assert first["posted_id"] < second["posted_id"] < other["posted_id"]
        assert (await a.call("say", room="#build", text="same words", key="one"))["posted_id"] == first[
            "posted_id"
        ]
        assert (await peer.call("say", room="#build", text="same words", key="one"))["posted_id"] == other[
            "posted_id"
        ]
    assert history(build) == ["same words"] * 3


async def test_key_expires_without_extending_on_replay(build: InProcBroker, clock: FakeClock) -> None:
    """After five minutes a key can be used again, even if it was replayed just before expiry."""
    async with FakeAgent(build.home, "writer") as a:
        await a.join("#build", "writer")
        first = await a.call("say", room="#build", text="hello", key="send-1")
        build.on_loop(clock.advance, 299)
        assert (await a.call("say", room="#build", text="hello", key="send-1"))["posted_id"] == first[
            "posted_id"
        ]
        build.on_loop(clock.advance, 1)
        second = await a.call("say", room="#build", text="hello", key="send-1")
        assert second["posted_id"] > first["posted_id"]
    assert history(build) == ["hello", "hello"]


@pytest.mark.parametrize("build", [3600], indirect=True)
async def test_queued_key_replays_before_and_after_shutdown_posts_it(
    build: InProcBroker, clock: FakeClock
) -> None:
    """Retries of an accepted queued send neither take a second slot nor repost after the flush."""
    async with FakeAgent(build.home, "writer") as a:
        await a.join("#build", "writer")
        assert (await a.say("#build", "first"))["posted_id"]
        queued = await a.call("say", room="#build", text="second", key="send-2")
        assert queued["queued"] is True
        build.on_loop(clock.advance, 600)  # the expiry starts when it posts, even on a long queue
        assert (await a.call("say", room="#build", text="second", key="send-2")) == queued
    await asyncio.to_thread(build.restart)
    async with FakeAgent(build.home, "writer") as a:
        posted = await a.call("say", room="#build", text="second", key="send-2")
        assert posted["posted_id"]
        assert history(build) == ["first", "second"]


async def test_replay_does_not_confirm_or_repeat_unread_and_upload_grants(build: InProcBroker) -> None:
    """Receipt replay leaves the first call's offer and upload grant alone."""
    async with FakeAgent(build.home, "writer") as a:
        await a.join("#build", "writer")
        prompt = build.call("human.say", {"room": "#build", "text": "Please post the notes."})["id"]
        args = {
            "room": "#build",
            "text": "artifact: notes.txt | /Users/someone/notes.txt",
            "key": "notes-1",
            "reply_to": prompt,
            "since": prompt,
        }
        # An ordinary say can return both a batch and a one-time upload command.
        posted = await a.call("say", **args)
        assert posted["batch_id"] and posted["count"] == 1
        assert "curl" in posted["share"]
        batch = posted["batch_id"]
        build.call("human.say", {"room": "#build", "text": "Thanks."})
        retry = await a.call("say", **args)
        assert retry["posted_id"] == posted["posted_id"]  # even if 'since' is stale now
        assert retry["count"] == 0 and retry["unread_text"] is None and retry["batch_id"] is None
        assert "share" not in retry
        st = build.state.store
        assert build.on_loop(st.get_batch, batch).state == "offered"
        assert len(build.on_loop(st.files_for_message, posted["posted_id"])) == 1


async def test_invalid_keys_refuse_without_echoing_or_posting(build: InProcBroker) -> None:
    """A supplied key fits the ASCII alphabet and length or the entire send is refused."""
    async with FakeAgent(build.home, "writer") as a:
        await a.join("#build", "writer")
        for invalid in ("x" * 65, "private key", "clé", "line\n", "x/y"):
            result = await a.call("say", room="#build", text="hello", key=invalid)
            assert result["code"] == "bad_request" and not result["ok"]
            assert invalid not in result["error"]
        assert (await a.call("say", room="#build", text="hello", key="a" * 64))["posted_id"]
    assert history(build) == ["hello"]


@pytest.mark.parametrize("build", [3600], indirect=True)
async def test_deleted_room_cannot_pass_receipts_or_queued_keys_to_a_new_room(
    build: InProcBroker,
) -> None:
    """Deletion removes persisted receipts and frees queued keys before a membership ID is reused."""
    room = {"room": "#build"}
    async with FakeAgent(build.home, "writer") as a:
        await a.join("#build", "writer")
        assert (await a.call("say", **room, text="original", key="posted"))["posted_id"]
        assert (await a.call("say", **room, text="cancelled", key="pending"))["queued"]
    build.call("human.command", {**room, "text": "/close"})
    plan = build.call("room.delete", {**room, "dry_run": True})
    build.call("room.delete", {**room, **{k: plan[k] for k in ("room_id", "name", "created_at")}})
    build.call("room.create", {"name": "#build"})
    async with FakeAgent(build.home, "peer") as peer:
        await peer.join("#build", "peer")
        assert (await peer.call("say", **room, text="new post", key="pending"))["posted_id"]
        assert (await peer.call("say", **room, text="new queued post", key="posted"))["queued"]
    await asyncio.to_thread(build.restart)
    assert history(build) == ["new post", "new queued post"]
