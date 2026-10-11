"""/catchup against a real in-process broker (DESIGN.md §26).

The agent and a bystander are scripted test agents (``switchboard mcp --harness test``); the
subject is a stand-in Claude or Codex session (``fakes/fake_claude.py``), so its session id
comes through the real join path. ``/catchup`` posts one human chat message: the engine wakes
the agent (and, for a member catch-up, a bystander like any human message), never the subject.
No history tool exists here and none is looked for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent, ids_in
from fakes.fake_claude import SID, FakeClaude

from switchboard.broker import catchup
from switchboard.broker.peer import AllowAllHumans
from switchboard.config import Config, ReviewCfg
from switchboard.mcp.client import RpcError

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
DAY = 24 * 3600.0


def start(tmp_home: Path, policy: Any = None, cfg: Config = FAST) -> InProcBroker:
    b = InProcBroker(tmp_home, cfg, policy=policy).start()
    # the MCP servers read the same config file the broker was built from
    (tmp_home / "config.toml").write_text(f'[claude]\nsessions_dir = "{b.cfg.claude.sessions_dir}"\n')
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    return b


@pytest.fixture
def broker(tmp_home: Path):
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
    rows = q(
        b,
        "SELECT m.screen_name, d.prio, d.mentioned, d.state FROM deliveries d"
        " JOIN memberships m ON m.id=d.membership_id WHERE d.message_id=?",
        message_id,
    )
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def human_chat(b: InProcBroker) -> list[dict[str, Any]]:
    msgs = b.web.get("/api/rooms/build/messages").json()["messages"]
    return [m for m in msgs if m["kind"] == "chat" and m["sender_kind"] == "human"]


def catchup_events(b: InProcBroker) -> list[sqlite3.Row]:
    return q(b, "SELECT data FROM events WHERE kind IN ('catchup', 'review')")


def subject_delivery_rows(b: InProcBroker, name: str) -> int:
    return q(
        b,
        "SELECT COUNT(*) FROM deliveries d JOIN memberships m ON m.id=d.membership_id WHERE m.screen_name=?",
        name,
    )[0][0]


def waiting(b: InProcBroker) -> set[str]:
    """The members whose wait() is open in the broker right now."""
    open_ids = b.on_loop(lambda: {s.membership_id for s in b.state.engine.sinks.open_sinks()})
    return {r["screen_name"] for r in q(b, "SELECT id, screen_name FROM memberships") if r["id"] in open_ids}


async def until_waiting(b: InProcBroker, *names: str) -> None:
    """Until each named member's wait() call is open in the broker: a condition, not a fixed sleep."""
    deadline = time.monotonic() + 10
    while not set(names) <= waiting(b):
        assert time.monotonic() < deadline, f"no open wait() for {names}: {waiting(b)}"
        await asyncio.sleep(0.02)


