"""A call hosted on a person's machine takes a snapshot when asked (#766, DESIGN.md §54).

The broker is the real one. The relay is the real ``Relay`` and its joinly is a ``FakeJoinly``, so the
picture is asked for and checked without a live call. The three ways to ask are the same as the server
bot's: ``/meeting snapshot`` (the bar's Snapshot button sends that command), and a call line that
addresses an agent and says "screen". One picture every 10 seconds, by the broker's clock. A picture
that is not a PNG, JPEG or WebP is not kept, and the line that asked is still posted.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import FakeClock, make_tmp_home
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly, tiny_png
from test_meeting_hosted import FP, KEY, Host, World, host, until
from test_relay_and_broker import Run, live

from switchboard.meeting import hosted
from switchboard.meeting.screen import SNAPSHOT_GAP_S, SNAPSHOT_TIMEOUT_S

CARD = re.compile(
    r"artifact: screen-\d{6}\.(png|jpg|webp) \| \d+ \| sha256:[0-9a-f]{64} \| switchboard:file/[0-9a-f]{32}"
)
SNAPSHOT = "/mcp/meeting/snapshot"


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


def cards(w: World) -> list[dict[str, Any]]:
    return [m for m in w.messages() if "switchboard:file/" in m["text"]]


def screen_line(w: World) -> dict[str, Any] | None:
    return next((m for m in w.call_lines() if "look at my screen" in m["text"]), None)


async def started(w: World, fake: FakeJoinly) -> tuple[Host, FakeAgent, Run]:
    """claude-1 and codex-1 in #build, the call live, the real relay on the fake joinly."""
    claude, codex = Host(w.b), await FakeAgent(w.b.home, "k2").start()
    assert (await claude.join("#build", "claude-1"))["ok"] and (await codex.join("#build", "codex-1"))["ok"]
    opened = await host(claude)
    assert opened["ok"] and opened["fingerprint"] == FP
    run = Run(w, fake, advance_clock=False)
    await until(live(w), what="the call to go live")
    return claude, codex, run


def say_screen(fake: FakeJoinly, start: float = 0) -> None:
    """A call-out, then a later line so the audio gap closes it (``lines.PAUSE_S``).

    ``start`` is the audio clock. A line still open from an earlier push merges with a new segment whose
    start is not at least two seconds after it, so a second call-out begins well after the first."""
    fake.push(
        [
            {
                "speaker": "bob",
                "text": "Claude one, look at my screen",
                "start": start,
                "end": start + 1,
                "role": "participant",
            },
            {
                "speaker": "bob",
                "text": "thanks",
                "start": start + 10,
                "end": start + 11,
                "role": "participant",
            },
        ]
    )


async def stop_run(run: Run | None, *agents: Any) -> None:
    if run is not None and not run.task.done():
        run.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run.task
    for agent in agents:
        await agent.close()


# ----------------------------------------------------------------------------- the three ways to ask
async def test_a_person_asks_and_the_relay_stores_the_picture_as_their_file(
    world: World, joinly: FakeJoinly
) -> None:
    """``/meeting snapshot``, which is also what the Snapshot button sends, asks the relay's own joinly
    and posts the picture as the person's file. Fails if the hosted call still refuses, the relay never
    asks joinly, or the card is from anyone but the person who asked."""
    w = world
    claude, codex, run = await started(w, joinly)
    try:
        waiting = codex.wait_task("#build", timeout_s=30)
        assert "snapshot" in w.command("/meeting snapshot")["text"]
        await until(lambda: len(cards(w)) == 1, what="the card")
        got = await waiting
    finally:
        await stop_run(run, claude, codex)
    assert joinly.snapshots == 1
    card = cards(w)[0]
    assert card["sender_kind"] == "human" and card["from"] == "alice"
    assert CARD.fullmatch(card["text"])
    assert card["files"][0]["name"].endswith(".png")
    r = w.web.get(f"/api/files/{card['files'][0]['id']}", headers=w.b.write_headers())
    assert r.status_code == 200 and r.content == joinly.frame
    assert r.headers["content-disposition"].startswith("attachment")
    assert "artifact: screen-" in got["text"] and "download:" in got["text"]


