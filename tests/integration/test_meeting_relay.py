"""The relay of a hosted call against the real broker and a scripted joinly (#623, DESIGN.md §54).

The broker is the real one on a ``FakeClock`` (``test_meeting_hosted.py``'s world), the call is registered
as the host script registers it, and the relay is the real ``Relay`` with its real HTTP client and its real
MCP client of a ``FakeJoinly`` (a real MCP server on a loopback port, ``tests/fakes/fake_joinly.py``): what a
spoken name does to the room and to the agents, what an agent's ``@call`` line does to joinly, what ending the
call from the room does to the relay, and how the relay takes a ``429``. The relay's own logic in every
corner is in ``tests/unit/test_meeting_relay.py``; this proves the two ends fit.

Nothing here sleeps to mean "by now": the relay and the room are waited on by what they do. The relay's pauses
are replaced by a function that moves the broker's clock by the pause (so a ``Retry-After`` of five seconds
costs five seconds of the clock's, not of ours).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from conftest import FakeClock, make_tmp_home
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly
from test_meeting_hosted import FP, KEY, Host, World, host, until

from switchboard.meeting import hosted
from switchboard.meeting.relay import (
    BrokerClient,
    Exit,
    Relay,
    RelayConfig,
    Reply,
    Secret,
    real_joinly,
)

LINK = "https://zoom.us/j/123456789?pwd=hunter2"


@pytest.fixture
def world() -> Iterator[World]:
    home = make_tmp_home()
    w = World(home, FakeClock())
    try:
        yield w
    finally:
        w.stop()


@pytest.fixture
def joinly() -> Iterator[FakeJoinly]:
    j = FakeJoinly().start()
    try:
        yield j
    finally:
        j.stop()


class Recording(BrokerClient):
    """The real client, noting every answer it was given."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.replies: list[tuple[str, int, dict[str, Any]]] = []

    async def post(self, path: str, body: dict[str, Any], timeout: float) -> Reply:
        got = await super().post(path, body, timeout)
        self.replies.append((path, got.status, got.body))
        return got


class Run:
    """A relay on the test's loop, for the key ``KEY`` against the world's broker and a ``FakeJoinly``."""

    def __init__(
        self, w: World, fake: FakeJoinly, *, link: str | None = LINK, key: str = KEY, join: bool = True
    ):
        self.w, self.fake = w, fake
        self.slept: list[float] = []
        self.said: list[str] = []
        self.client = Recording(w.b.base, Secret(key))
        cfg = RelayConfig(join=join, model="")

        async def sleep(seconds: float) -> None:
            self.slept.append(seconds)
            w.clock.advance(seconds)
            await asyncio.sleep(0)

        self.relay = Relay(
            cfg,
            Secret(key),
            Secret(link) if link else None,
            self.client,
            real_joinly(fake.url, cfg),
            sleep=sleep,
            say=self.said.append,
        )
        self.task = asyncio.create_task(self.relay.run())

    async def code(self, timeout: float = 30.0) -> Exit:
        return await asyncio.wait_for(self.task, timeout)

    def heard(self) -> str:
        return "\n".join(self.said)


async def hosting(w: World) -> tuple[Host, FakeAgent]:
    """claude-1 (which hosts, on alice-laptop) and codex-1, both in #build, and the call registered but not
    yet live: what the host script leaves behind before it starts the relay."""
    claude, codex = Host(w.b), await FakeAgent(w.b.home, "k2").start()
    assert (await claude.join("#build", "claude-1"))["ok"] and (await codex.join("#build", "codex-1"))["ok"]
    opened = await host(claude, model="large-v3-turbo")
    assert opened["ok"] and opened["fingerprint"] == FP
    return claude, codex


def live(w: World) -> Callable[[], bool]:
    return lambda: bool(w.settings()["meeting"]) and w.settings()["meeting"]["state"] == "live"


def seg(text: str, start: float, end: float, speaker: str = "bob") -> dict[str, Any]:
    return {"speaker": speaker, "text": text, "start": start, "end": end}