# ------------------------------------------------------------------ waking
async def test_catchup_wakes_the_agent_not_the_subject(broker: InProcBroker, claude: FakeClaude) -> None:
    async with FakeAgent(broker.home, "ag") as ag, FakeAgent(broker.home, "by") as by:
        await ag.join("#build", "catcher")
        await by.join("#build", "bystander")
        t_ag, t_by = ag.wait_task("#build", 20), by.wait_task("#build", 20)
        await until_waiting(broker, "catcher", "bystander")
        r = web_cmd(broker, "/catchup catcher on claude-1 pick it apart")
        assert r.status_code == 200, r.text
        lines = r.json()["text"].splitlines()
        assert lines[0].startswith("asked catcher to catch up on claude-1's work since ")
        assert (
            lines[1]
            == f"  subject: claude-1 · claude · session {SID} · host: the switchboard machine (yours)"
        )
        assert lines[2] == (
            "⚠ catcher may run with approvals off (its approval mode is unknown): transcripts it reads"
            " may make it act without asking"
        )
        [msg] = human_chat(broker)  # exactly one post
        assert msg["text"].splitlines()[0] == "@catcher please catch up on claude-1's work."
        assert "  note: pick it apart" in msg["text"].splitlines()
        assert msg["from"] == "alice" and msg["via"] == "web" and msg["mentions"] == ["catcher"]
        # the agent's open wait() returns it, addressed to it, through the engine
        got = await asyncio.wait_for(t_ag, 5)
        assert got["status"] == "messages" and ids_in(got["text"]) == [msg["id"]]
        assert "to_you=yes" in got["text"] and "prio=human" in got["text"]
        assert "catch-up request (switchboard)" in got["text"] and f"session {SID}" in got["text"]
        assert "read() shows full" not in got["text"]  # one subject and a short note: shown whole
        # a bystander gets it like any human message (not addressed to it)
        other = await asyncio.wait_for(t_by, 5)
        assert ids_in(other["text"]) == [msg["id"]] and "to_you=no" in other["text"]
        # the subject: no delivery row, so nothing can wake it
        d = deliveries(broker, msg["id"])
        assert set(d) == {"catcher", "bystander"}
        assert d["catcher"][:2] == (2, 1) and d["bystander"][:2] == (2, 0)
        assert subject_delivery_rows(broker, "claude-1") == 0
        [b] = q(
            broker,
            "SELECT b.path, b.kind, b.wake_reason FROM batches b JOIN memberships m"
            " ON m.id=b.membership_id WHERE m.screen_name='catcher'",
        )
        assert tuple(b) == ("wait", "wake", "human")
        # the approvals warning is a red room notice after the request
        msgs = broker.web.get("/api/rooms/build/messages").json()["messages"]
        assert msgs[-2]["id"] == msg["id"] and msgs[-1]["kind"] == "notice"
        assert (
            msgs[-1]["text"].startswith("⚠ catcher may run with approvals off")
            and msgs[-1]["level"] == "warn"
        )
        assert len(catchup_events(broker)) == 1
        # the agent answers like any member
        assert (await ag.pass_("#build"))["ok"]


async def test_room_wide_wakes_only_the_agent(broker: InProcBroker, claude: FakeClaude) -> None:
    async with FakeAgent(broker.home, "ag") as ag, FakeAgent(broker.home, "by") as by:
        await ag.join("#build", "catcher")
        await by.join("#build", "tester")
        t_ag, t_by = ag.wait_task("#build", 20), by.wait_task("#build", 20)
        await until_waiting(broker, "catcher", "tester")
        r = web_cmd(broker, '/catchup catcher on "the parser"')
        assert r.status_code == 200, r.text
        assert (
            r.json()["text"]
            .splitlines()[0]
            .startswith('asked catcher to catch up on "the parser" across 2 session(s) since ')
        )
        t0 = time.time()
        r = web_cmd(broker, "/catchup catcher")
        t1 = time.time()
        assert r.status_code == 200, r.text
        topic, room = human_chat(broker)
        for msg in (topic, room):
            subjects = [x for x in msg["text"].splitlines() if x.startswith("  subject: ")]
            assert [x.split(" · ")[0] for x in subjects] == ["  subject: claude-1", "  subject: tester"]
            assert "no session id: ask tester here for a short summary" in subjects[1]
            assert set(deliveries(broker, msg["id"])) == {"catcher"}  # every other member is a subject
        got = await asyncio.wait_for(t_ag, 5)
        assert ids_in(got["text"])[0] == topic["id"]
        # the others were never woken by either request: the bystander's wait() is still open
        assert "tester" in waiting(broker) and not t_by.done()
        assert subject_delivery_rows(broker, "claude-1") == subject_delivery_rows(broker, "tester") == 0
        t_by.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t_by
        # the agent joined moments ago: the room-wide window is the last 24 h
        window = next(x for x in room["text"].splitlines() if x.startswith("  window: since "))
        assert window in {f"  window: since {catchup.utc(t - DAY)}" for t in (t0, t1)}
        assert [r[0] for r in q(broker, "SELECT kind FROM events WHERE kind='catchup'")] == [
            "catchup",
            "catchup",
        ]