async def test_a_call_line_that_says_screen_posts_the_picture_before_the_line(
    world: World, joinly: FakeJoinly
) -> None:
    """A line that addresses one agent and says "screen" is posted after the picture, and the picture is
    from the call. A call-out without the word takes none. Fails if the line arrives with no picture, the
    picture comes second, or "screenshot" would have counted (it does not: the word is "screen")."""
    w = world
    claude, codex, run = await started(w, joinly)
    try:
        named = codex.wait_task("#build", timeout_s=30)
        joinly.push(
            [
                {
                    "speaker": "bob",
                    "text": "Codex, take a screenshot",
                    "start": 0,
                    "end": 1,
                    "role": "participant",
                },
                {"speaker": "bob", "text": "moving on", "start": 10, "end": 11, "role": "participant"},
            ]
        )
        await until(
            lambda: any("screenshot" in m["text"] for m in w.call_lines()), what="the screenshot line"
        )
        plain = await named
        assert "screenshot" in plain["text"] and joinly.snapshots == 0 and cards(w) == []
        w.tick(2.6)  # the trailing line closes on the wall clock, so it does not merge with the next
        await until(lambda: any(m["text"] == "moving on" for m in w.call_lines()), what="the trailing line")

        named = claude.wait_task("#build", timeout_s=30)
        pushes = sum(1 for path, _, _ in run.client.replies if path == "/mcp/meeting/push")
        say_screen(joinly, start=30)
        await until(
            lambda: sum(1 for path, _, _ in run.client.replies if path == "/mcp/meeting/push") > pushes,
            what="the relay to push the screen line",
        )
        w.tick(1.1)  # one a second since "moving on"; the screen line is held for its picture
        got = await named
        await until(
            lambda: screen_line(w) is not None and len(cards(w)) == 1, what="the picture and the line"
        )
    finally:
        await stop_run(run, claude, codex)
    assert joinly.snapshots == 1
    card = cards(w)[0]
    line = screen_line(w)
    assert line is not None
    assert card["sender_kind"] == "meeting" and card["from"] == "call"
    assert card["id"] < line["id"]
    assert "look at my screen" in got["text"] and "artifact: screen-" in got["text"]


# --------------------------------------------------------------------------------------- the limits
async def test_one_picture_every_ten_seconds_whichever_way_it_is_asked(
    world: World, joinly: FakeJoinly
) -> None:
    """The gap is the server bot's, on the broker's clock, and it covers the command and a call line.
    Inside it the line is still posted, with a notice and no second picture. Fails if a second request
    goes through, the window is not ten seconds, or the line is held back."""
    w = world
    claude, codex, run = await started(w, joinly)
    try:
        w.command("/meeting snapshot")
        await until(lambda: joinly.snapshots == 1 and len(cards(w)) == 1, what="the first picture")
        # the picture is waiting for claude-1: take it, so the next wait is the call line
        assert "artifact: screen-" in (await claude.wait_task("#build", timeout_s=30))["text"]

        again = w.command("/meeting snapshot", expect=409)
        assert "less than 10 seconds" in again["message"]
        w.clock.advance(SNAPSHOT_GAP_S - 0.5)
        assert w.command("/meeting snapshot", expect=409)
        assert joinly.snapshots == 1

        named = claude.wait_task("#build", timeout_s=30)
        say_screen(joinly)
        got = await named
        assert "look at my screen" in got["text"]
        assert joinly.snapshots == 1 and len(cards(w)) == 1
        assert any("less than 10 seconds" in t for t in w.notices())

        w.clock.advance(1.0)
        w.command("/meeting snapshot")
        await until(lambda: joinly.snapshots == 2 and len(cards(w)) == 2, what="the picture after the gap")
    finally:
        await stop_run(run, claude, codex)


async def test_a_picture_that_is_not_an_image_is_refused_and_the_line_is_still_posted(
    world: World, joinly: FakeJoinly
) -> None:
    """The bytes decide, not the type joinly claims. The line that asked is posted after the refusal.
    Fails if HTML is stored, or if a bad picture holds the line back."""
    joinly.frame = b"<html><script>alert(1)</script></html>"
    joinly.frame_type = "image/png"
    w = world
    claude, codex, run = await started(w, joinly)
    try:
        named = claude.wait_task("#build", timeout_s=30)
        say_screen(joinly)
        got = await named
        await until(lambda: screen_line(w) is not None, what="the line")
    finally:
        await stop_run(run, claude, codex)
    assert "look at my screen" in got["text"] and "artifact:" not in got["text"]
    assert cards(w) == []
    assert any("not a PNG, JPEG or WebP" in t for t in w.notices())
    line = screen_line(w)
    assert line is not None and (not cards(w) or cards(w)[0]["id"] > line["id"])


