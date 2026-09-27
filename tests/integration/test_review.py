"""/review against a real in-process broker (DESIGN.md §26).

The reviewer and a bystander are scripted test agents (``switchboard mcp --harness test``); the
author is a stand-in Claude or Codex session (``fakes/fake_claude.py``), so its session id
comes through the real join path. ``/review`` posts one human chat message: the engine wakes
the reviewer (and, like any human message, the bystander), never the author. agentsview is
never run: ``shutil.which`` is patched in this process (the broker runs in it).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from conftest import InProcBroker
from fakes.fake_agent import FakeAgent, ids_in
from fakes.fake_claude import SID, FakeClaude
from switchboard.broker import review
from switchboard.broker.peer import AllowAllHumans
from switchboard.config import Config
from switchboard.mcp.client import RpcError

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
_REAL_WHICH = shutil.which


def with_agentsview(monkeypatch: pytest.MonkeyPatch, found: bool = True) -> None:
    def which(cmd: Any, *a: Any, **kw: Any) -> str | None:
        if cmd == "agentsview":
            return "/opt/fake/bin/agentsview" if found else None
        return _REAL_WHICH(cmd, *a, **kw)

    monkeypatch.setattr(shutil, "which", which)


def start(tmp_home: Path, policy: Any = None) -> InProcBroker:
    b = InProcBroker(tmp_home, FAST, policy=policy).start()
    # the MCP servers read the same config file the broker was built from
    (tmp_home / "config.toml").write_text(f'[claude]\nsessions_dir = "{b.cfg.claude.sessions_dir}"\n')
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    return b


@pytest.fixture
def broker(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    with_agentsview(monkeypatch)
    b = start(tmp_home)
    yield b
    b.web.close()
    b.stop()


@pytest.fixture
def claude(broker: InProcBroker):
    c = FakeClaude(broker)
    r = c.tool("join", room="#build", screen_name="claude-1")
    assert r["ok"], r
    yield c
    c.close()


def web_cmd(b: InProcBroker, text: str) -> Any:
    return b.web.post("/api/rooms/build/command", json={"text": text}, headers=b.write_headers())


def q(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def deliveries(b: InProcBroker, message_id: int) -> dict[str, tuple[int, int, str]]:
    rows = q(b, "SELECT m.screen_name, d.prio, d.mentioned, d.state FROM deliveries d"
                " JOIN memberships m ON m.id=d.membership_id WHERE d.message_id=?", message_id)
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def human_chat(b: InProcBroker) -> list[dict[str, Any]]:
    msgs = b.web.get("/api/rooms/build/messages").json()["messages"]
    return [m for m in msgs if m["kind"] == "chat" and m["sender_kind"] == "human"]


def review_events(b: InProcBroker) -> list[sqlite3.Row]:
    return q(b, "SELECT data FROM events WHERE kind='review'")


# ------------------------------------------------------------------ waking
async def test_review_wakes_the_reviewer_not_the_author(broker: InProcBroker, claude: FakeClaude) -> None:
    async with FakeAgent(broker.home, "rev") as rev, FakeAgent(broker.home, "by") as by:
        await rev.join("#build", "reviewer")
        await by.join("#build", "bystander")
        t_rev, t_by = rev.wait_task("#build", 20), by.wait_task("#build", 20)
        await asyncio.sleep(0.4)
        r = web_cmd(broker, "/review reviewer claude-1 focus on the error paths")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "text": f"asked reviewer to review claude-1's recent work"
                                                f" (agentsview id {SID})\n"
                                                "⚠ reviewer may run with approvals off (its approval mode is"
                                                " unknown): the transcript it reads (tool output, web pages)"
                                                " can steer it"}
        [msg] = human_chat(broker)  # exactly one post
        assert msg["text"] == review.request_text("reviewer", "claude-1", SID, note="focus on the error paths")
        assert msg["from"] == "alice" and msg["via"] == "web" and msg["mentions"] == ["reviewer"]
        # the reviewer's open wait() returns it, addressed to it, through the engine
        got = await asyncio.wait_for(t_rev, 5)
        assert got["status"] == "messages" and ids_in(got["text"]) == [msg["id"]]
        assert "to_you=yes" in got["text"] and "prio=human" in got["text"]
        assert f"session messages {SID} --direction desc" in got["text"]
        # a bystander gets it like any human message (not addressed to it)
        other = await asyncio.wait_for(t_by, 5)
        assert ids_in(other["text"]) == [msg["id"]] and "to_you=no" in other["text"]
        # the author: no delivery row, so nothing can wake it
        d = deliveries(broker, msg["id"])
        assert set(d) == {"reviewer", "bystander"}
        assert d["reviewer"][:2] == (2, 1) and d["bystander"][:2] == (2, 0)
        assert q(broker, "SELECT COUNT(*) FROM deliveries d JOIN memberships m ON m.id=d.membership_id"
                         " WHERE m.screen_name='claude-1'")[0][0] == 0
        [b] = q(broker, "SELECT b.path, b.kind, b.wake_reason FROM batches b JOIN memberships m"
                        " ON m.id=b.membership_id WHERE m.screen_name='reviewer'")
        assert tuple(b) == ("wait", "wake", "human")
        # the approvals warning is a red room notice after the request
        msgs = broker.web.get("/api/rooms/build/messages").json()["messages"]
        assert msgs[-2]["id"] == msg["id"] and msgs[-1]["kind"] == "notice"
        assert msgs[-1]["text"].startswith("⚠ reviewer may run with approvals off") and msgs[-1]["level"] == "warn"
        assert len(review_events(broker)) == 1
        # the reviewer answers like any member
        assert (await rev.pass_("#build"))["ok"]


async def test_a_held_reviewer_keeps_the_request_queued(broker: InProcBroker, claude: FakeClaude) -> None:
    async with FakeAgent(broker.home, "rev") as rev:
        await rev.join("#build", "reviewer")
        assert web_cmd(broker, "/hold reviewer").status_code == 200
        t = rev.wait_task("#build", 20)
        await asyncio.sleep(0.4)
        r = web_cmd(broker, "/review reviewer claude-1")
        assert r.status_code == 200 and "reviewer is held: it gets the request after /release reviewer" \
            in r.json()["text"]
        [msg] = human_chat(broker)
        await asyncio.sleep(0.6)
        assert not t.done()
        assert deliveries(broker, msg["id"]) == {"reviewer": (2, 1, "pending")}
        assert web_cmd(broker, "/release reviewer").status_code == 200
        got = await asyncio.wait_for(t, 5)
        assert ids_in(got["text"]) == [msg["id"]]


async def test_a_codex_author_through_the_uds(broker: InProcBroker) -> None:
    """The author's thread id comes from its join's ``_meta.threadId``, used once the thread
    proof passed (§9.3); the command comes in over the UDS as ``switchboard cmd`` sends it
    (``human.command``), so it leaves a CLI audit line."""
    cx = FakeClaude(broker, as_harness="codex")
    try:
        assert cx.tool("join", meta={"threadId": "thread-A"}, room="#build", screen_name="codex-1")["ok"]
        async with FakeAgent(broker.home, "rev") as rev:
            await rev.join("#build", "reviewer")
            # no Codex daemon here, so the thread proof can't pass: refused, nothing posted
            with pytest.raises(RpcError) as e:
                broker.call("human.command", {"room": "#build", "text": "/review reviewer codex-1"})
            assert e.value.code == "bad_request" and "codex-1's Codex thread isn't verified yet" in e.value.message
            assert human_chat(broker) == [] and review_events(broker) == []
            assert "transcript" not in web_cmd(broker, "/who").json()["text"]

            def prove() -> None:  # what a passing thread proof records
                st = broker.state.store
                part = st.find_participant("codex", "codex:thread-A")
                st.update_participant(part.id, thread_proof=1)

            broker.on_loop(prove)
            res = broker.call("human.command", {"room": "#build", "text": "/review reviewer codex-1"})
            assert res["ok"] and "(agentsview id codex:thread-A)" in res["text"]
            [msg] = human_chat(broker)
            assert msg["via"] == "cli" and "agentsview session messages codex:thread-A --direction" in msg["text"]
            assert set(deliveries(broker, msg["id"])) == {"reviewer"}
            notices = [m["text"] for m in broker.web.get("/api/rooms/build/messages").json()["messages"]
                       if m["kind"] == "notice"]
            assert any(n.startswith("/review by alice (via cli: ") for n in notices)
    finally:
        cx.close()


# ---------------------------------------------------------------- refusals
async def test_kicked_and_departed_members_fail(broker: InProcBroker, claude: FakeClaude) -> None:
    async with FakeAgent(broker.home, "rev") as rev, FakeAgent(broker.home, "gone") as gone:
        await rev.join("#build", "reviewer")
        await gone.join("#build", "leaver")
        await gone.leave("#build")
        r = web_cmd(broker, "/review reviewer leaver")
        assert r.status_code == 404 and "no such member in #build: leaver" in r.text
        assert web_cmd(broker, "/kick claude-1").status_code == 200
        r = web_cmd(broker, "/review reviewer claude-1")
        assert r.status_code == 404 and "no such member in #build: claude-1" in r.text
        r = web_cmd(broker, "/review leaver reviewer")
        assert r.status_code == 404
        assert human_chat(broker) == [] and review_events(broker) == []


async def test_nothing_is_posted_without_agentsview(broker: InProcBroker, claude: FakeClaude,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    with_agentsview(monkeypatch, found=False)
    async with FakeAgent(broker.home, "rev") as rev:
        await rev.join("#build", "reviewer")
        r = web_cmd(broker, "/review reviewer claude-1")
        assert r.status_code == 400 and r.json()["message"].startswith("/review needs agentsview")
        with pytest.raises(RpcError) as e:
            broker.call("human.command", {"room": "#build", "text": "/review reviewer claude-1"})
        assert e.value.code == "bad_request"
        assert human_chat(broker) == [] and review_events(broker) == []


async def test_an_agent_cannot_run_review(broker: InProcBroker, claude: FakeClaude) -> None:
    """An agent's say("/review ...") is stored literally, as for every command."""
    async with FakeAgent(broker.home, "rev") as rev:
        await rev.join("#build", "reviewer")
        r = await rev.say("#build", "/review reviewer claude-1")
        assert r["ok"]
        msgs = broker.web.get("/api/rooms/build/messages").json()["messages"]
        assert msgs[-1]["text"] == "/review reviewer claude-1" and msgs[-1]["sender_kind"] == "agent"
        assert human_chat(broker) == [] and review_events(broker) == []