# ---------------------------------------------------------------------------------- the whole path
async def test_the_relay_joins_goes_live_wakes_the_named_agent_speaks_and_leaves_when_it_is_ended(
    world: World, joinly: FakeJoinly
) -> None:
    """The whole of what a person gets from hosting a call on their machine, with the real broker: the relay
    joins with the room's names as hints, and its first push makes the call bar live; a spoken name in the
    transcript wakes only the agent named, with the line in its batch; with voice on, an agent's ``@call``
    line is spoken into the call and nothing else is; and a person's End call makes the relay leave and exit
    0, after which the key opens nothing. Fails if any link in that chain is missing."""
    w = world
    claude, codex = await hosting(w)
    run: Run | None = None
    try:
        run = Run(w, joinly)
        await until(live(w), what="the call to go live")
        assert joinly.joins == [
            {
                "meeting_url": LINK,
                "participant_name": "switchboard (transcribing)",
                "passcode": None,
            }
        ]
        settings = json.loads(joinly.settings[-1])
        assert {"Claude", "Codex"} <= set(settings["stt_args"]["hotwords"])  # the room's names, as hints
        assert "model_name" not in settings["stt_args"]  # joinly's own model: nothing to download
        assert w.hosted_row()["state"] == "live"

        waiting = codex.wait_task("#build", timeout_s=30)
        claude_waiting = claude.wait_task("#build", timeout_s=30)
        joinly.push(
            [
                {**seg("morning all", 0, 1), "role": "participant"},
                {**seg("Codex, how much is gas in Los Angeles?", 10, 12), "role": "participant"},
                {**seg("thanks", 30, 31), "role": "participant"},  # the pause closes the call-out
            ]
        )
        await until(lambda: len(w.call_lines()) >= 1, what="the first line")
        w.tick(1.1)
        got = await asyncio.wait_for(waiting, 10)
        assert got["ok"] and "to_you=yes" in got["text"] and "kind=meeting" in got["text"]
        assert "gas in Los Angeles" in got["text"]
        assert not claude_waiting.done()  # claude-1 was not named
        assert w.call_lines()[0]["host"] == "alice-laptop"  # and each line says which machine heard it

        await claude.say("#build", "@call nobody turned voice on")
        assert "voice is on" in w.command("/meeting voice on")["text"]
        await claude.say("#build", "@call The limit is **ten** megabytes.")
        await until(lambda: joinly.spoken != [], what="the line to be spoken")
        assert joinly.spoken == ["The limit is ten megabytes."]  # made speakable by the broker, once

        assert "leaving" in w.command("/meeting leave")["text"]
        assert await run.code() == Exit.ENDED
        assert joinly.leaves == 1
        assert "the call has ended" in run.heard()
        assert w.settings()["meeting"] is None
        late = w.relay.push([])  # the key opens nothing once the call is over
        assert late.status_code == 410
    finally:
        if run is not None and not run.task.done():
            run.task.cancel()
        await claude.close()
        await codex.close()


async def test_a_429_is_waited_out_by_the_time_the_broker_names_and_every_segment_arrives(
    world: World, joinly: FakeJoinly
) -> None:
    """More segments at once than the broker's burst allows: it answers 429 with ``Retry-After``, the relay
    waits that long (here the broker's clock moves by it), sends the same batch again, and the broker has
    all 150 in the end. Fails if the relay ignores the pause, hammers the broker, or loses the batch."""
    w = world
    claude, codex = await hosting(w)
    try:
        run = Run(w, joinly)
        await until(live(w), what="the call to go live")
        joinly.push(
            [{**seg(f"s{i}", float(i * 5), float(i * 5 + 1)), "role": "participant"} for i in range(150)]
        )
        await until(
            lambda: (
                sum(b["accepted"] for p, s, b in run.client.replies if s == 200 and "accepted" in b) == 150
            ),
            what="all 150 segments to be accepted",
        )
        limited = [(s, b) for p, s, b in run.client.replies if s == 429]
        assert limited and limited[0][1]["error"] == "rate_limited"
        waits = [x for x in run.slept if x != 0.5 and x >= 1.0]
        assert (
            waits and max(waits) < hosted.BURST / hosted.RATE_PER_S + 1
        )  # the broker's own number, honoured
        w.command("/meeting leave")
        assert await run.code() == Exit.ENDED
    finally:
        await claude.close()
        await codex.close()


