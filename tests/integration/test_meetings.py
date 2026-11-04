"""A room transcribes a call through a scripted joinly (DESIGN.md §52).

The broker is the real one. joinly is ``FakeJoinly``: it records the join, the hotwords
and the leave, and pushes transcript segments. A passcode in the link is how the
call is joined and is never written down.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly

from switchboard.config import Config, MeetingCfg

SECRET = "SECRET99"
ZOOM = f"https://us02web.zoom.us/j/123456789?pwd={SECRET}"


async def wait_until(cond, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        await __import__("asyncio").sleep(0.05)


@pytest.fixture
def joinly() -> Iterator[FakeJoinly]:
    server = FakeJoinly().start()
    yield server
    server.stop()


@pytest.fixture
def broker(tmp_home: Path, joinly: FakeJoinly) -> Iterator[InProcBroker]:
    cfg = Config(human_name="alice").replace(meeting=MeetingCfg(joinly_url=joinly.url))
    cfg = cfg.with_delivery(quiet_s=0.0, max_hold_s=0.0, unaddressed_hold_s=0.0)
    b = InProcBroker(tmp_home, cfg).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web  # type: ignore[attr-defined]
    yield b
    web.close()
    b.stop()


def command(b: InProcBroker, text: str, room: str = "build") -> dict:
    r = b.web.post(f"/api/rooms/{room}/command", json={"text": text}, headers=b.write_headers())  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    return r.json()


def history(b: InProcBroker) -> list[dict]:
    r = b.web.get("/api/rooms/build/messages", headers=b.write_headers())  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    return r.json()["messages"]


def notices(b: InProcBroker) -> str:
    return "\n".join(m["text"] for m in history(b) if m["kind"] == "notice")


def meeting_lines(b: InProcBroker) -> list[dict]:
    return [m for m in history(b) if m.get("sender_kind") == "meeting"]


def open_calls(b: InProcBroker) -> int:
    con = sqlite3.connect(b.paths.db)
    n = con.execute("SELECT COUNT(*) FROM meetings WHERE state!='ended'").fetchone()[0]
    con.close()
    return int(n)


def seg(text: str, start: float, end: float, speaker: str | None = None, role: str = "participant") -> dict:
    return {"text": text, "start": start, "end": end, "speaker": speaker, "role": role}


async def test_a_link_joins_with_hotwords_and_a_call_out_wakes_only_that_agent(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """``/meeting`` joins as "switchboard (transcribing)" with the room's spoken names.
    A line that names claude-1 wakes claude-1, with to_you, and a line that names
    nobody wakes nobody. Fails if the header omits the hotwords, the line is stored
    as a person, or talk that names nobody starts a turn."""
    async with FakeAgent(broker.home, "k1") as claude, FakeAgent(broker.home, "k2") as codex:
        assert (await claude.join("#build", "claude-1"))["ok"]
        assert (await codex.join("#build", "codex-1"))["ok"]
        out = command(broker, f"/meeting {ZOOM}")
        assert "joining" in out["text"].lower() and SECRET not in out["text"]
        await wait_until(lambda: len(joinly.joins) == 1)
        join = joinly.joins[0]
        assert join["meeting_url"] == ZOOM
        assert join["participant_name"] == "switchboard (transcribing)"
        assert join["passcode"] is None
        settings = [json.loads(s) for s in joinly.settings]
        assert any(s.get("name") == "switchboard (transcribing)" for s in settings)
        words = [w.lower() for s in settings for w in s.get("stt_args", {}).get("hotwords", [])]
        assert "claude one" in words and "codex" in words
        waiting = codex.wait_task("#build", timeout_s=30)
        joinly.push(
            [
                seg("hello everyone", 0.0, 1.0),
            ]
        )
        await wait_until(lambda: len(meeting_lines(broker)) >= 1)
        assert not waiting.done()
        waiting.cancel()
        named = claude.wait_task("#build", timeout_s=30)
        joinly.push([seg("Claude one, tell me how much", 10.0, 12.0)])
        got = await named
        assert got.get("ok") and "to_you=yes" in got["text"] and "kind=meeting" in got["text"]
        assert "from=call" in got["text"]


async def test_an_ambiguous_name_asks_which_agent(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """A bare "Claude" with two Claudes posts the line and a notice, and wakes neither.
    Fails if one of them is guessed, or the notice names the wrong pair."""
    async with FakeAgent(broker.home, "k1") as a, FakeAgent(broker.home, "k2") as b:
        await a.join("#build", "claude-1")
        await b.join("#build", "claude-2")
        command(broker, "/meeting https://meet.google.com/abc-defg-hij")
        await wait_until(lambda: len(joinly.joins) == 1)
        wa, wb = a.wait_task("#build", timeout_s=30), b.wait_task("#build", timeout_s=30)
        joinly.push([seg("Claude, say the number", 0.0, 1.5)])
        await wait_until(lambda: "Say the number." in notices(broker))
        assert (
            'Someone on the call said "Claude", and this room has claude-1 and claude-2. Say the number.'
            in notices(broker)
        )
        assert not wa.done() and not wb.done()
        wa.cancel()
        wb.cancel()


async def test_leave_close_and_shutdown_each_leave_the_call(
    broker: InProcBroker, joinly: FakeJoinly, tmp_home: Path
) -> None:
    """Each way the call ends tells joinly to leave, so the bot does not stay as a ghost.
    Fails if one of the three paths only drops the client."""
    command(broker, "/meeting https://teams.microsoft.com/l/meetup-join/abc")
    await wait_until(lambda: len(joinly.joins) == 1)
    command(broker, "/meeting leave")
    await wait_until(lambda: joinly.leaves >= 1 and open_calls(broker) == 0)
    assert "stopped transcribing" in notices(broker)

    command(broker, "/meeting https://zoom.us/j/555")
    await wait_until(lambda: len(joinly.joins) == 2)
    command(broker, "/close")
    await wait_until(lambda: joinly.leaves >= 2 and open_calls(broker) == 0)

    web = broker.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=broker.write_headers()).status_code == 200
    broker.web = web  # type: ignore[attr-defined]
    command(broker, "/meeting https://teams.live.com/meet/abc")
    await wait_until(lambda: len(joinly.joins) == 3)
    broker.stop()
    await wait_until(lambda: joinly.leaves >= 3)
    # start again so the fixture's stop is a second, harmless one
    restarted = InProcBroker(tmp_home, broker.cfg).start()
    broker.server = restarted.server
    broker.thread = restarted.thread
    broker.app = restarted.app
    broker.tcp = restarted.tcp


async def test_a_link_off_the_allowlist_is_refused_and_the_passcode_is_nowhere(
    broker: InProcBroker, joinly: FakeJoinly, caplog: pytest.LogCaptureFixture
) -> None:
    """joinly's browser would open any URL, so anything but Zoom, Meet or Teams https
    is refused. The passcode is in the join and nowhere in the database, the notices
    or the log. Fails if an internal address is opened, or ``pwd=`` is stored or logged."""
    caplog.set_level(logging.DEBUG)
    for link in (
        "https://example.com/j/1",
        "http://zoom.us/j/1",
        "https://zoom.us.evil.example/j/1",
        "https://10.0.0.1/j/1",
    ):
        r = broker.web.post(  # type: ignore[attr-defined]
            "/api/rooms/build/command", json={"text": f"/meeting {link}"}, headers=broker.write_headers()
        )
        assert r.status_code == 400, r.text
        assert link not in r.text
    assert joinly.joins == []
    command(broker, f"/meeting {ZOOM}")
    await wait_until(lambda: len(joinly.joins) == 1)
    await wait_until(
        lambda: any(m.get("sender_kind") == "meeting" or "joining" in m["text"] for m in history(broker))
    )
    blob = Path(broker.paths.db).read_bytes()
    wal = Path(str(broker.paths.db) + "-wal")
    if wal.exists():
        blob += wal.read_bytes()
    assert SECRET.encode() not in blob
    assert SECRET not in notices(broker)
    assert SECRET not in caplog.text
    again = broker.web.post(  # type: ignore[attr-defined]
        "/api/rooms/build/command",
        json={"text": "/meeting https://meet.google.com/xyz-abcd-efg"},
        headers=broker.write_headers(),
    )
    assert again.status_code == 409
    assert "already" in again.text.lower()


async def test_an_agents_meeting_command_is_literal_text(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """An agent's ``say("/meeting ...")`` is chat, like every other command. Fails if
    an agent can start a call."""
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        said = await agent.say("#build", "/meeting https://zoom.us/j/999")
        assert said.get("ok")
    await wait_until(lambda: any("/meeting https://zoom.us/j/999" in m["text"] for m in history(broker)))
    assert joinly.joins == []
    assert all(m["sender_kind"] != "meeting" for m in history(broker))


def test_without_a_meeting_server_the_command_names_the_setup(tmp_home: Path) -> None:
    """No ``[meeting] joinly_url``: a short hint that names DEPLOY.md, and no join.
    Fails if the command tries a default URL, or stays silent about how to set one."""
    b = InProcBroker(tmp_home, Config(human_name="alice")).start()
    web = b.web_client()
    try:
        assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
        r = web.post(
            "/api/rooms/build/command",
            json={"text": "/meeting https://zoom.us/j/1"},
            headers=b.write_headers(),
        )
        assert r.status_code == 200, r.text
        assert "DEPLOY.md" in r.json()["text"] and "joinly_url" in r.json()["text"]
        con = sqlite3.connect(b.paths.db)
        assert con.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 0
        con.close()
    finally:
        web.close()
        b.stop()


async def test_a_join_failure_ends_the_call_and_still_leaves(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """joinly refusing the join marks the call ended, tells the room, and still calls
    leave_meeting. Fails if the room stays on 'joining' or the session is abandoned."""
    joinly.fail_join = True
    command(broker, "/meeting https://zoom.us/j/42")
    await wait_until(
        lambda: "couldn't join" in notices(broker).lower() or "could not" in notices(broker).lower()
    )
    await wait_until(lambda: joinly.leaves >= 1)
    con = sqlite3.connect(broker.paths.db)
    row = con.execute("SELECT state, error FROM meetings").fetchone()
    con.close()
    assert row[0] == "ended" and row[1]