# ------------------------------------------------------------------- who
def test_who_shows_the_transcript_to_the_human(broker: InProcBroker, claude: FakeClaude,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    who = web_cmd(broker, "/who").json()["text"]
    assert f"transcript: {SID}" in who
    [m] = broker.call("room.who", {"room": "#build"})["members"]
    assert m["name"] == "claude-1" and m["transcript"] == SID
    # never in the buddy-list rows the web UI and its WebSocket get
    [row] = broker.web.get("/api/rooms/build/members").json()["members"]
    assert "transcript" not in row
    with_agentsview(monkeypatch, found=False)
    assert "transcript" not in web_cmd(broker, "/who").json()["text"]
    assert "transcript" not in broker.call("room.who", {"room": "#build"})["members"][0]


class NotHuman(AllowAllHumans):
    """A same-user caller that fails the human_cli check (e.g. an agent's shell)."""

    def human_cli_allowed(self, peer: Any) -> bool:  # type: ignore[override]
        return False

    def human_allowed(self, peer: Any) -> bool:  # type: ignore[override]
        return False

    def login_allowed(self, peer: Any) -> bool:  # type: ignore[override]
        return peer.uid == os.getuid()  # the fixture still gets a web session


def test_room_who_hides_transcripts_from_an_agents_shell(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with_agentsview(monkeypatch)
    b = start(tmp_home, NotHuman())
    c = FakeClaude(b)
    try:
        assert c.tool("join", room="#build", screen_name="claude-1")["ok"]
        [m] = b.call("room.who", {"room": "#build"})["members"]  # room.who is anon: it still answers
        assert m["name"] == "claude-1" and "transcript" not in m
        with pytest.raises(RpcError) as e:  # and /review isn't available to such a caller at all
            b.call("human.command", {"room": "#build", "text": "/review claude-1 claude-2"})
        assert e.value.code == "forbidden"
        assert f"transcript: {SID}" in b.web.post("/api/rooms/build/command", json={"text": "/who"},
                                                  headers=b.write_headers()).json()["text"]
    finally:
        c.close()
        b.web.close()
        b.stop()