async def test_a_picture_over_the_rooms_file_limit_is_not_kept() -> None:
    """The room's file limit is the snapshot's, as it is for the server bot. One megabyte is the smallest
    the setting allows. The relay's own joinly cannot carry a body that size over its stream, so this posts
    the bytes the way the relay does, on a call that has asked. Fails if a larger body is stored, or the
    room is not told."""
    w = World(make_tmp_home(), FakeClock(), files_mb=1)
    claude = Host(w.b)
    try:
        assert (await claude.join("#build", "claude-1"))["ok"]
        assert (await host(claude))["ok"]
        assert w.relay.push([]).status_code == 200
        assert "snapshot" in w.command("/meeting snapshot")["text"]
        raw = w.relay.c.post(
            SNAPSHOT,
            content=tiny_png() + b"\x00" * (1024 * 1024),
            headers={"authorization": f"Bearer {KEY}", "content-type": "application/octet-stream"},
        )
        assert raw.status_code == 413
        assert any("file limit" in t for t in w.notices())
        assert cards(w) == []
    finally:
        await claude.close()
        w.stop()


async def test_a_held_line_is_posted_when_the_picture_never_comes(world: World) -> None:
    """The relay has eight seconds, the server bot's wait. Past it the room is told and the line is
    posted. Fails if the line stays queued, or is posted before the wait."""
    w = world
    claude = Host(w.b)
    try:
        assert (await claude.join("#build", "claude-1"))["ok"]
        assert (await host(claude))["ok"]
        assert w.relay.push([]).status_code == 200  # live
        pushed = w.relay.push(
            [
                {"speaker": "bob", "text": "Claude one, look at my screen", "start": 0, "end": 1},
                {"speaker": "bob", "text": "thanks", "start": 10, "end": 11},
            ]
        )
        assert pushed.status_code == 200
        assert screen_line(w) is None
        w.tick(SNAPSHOT_TIMEOUT_S - 0.1)
        assert screen_line(w) is None
        w.tick(0.2)
        assert screen_line(w) is not None
        assert any("Couldn't get a snapshot" in t for t in w.notices())
        assert cards(w) == []
    finally:
        await claude.close()


async def test_a_picture_nobody_asked_for_is_refused_and_another_credential_cannot_post_one(
    world: World,
) -> None:
    """The call key delivers a picture only while this call is waiting for one, and only that key opens
    the route. Fails if an unsolicited body is stored, or an agent token, a listener key or a browser can
    post one."""
    w = world
    claude = Host(w.b)
    try:
        assert (await claude.join("#build", "claude-1"))["ok"]
        assert (await host(claude))["ok"]
        assert w.relay.push([]).status_code == 200
        raw = w.relay.c.post(
            SNAPSHOT,
            content=tiny_png(),
            headers={"authorization": f"Bearer {KEY}", "content-type": "application/octet-stream"},
        )
        assert (raw.status_code, raw.json()["error"]) == (409, "conflict")
        assert cards(w) == []
        token = claude.token
        for headers in (
            {"authorization": f"Bearer {token}", "content-type": "application/octet-stream"},
            {"authorization": "Bearer sbl_" + "ab" * 32, "content-type": "application/octet-stream"},
            {
                "authorization": f"Bearer {KEY}",
                "content-type": "application/octet-stream",
                "origin": w.b.origin,
            },
        ):
            r = w.relay.c.post(SNAPSHOT, content=tiny_png(), headers=headers)
            assert r.status_code in (401, 403), r.text
        assert cards(w) == []
    finally:
        await claude.close()


async def test_a_snapshot_needs_the_bot_in_the_call(world: World) -> None:
    """A hosted call that is still joining has nothing to photograph. Sharing a picture stays refused:
    that is a different path. Fails if the snapshot command still says it is not available, or if it
    pretends to ask before the bot is in the call."""
    w = world
    claude = Host(w.b)
    try:
        assert (await claude.join("#build", "claude-1"))["ok"]
        assert (await host(claude))["ok"]
        snap = w.command("/meeting snapshot", expect=409)
        assert "not in the call yet" in snap["message"]
        assert "isn't available yet" not in snap["message"]
        shared = w.command("/meeting share plan.png", expect=409)
        assert "isn't available yet for a call hosted" in shared["message"]
    finally:
        await claude.close()


def test_the_timeout_matches_the_server_bots() -> None:
    """The wait and the gap are the server bot's numbers. Fails if a hosted call uses another limit."""
    assert SNAPSHOT_GAP_S == 10.0
    assert SNAPSHOT_TIMEOUT_S == 8.0
    assert hosted.SNAPSHOT_TIMEOUT_S == SNAPSHOT_TIMEOUT_S
