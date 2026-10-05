"""The two ends of the Claude inbox relay, without processes (DESIGN.md §6.4, §9.2):
the MCP server's ``deliver`` (guard, text check, ``mcp.posted``) and the
broker adapter's ``send``/``posted`` handshake."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fakes.fake_claude_inbox import FakeInbox

from switchboard.adapters.claude import ClaudeAdapter, SendError
from switchboard.config import Config
from switchboard.mcp import claude_inbox as ci
from switchboard.mcp import server as srv
from switchboard.mcp.client import BrokerConn


class RecConn(BrokerConn):
    def __init__(self) -> None:
        super().__init__("/nonexistent.sock")
        self.notes: list[tuple[str, dict[str, Any]]] = []

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notes.append((method, params))


def state(inbox: ci.InboxTarget | None, attached: bool = True) -> srv.McpState:
    st = srv.McpState(
        RecConn(),
        env={"CLAUDECODE": "1", ci.SOCKET_ENV: inbox.sock if inbox else "", ci.TOKEN_ENV: ""},
        parent_argv="/x/claude",
        ppid=os.getppid(),
        sessions_dir="/nonexistent",
        inbox_hold_s=0.0,
    )
    st.inbox = inbox
    st.inbox_attached = attached
    return st


@pytest.fixture
def fake_inbox(monkeypatch: pytest.MonkeyPatch):
    d = Path(tempfile.mkdtemp(prefix="yk-ir-", dir="/tmp"))
    ib = FakeInbox(str(d / "in.sock"))
    monkeypatch.setenv(ci.SOCKET_ENV, ib.path)
    monkeypatch.setenv(ci.TOKEN_ENV, "tok-unit")
    yield ib
    ib.close()
    for p in d.iterdir():
        p.unlink()
    d.rmdir()


async def test_deliver_posts_into_the_verified_parent_socket_and_reports(fake_inbox: FakeInbox) -> None:
    st = state(ci.InboxTarget(fake_inbox.path, os.getppid()))
    await st.deliver({"batch_id": 9, "text": "[switchboard] #build: hi", "room": "#build", "sender": "alice"})
    [(conn, frame)] = fake_inbox.wait_frames(1)
    assert conn.lines[0] == {"type": "auth", "token": "tok-unit"}
    assert frame["from"] == "switchboard:#build/alice" and frame["msg_id"].startswith("yk-b9-")
    [(method, res)] = st.conn.notes
    assert method == "mcp.posted" and res["batch_id"] == 9 and res["ok"] is True
    assert isinstance(res["t_post"], float) and "tok-unit" not in repr(res)


@pytest.mark.parametrize(
    "case", ["not_attached", "no_target", "bad_text", "not_switchboard_text", "no_batch"]
)
async def test_deliver_refuses(case: str, fake_inbox: FakeInbox) -> None:
    target = ci.InboxTarget(fake_inbox.path, os.getppid())
    st = state(None if case == "no_target" else target, attached=case != "not_attached")
    data: dict[str, Any] = {"batch_id": 3, "text": "[switchboard] x", "room": "#build", "sender": "alice"}
    if case == "bad_text":
        data["text"] = ""
    if case == "not_switchboard_text":
        data["text"] = "/clear"
    if case == "no_batch":
        data["batch_id"] = "3"
    await st.deliver(data)
    await asyncio.sleep(0.05)
    assert fake_inbox.frames() == []
    if case == "no_batch":
        assert st.conn.notes == []
    else:
        [(_m, res)] = st.conn.notes
        assert res["ok"] is False and res["err"] in ("guard", "bad_text")


def test_only_deliver_pushes_are_acted_on() -> None:
    st = state(None)
    st.on_push({"push": "message", "data": {"batch_id": 1, "text": "[switchboard] x"}})
    st.on_push({"push": "deliver", "data": "nope"})
    assert st._tasks == set()


# ----------------------------------------------------------- broker side
class Chan:
    closed = False

    def __init__(self) -> None:
        self.pushes: list[tuple[str, dict[str, Any]]] = []

    def push(self, kind: str, data: dict[str, Any]) -> None:
        self.pushes.append((kind, data))


class P:
    """Just the participant fields the adapter reads."""

    def __init__(self, pid: int = 1) -> None:
        self.id = pid
        self.mcp_pid = 4242
        self.mcp_start = 100.0
        self.claude_socket = "/tmp/x.sock"


class B:
    def __init__(self, bid: int) -> None:
        self.id = bid


async def test_send_waits_for_posted_from_the_same_channel() -> None:
    a = ClaudeAdapter(Config())
    ch, other = Chan(), Chan()
    a.attach(4242, 100.0, ch)
    task = asyncio.create_task(a.send(P(), B(5), "[switchboard] hi", room="#build", sender="alice"))
    await asyncio.sleep(0.01)
    assert ch.pushes == [
        ("deliver", {"batch_id": 5, "text": "[switchboard] hi", "room": "#build", "sender": "alice"})
    ]
    assert a.posted(5, other, {"ok": True, "t_post": 1.0}) is False  # another connection can't settle it
    assert a.posted(5, ch, {"ok": True, "t_post": 123.5}) is True
    assert await task == 123.5


async def test_send_failures_raise_and_back_off() -> None:
    a = ClaudeAdapter(Config())
    with pytest.raises(SendError):
        await a.send(P(), B(1), "[switchboard] x")  # not attached
    assert 1 in a.backoff
    ch = Chan()
    a.attach(4242, 100.0, ch)
    task = asyncio.create_task(a.send(P(2), B(2), "[switchboard] x"))
    await asyncio.sleep(0.01)
    a.posted(2, ch, {"ok": False, "err": "guard"})
    with pytest.raises(SendError, match="guard"):
        await task
    # a lost channel fails what was in flight on it
    task = asyncio.create_task(a.send(P(3), B(3), "[switchboard] x"))
    await asyncio.sleep(0.01)
    assert a.detach(ch) == [4242]
    with pytest.raises(SendError, match="disconnected"):
        await task
    assert a.conn_for(P()) is None


def test_a_recycled_mcp_pid_is_not_the_channel() -> None:
    a = ClaudeAdapter(Config())
    a.attach(4242, 100.0, Chan())
    p = P()
    p.mcp_start = 200.0
    assert a.conn_for(p) is None and a.tier(p) == ("claude:hook", None)