# ------------------------------------------------------------------------------------ the keys
async def test_a_key_the_broker_does_not_know_stops_the_relay_before_it_touches_joinly(
    world: World, joinly: FakeJoinly
) -> None:
    """Nothing registered this key: the first request is a 401, the relay exits 3, and no session is opened
    with joinly. Fails if the relay starts a browser for a call that cannot be pushed."""
    run = Run(world, joinly)
    assert await run.code() == Exit.REFUSED
    assert joinly.joins == [] and joinly.sessions == []
    assert "unknown_key" in run.heard() and KEY not in run.heard()


async def test_a_call_already_ended_stops_the_relay_with_nothing_to_leave(
    world: World, joinly: FakeJoinly
) -> None:
    w = world
    claude, codex = await hosting(w)
    try:
        w.command("/meeting leave")
        run = Run(w, joinly)
        assert await run.code() == Exit.ENDED
        assert joinly.joins == [] and joinly.leaves == 0
    finally:
        await claude.close()
        await codex.close()


async def test_revoking_the_machines_sign_in_stops_the_relay_with_a_refusal(
    world: World, joinly: FakeJoinly
) -> None:
    """Settings → Agents → revoke: the key is refused (401 ``revoked``, or 410 if the broker's own tick ended
    the call first), and the relay leaves. A refusal is exit 3, an ended call 0: either way it is gone."""
    w = world
    claude, codex = await hosting(w)
    try:
        run = Run(w, joinly)
        await until(live(w), what="the call to go live")
        token_id = w.hosted_row()["token_id"]
        assert w.b.on_loop(w.b.state.store.agent_token_revoke, token_id, None) is not None
        joinly.push([{**seg("anyone there", 0, 1), "role": "participant"}])
        assert await run.code() in (Exit.REFUSED, Exit.ENDED)
        assert joinly.leaves == 1
    finally:
        await claude.close()
        await codex.close()


async def test_ending_the_call_while_the_relay_is_still_joining_leaves_without_going_live(
    world: World, joinly: FakeJoinly
) -> None:
    """A first start can spend minutes loading a model before it joins. A person who ends the call meanwhile
    is heard at once: the relay drops the join and leaves, and the call never went live. Fails if the relay
    waits for the join before it looks."""
    w = world
    joinly.hold_join = True
    claude, codex = await hosting(w)
    try:
        run = Run(w, joinly)
        await until(lambda: joinly.join_attempts == 1, what="the join to begin")
        w.command("/meeting leave")
        assert await run.code() == Exit.ENDED
        assert w.rows()[-1]["live_at"] is None and joinly.leaves == 1
        joinly.release_join()
    finally:
        joinly.release_join()
        await claude.close()
        await codex.close()


async def test_a_relay_with_no_join_is_heard_from_and_obeys_the_end(world: World, joinly: FakeJoinly) -> None:
    """``--no-join``, the setup check: joinly is reached, nothing is joined, the call stays ``joining`` (a
    live call needs a first push), the key is heard from by its poll, and the end is obeyed."""
    w = world
    claude, codex = await hosting(w)
    try:
        run = Run(w, joinly, link=None, join=False)
        await until(lambda: bool(joinly.sessions), what="joinly to be reached")
        assert joinly.joins == [] and w.settings()["meeting"]["state"] == "joining"
        w.command("/meeting leave")
        assert await run.code() == Exit.ENDED
    finally:
        await claude.close()
        await codex.close()