async def test_a_held_agent_keeps_the_request_queued(broker: InProcBroker, claude: FakeClaude) -> None:
    async with FakeAgent(broker.home, "ag") as ag:
        await ag.join("#build", "catcher")
        assert web_cmd(broker, "/hold catcher").status_code == 200
        t = ag.wait_task("#build", 20)
        await until_waiting(broker, "catcher")
        r = web_cmd(broker, "/catchup catcher on claude-1")
        assert (
            r.status_code == 200
            and "catcher is held: it gets the request after /release catcher" in r.json()["text"]
        )
        [msg] = human_chat(broker)
        # the post was evaluated before the command answered: the held agent's wait() is still
        # open and the request queued
        assert "catcher" in waiting(broker) and not t.done()
        assert deliveries(broker, msg["id"]) == {"catcher": (2, 1, "pending")}
        assert web_cmd(broker, "/release catcher").status_code == 200
        got = await asyncio.wait_for(t, 5)
        assert ids_in(got["text"]) == [msg["id"]]


async def test_reviews_old_form_posts_nothing(broker: InProcBroker, claude: FakeClaude) -> None:
    """0.3's /review alias is gone, and the name opens a review board now (#248): the old form
    is refused as a URL that isn't one, and nothing is posted or recorded as a catch-up."""
    async with FakeAgent(broker.home, "ag") as ag:
        await ag.join("#build", "reviewer")
        r = web_cmd(broker, "/review reviewer claude-1 focus on the error paths")
        assert r.status_code == 400
        assert "/review: url must be an http(s) link to the pull request or merge request" in r.text
        assert human_chat(broker) == [] and catchup_events(broker) == []


async def test_a_codex_subject_through_the_uds(broker: InProcBroker) -> None:
    """The subject's thread id comes from its join's ``_meta.threadId``, sent once the thread
    proof passed (§9.3); before that its line says to ask it. The command comes in over the
    UDS as ``switchboard cmd`` sends it (``human.command``), so it leaves a CLI audit line."""
    cx = FakeClaude(broker, as_harness="codex")
    try:
        assert cx.tool("join", meta={"threadId": "thread-A"}, room="#build", screen_name="codex-1")["ok"]
        async with FakeAgent(broker.home, "ag") as ag:
            await ag.join("#build", "catcher")
            # no Codex daemon here, so the thread proof can't pass: no id yet, but the request goes out
            res = broker.call("human.command", {"room": "#build", "text": "/catchup catcher on codex-1"})
            assert res["ok"] and "no session id: ask codex-1 here for a short summary" in res["text"]
            assert "(its Codex thread isn't verified yet)" in res["text"]
            assert "session:" not in web_cmd(broker, "/who").json()["text"]

            def prove() -> None:  # what a passing thread proof records
                st = broker.state.store
                part = st.find_participant("codex", "codex:thread-A")
                st.update_participant(part.id, thread_proof=1)

            broker.on_loop(prove)
            res = broker.call("human.command", {"room": "#build", "text": "/catchup catcher on codex-1"})
            assert res["ok"] and "codex-1 · codex · session thread-A · host" in res["text"]
            first, msg = human_chat(broker)
            assert "no session id: ask codex-1 here" in first["text"]
            assert msg["via"] == "cli" and "codex-1 · codex · session thread-A · host" in msg["text"]
            assert set(deliveries(broker, msg["id"])) == {"catcher"}
            assert "session: thread-A @ this machine" in web_cmd(broker, "/who").json()["text"]
            notices = [
                m["text"]
                for m in broker.web.get("/api/rooms/build/messages").json()["messages"]
                if m["kind"] == "notice"
            ]
            assert sum(1 for n in notices if n.startswith("/catchup by alice (via cli: ")) == 2
    finally:
        cx.close()


# ---------------------------------------------------------------- refusals
async def test_kicked_and_departed_members_fail(broker: InProcBroker, claude: FakeClaude) -> None:
    async with FakeAgent(broker.home, "ag") as ag, FakeAgent(broker.home, "gone") as gone:
        await ag.join("#build", "catcher")
        await gone.join("#build", "leaver")
        await gone.leave("#build")
        r = web_cmd(broker, "/catchup catcher on leaver")
        assert r.status_code == 404 and "no such member in #build: leaver" in r.text
        assert web_cmd(broker, "/kick claude-1").status_code == 200
        r = web_cmd(broker, "/catchup catcher on claude-1")
        assert r.status_code == 404 and "no such member in #build: claude-1" in r.text
        r = web_cmd(broker, "/catchup leaver")
        assert r.status_code == 404
        r = web_cmd(broker, "/catchup catcher")
        assert r.status_code == 400 and "there is nobody to catch up on" in r.text
        r = web_cmd(broker, "/catchup catcher on catcher")
        assert r.status_code == 400 and "can't catch up on itself" in r.text
        assert human_chat(broker) == [] and catchup_events(broker) == []


async def test_an_agent_cannot_run_catchup(broker: InProcBroker, claude: FakeClaude) -> None:
    """An agent's say("/catchup ...") is stored literally, as for every command."""
    async with FakeAgent(broker.home, "ag") as ag:
        await ag.join("#build", "catcher")
        r = await ag.say("#build", "/catchup catcher on claude-1")
        assert r["ok"]
        msgs = broker.web.get("/api/rooms/build/messages").json()["messages"]
        assert msgs[-1]["text"] == "/catchup catcher on claude-1" and msgs[-1]["sender_kind"] == "agent"
        assert human_chat(broker) == [] and catchup_events(broker) == []


# ------------------------------------------------------------------- who
def test_who_shows_the_session_to_the_human(broker: InProcBroker, claude: FakeClaude) -> None:
    who = web_cmd(broker, "/who").json()["text"]
    assert f"session: {SID} @ this machine" in who
    [m] = broker.call("room.who", {"room": "#build"})["members"]
    assert m["name"] == "claude-1" and m["session"] == f"{SID} @ this machine"
    # never in the buddy-list rows the web UI and its WebSocket get
    [row] = broker.web.get("/api/rooms/build/members").json()["members"]
    assert "session" not in row and "transcript" not in row


class NotHuman(AllowAllHumans):
    """A same-user caller that fails the human_cli check (e.g. an agent's shell)."""

    def human_cli_allowed(self, peer: Any) -> bool:  # type: ignore[override]
        return False

    def human_allowed(self, peer: Any) -> bool:  # type: ignore[override]
        return False

    def login_allowed(self, peer: Any) -> bool:  # type: ignore[override]
        return peer.uid == os.getuid()  # the fixture still gets a web session


def test_room_who_hides_sessions_from_an_agents_shell(tmp_home: Path) -> None:
    b = start(tmp_home, NotHuman())
    c = FakeClaude(b)
    try:
        assert c.tool("join", room="#build", screen_name="claude-1")["ok"]
        [m] = b.call("room.who", {"room": "#build"})["members"]  # room.who is anon: it still answers
        assert m["name"] == "claude-1" and "session" not in m
        # the agents' own who() never carries it either
        r = c.tool("who", room="#build")
        assert r["ok"] and SID not in str(r)
        with pytest.raises(RpcError) as e:  # and /catchup isn't available to such a caller at all
            b.call("human.command", {"room": "#build", "text": "/catchup claude-1"})
        assert e.value.code == "forbidden"
        assert (
            f"session: {SID} @ this machine"
            in b.web.post(
                "/api/rooms/build/command", json={"text": "/who"}, headers=b.write_headers()
            ).json()["text"]
        )
    finally:
        c.close()
        b.web.close()
        b.stop()


# ---------------------------------------------------------------- config
def test_the_ignored_config_key_is_logged_once_at_start(
    tmp_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A 0.2.0 ``[review] agentsview`` still loads (it is ignored); the broker says so once."""
    cfg = FAST.replace(review=ReviewCfg(agentsview="/opt/x/agentsview"))
    with caplog.at_level(logging.WARNING, logger="switchboard.broker"):
        b = InProcBroker(tmp_home, cfg).start()
        try:
            b.restart()  # once per start
        finally:
            b.stop()
    hits = [r.getMessage() for r in caplog.records if "[review] agentsview" in r.getMessage()]
    assert (
        hits
        == ["config.toml: [review] agentsview is ignored since /catchup replaced /review; you can remove it"]
        * 2
    )


def test_no_warning_without_the_key(tmp_home: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="switchboard.broker"):
        InProcBroker(tmp_home, FAST).start().stop()
    assert not [r for r in caplog.records if "[review]" in r.getMessage()]
