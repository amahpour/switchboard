"""A Claude Code session on a remote host is first-class over the link (DESIGN.md §27.5.6, §27.7).

The Pi session is ``fakes/fake_claude.py`` run with the Pi home: its MCP server and hooks
dial the Pi home's satellite, it writes its registry into the Pi's sessions dir, and it
serves a fake inbox socket on the Pi side. The broker's own sessions dir stays empty, so
only the Pi's registry, read by the satellite, can vouch for it or hold it. The satellite
runs with the PID shift, so a desktop probe of a Pi pid would find no process.

Covered: the verified tier ``claude:inbox``; an idle wake posted into the Pi inbox and
confirmed by the UserPromptSubmit token; the relayed ``waiting`` registry holding every
delivery; the satellite's last-mile check (a status that changed after the broker's view
means no frame, then delivery once idle); a stalled satellite (no push on a stale view);
the bypass mid-task push only while busy; a nested Claude's hooks inert; ``/clear``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from fakes.fake_agent import ids_in
from fakes.fake_claude import SID, TOKEN, FakeClaude, fixture
from fakes.fake_link import PID_SHIFT, FakeLink, wait_for

from switchboard.config import Config
from switchboard.install.common import hook_command
from switchboard.paths import hook_sha12

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
FAST = FAST.replace(claude=dataclasses.replace(FAST.claude, inbox_idle_expire_s=30.0))


@pytest.fixture
def link() -> Any:
    lk = FakeLink(kind="inproc", broker_cfg=FAST)
    try:
        lk.start()
        yield lk
    finally:
        lk.close()


@pytest.fixture
def fc(link: FakeLink) -> Any:
    c = FakeClaude(None, inbox=True, home=link.pi, sessions_dir=link.pi_sessions)
    try:
        yield c
    finally:
        c.close()


def say(link: FakeLink, text: str) -> int:
    return link.call("human.say", {"room": "#fpga", "text": text})["id"]


def q(link: FakeLink, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{link.desk_paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def part(link: FakeLink) -> sqlite3.Row:
    return q(link, "SELECT * FROM participants WHERE harness='claude' ORDER BY id")[-1]


def member(link: FakeLink, name: str = "bench") -> dict[str, Any]:
    return next(m for m in link.members() if m["name"] == name)


def state(link: FakeLink, mid: int) -> str:
    return q(link, "SELECT state FROM deliveries WHERE message_id=?", mid)[0][0]


def inbox_batches(link: FakeLink) -> list[sqlite3.Row]:
    return q(link, "SELECT * FROM batches WHERE path='inbox' ORDER BY id")


def relayed(link: FakeLink) -> Any:
    """The broker's registry view of the Pi Claude (relayed by the satellite)."""
    p = part(link)
    return link.broker.state.engine.adapters["claude"].registry.get(("fpga-pi", p["agent_pid"]))


def joined_idle(fc: FakeClaude, link: FakeLink) -> None:
    """Join from the Pi, then a turn ends: idle, hooks seen, a relayed view that says idle."""
    j = fc.tool("join", room="#fpga", screen_name="bench")
    assert j["ok"] and j["tier"] == "claude:inbox", j
    fc.hook(fixture("PostToolUse_mcp"))
    fc.hook(fixture("Stop"))
    assert wait_for(lambda: part(link)["status"] == "idle", what="idle")
    assert wait_for(
        lambda: (v := relayed(link)) is not None and v.status == "idle", what="a relayed idle view"
    )


def turn_from(fc: FakeClaude, text: str) -> str:
    """Claude starts a turn from an inbox frame: UserPromptSubmit, prompt = the body."""
    return fc.hook(fixture("UserPromptSubmit", prompt=text))


# ----------------------------------------------------------------------------
def test_pi_claude_verified_tier_inbox(link: FakeLink, fc: FakeClaude) -> None:
    j = fc.tool("join", room="#fpga", screen_name="bench")
    assert j["ok"] and j["tier"] == "claude:inbox", j
    assert (
        "arrive as a message from switchboard" in j["text"] and "You don't need to call wait()" in j["text"]
    )
    p = part(link)
    assert p["host"] == "fpga-pi" and p["agent_pid"] == fc.pid + PID_SHIFT
    assert p["tier"] == "claude:inbox" and p["claude_socket"] == fc.inbox_path
    m = member(link)
    assert (m["harness"], m["tier"], m["host"]) == ("claude", "claude:inbox", "fpga-pi")
    # only the Pi's registry vouched for it: the broker's own sessions dir holds nothing
    reg_dir = Path(link.broker.cfg.claude.sessions_dir)
    assert not reg_dir.exists() or not list(reg_dir.glob("*.json"))
    # the channel is keyed by (host, the MCP server's pid as the link reports it)
    conns = link.broker.state.engine.adapters["claude"].conns
    assert list(conns) == [("fpga-pi", p["mcp_pid"])]
    # and its registry arrives relayed, within a round trip of the join
    assert wait_for(lambda: relayed(link) is not None and relayed(link).status == "idle", what="relayed view")


def test_idle_wake_lands_in_pi_inbox_and_token_confirms(link: FakeLink, fc: FakeClaude) -> None:
    joined_idle(fc, link)
    mid = say(link, "flash the new bitstream")
    [(conn, frame)] = fc.inbox.wait_frames(1)
    # the Pi MCP server posted it, with the Pi session's own token
    assert conn.lines[0] == {"type": "auth", "token": TOKEN}
    assert set(frame) == {"type", "message", "from", "msg_id"}
    assert frame["from"] == "switchboard:#fpga/alice" and "chk" not in conn.raw.decode()
    body = frame["message"]["content"]
    assert body.startswith("[switchboard] #fpga: 1 message from alice") and ids_in(body) == [mid]
    [b] = inbox_batches(link)
    assert b["state"] == "offered" and b["wake_kind"] == "idle_wake" and b["posted_at"] is not None
    # the turn starts from the frame: the Pi hook's UserPromptSubmit carries the token
    assert turn_from(fc, body) == ""
    [b] = inbox_batches(link)
    assert b["state"] == "confirmed" and b["evidence"] == "hook:UserPromptSubmit"
    assert b["turn_start_at"] is not None and abs(b["turn_start_at"] - time.time()) < 5
    assert state(link, mid) == "in_context" and part(link)["status"] == "busy"
    assert part(link)["push_expiries"] == 0


def test_waiting_registry_holds_and_shows_waiting_approval(link: FakeLink, fc: FakeClaude) -> None:
    joined_idle(fc, link)
    fc.hook(fixture("UserPromptSubmit"))
    fc.set_registry("waiting")  # an approval prompt opened on the Pi
    assert wait_for(lambda: part(link)["status"] == "waiting-approval", what="waiting-approval")
    assert wait_for(lambda: member(link)["status"] == "waiting-approval", what="member waiting-approval")
    mid = say(link, "held while you decide")
    time.sleep(1.0)
    assert fc.inbox.frames() == [] and state(link, mid) == "pending"
    assert q(link, "SELECT COUNT(*) FROM batches")[0][0] == 0
    assert fc.hook(fixture("PostToolUse_bash")) == ""  # no hook context either
    # declined on the Pi: the registry goes idle, the turn is over; the message goes out now
    fc.set_registry("idle")
    [(_c, frame)] = fc.inbox.wait_frames(1)
    assert ids_in(frame["message"]["content"]) == [mid]
    assert part(link)["status"] == "idle"
    srcs = [json.loads(e["data"]).get("src") for e in q(link, "SELECT data FROM events WHERE kind='status'")]
    assert "claude:registry" in srcs


def test_registry_flips_before_post_no_frame_then_delivered(link: FakeLink, fc: FakeClaude) -> None:
    """The broker routes on a relayed idle view; the Pi session gets busy a moment before
    the post (the owner typed there). The satellite's fresh read refuses the frame: nothing
    lands in the inbox, the batch is re-routed uncounted, and it goes out once idle again."""
    joined_idle(fc, link)
    rl = link.broker.state.remotes.links[link.name]
    real = rl.send_frame
    checked: list[dict[str, Any]] = []

    def flip_first(att: Any, frame: dict[str, Any]) -> None:
        if frame.get("t") == "out" and "chk" in frame:
            checked.append(frame["chk"])
            if len(checked) == 1:
                fc.set_registry("busy")  # between the broker's view and the satellite's read
        real(att, frame)

    rl.send_frame = flip_first  # type: ignore[method-assign]
    try:
        mid = say(link, "one for the bench")
        assert wait_for(
            lambda: [b["expire_reason"] for b in inbox_batches(link)] == ["reroute"], what="reroute"
        )
        assert fc.inbox.frames() == [] and state(link, mid) == "pending"
        assert checked[0]["want"] == "idle" and checked[0]["pid"] == part(link)["agent_pid"]
        assert part(link)["push_expiries"] == 0  # uncounted
        assert wait_for(lambda: relayed(link).status == "busy", what="the fresh view")
        time.sleep(0.6)
        assert fc.inbox.frames() == [] and len(inbox_batches(link)) == 1  # no retry while busy
        fc.set_registry("idle")
        [(_c, frame)] = fc.inbox.wait_frames(1)
        assert ids_in(frame["message"]["content"]) == [mid]
        assert [b["state"] for b in inbox_batches(link)] == ["expired", "offered"]
        # the refused wake reached nobody: its budget unit went back to the room
        assert [b["budget_counted"] for b in inbox_batches(link)] == [0, 1]
        room = q(link, "SELECT budget_per_hour, budget_remaining FROM rooms WHERE name='#fpga'")[0]
        assert room["budget_remaining"] == room["budget_per_hour"] - 1
    finally:
        rl.send_frame = real  # type: ignore[method-assign]


def test_satellite_stalled_no_push_then_recovers(link: FakeLink, fc: FakeClaude) -> None:
    joined_idle(fc, link)
    sat = link.satellite_pid()
    assert sat
    os.kill(sat, signal.SIGSTOP)
    try:
        time.sleep(1.7)  # the last relayed view is now older than the remote fresh limit (1.5 s)
        mid = say(link, "while the link is stalled")
        time.sleep(0.3)
        assert inbox_batches(link) == [] and state(link, mid) == "pending"
        assert not member(link)["parked"]  # not yet: that takes 5 s without a view
    finally:
        os.kill(sat, signal.SIGCONT)
    [(_c, frame)] = fc.inbox.wait_frames(1, timeout=8)
    assert ids_in(frame["message"]["content"]) == [mid]
    assert link.status()["state"] == "up"


def test_bypass_mid_task_push_only_while_busy(link: FakeLink, fc: FakeClaude) -> None:
    joined_idle(fc, link)
    rl = link.broker.state.remotes.links[link.name]
    real = rl.send_frame
    wants: list[str] = []

    def record(att: Any, frame: dict[str, Any]) -> None:
        if frame.get("t") == "out" and "chk" in frame:
            wants.append(frame["chk"]["want"])
        real(att, frame)

    rl.send_frame = record  # type: ignore[method-assign]
    try:
        fc.hook(fixture("UserPromptSubmit", permission_mode="bypassPermissions"))
        fc.set_registry("busy")  # the turn is running
        fc.hook(fixture("PostToolUse_bypass"))
        assert part(link)["approval_mode"] == "bypass"
        assert wait_for(lambda: relayed(link).status == "busy", what="relayed busy")
        m1 = say(link, "stop and look at this")
        [(_c, f1)] = fc.inbox.wait_frames(1)
        body = f1["message"]["content"]
        assert ids_in(body) == [m1] and wants == ["busy"]
        [b] = inbox_batches(link)
        assert b["kind"] == "priority" and b["budget_counted"] == 0
        # taken up at the next tool boundary (still in bypass mode)
        fc.hook(fixture("UserPromptSubmit", prompt=body, permission_mode="bypassPermissions"))
        assert wait_for(lambda: state(link, m1) == "in_context", what="in context")
        # an approval prompt opens (the human switched modes): no frame while it is open
        fc.set_registry("waiting")
        assert wait_for(lambda: part(link)["status"] == "waiting-approval", what="waiting-approval")
        m2 = say(link, "not now")
        time.sleep(1.0)
        assert len(fc.inbox.frames()) == 1 and state(link, m2) == "pending"
        # approved: the turn runs again, and the priority frame goes to the inbox
        fc.set_registry("busy")
        fr = fc.inbox.wait_frames(2)
        assert len(fr) == 2 and ids_in(fr[1][1]["message"]["content"]) == [m2] and wants == ["busy", "busy"]
    finally:
        rl.send_frame = real  # type: ignore[method-assign]


NESTED = "import subprocess, sys\nsys.exit(subprocess.run(sys.argv[1], shell=True).returncode)\n"


def test_nested_claude_on_pi_is_inert(link: FakeLink, fc: FakeClaude) -> None:
    """`claude -p` run from the Pi Claude's Bash fires the same hooks, through the same
    satellite: they must not claim the outer member's context, flip its status or re-key it."""
    fc.tool("join", room="#fpga", screen_name="bench")
    fc.hook(fixture("UserPromptSubmit"))
    mid = say(link, "for the outer session only")
    d = Path(tempfile.mkdtemp(prefix="yk-nest-", dir="/tmp"))
    try:
        nested = d / "claude"  # argv ends in /claude: another Claude session in between
        nested.write_text(NESTED)
        other = "00000000-0000-4000-8000-00000000beef"
        for name in ("PostToolUse_bash", "Stop", "SessionStart_startup", "SessionEnd_exit"):
            payload = fixture(name, session_id=other)
            cmd = hook_command(
                sys.executable, str(link.pi), hook_sha12(), "claude", payload["hook_event_name"]
            )
            cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(nested))} {shlex.quote(cmd)}"
            fc.p.stdin.write(json.dumps({"op": "hook", "command": cmd, "payload": payload}) + "\n")
            fc.p.stdin.flush()
            r = fc.recv()
            assert r["rc"] == 0 and r["stdout"] == "", r
        p = part(link)
        assert p["status"] == "busy" and p["session_id"] == SID and state(link, mid) == "pending"
        assert p["ended_at"] is None and fc.inbox.frames() == []
        # the outer session's own hook still gets it
        out = json.loads(fc.hook(fixture("PostToolUse_bash")))
        assert ids_in(out["hookSpecificOutput"]["additionalContext"]) == [mid]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_clear_keeps_hooks_resolving(link: FakeLink, fc: FakeClaude) -> None:
    joined_idle(fc, link)
    before = part(link)["id"]
    new_sid = "00000000-0000-4000-8000-00000000c1ff"
    fc.hook(fixture("SessionEnd_clear"))
    out = json.loads(fc.hook(fixture("SessionStart_clear", session_id=new_sid)))
    assert "#fpga as bench" in out["hookSpecificOutput"]["additionalContext"]
    p = part(link)
    assert (
        p["id"] == before and p["session_id"] == new_sid and p["status"] == "idle" and p["ended_at"] is None
    )
    assert p["tier"] == "claude:inbox"
    mid = say(link, "after the clear")
    [(_c, frame)] = fc.inbox.wait_frames(1)
    turn_from(fc, frame["message"]["content"])
    assert wait_for(lambda: state(link, mid) == "in_context", what="in context")


def test_the_session_token_never_crosses_the_link(tmp_path: Path) -> None:
    """The Pi session's messaging token stays on the Pi (its MCP server reads it at post
    time): not in any frame either way. ``chk`` goes out beside the push; the relayed
    registry carries statuses and ages only (no socket path, no session id: the session
    id crosses only where it did before, in the hello and the hook payloads)."""
    log = tmp_path / "frames.log"
    lk = FakeLink(kind="inproc", broker_cfg=FAST, env={"SWITCHBOARD_TEST_FRAME_LOG": str(log)})
    c = None
    try:
        lk.start()
        c = FakeClaude(None, inbox=True, home=lk.pi, sessions_dir=lk.pi_sessions)
        joined_idle(c, lk)
        mid = say(lk, "a message for the bench")
        [(_conn, frame)] = c.inbox.wait_frames(1)
        turn_from(c, frame["message"]["content"])
        assert wait_for(lambda: state(lk, mid) == "in_context", what="in context")
    finally:
        if c is not None:
            c.close()
        lk.close()
    text = log.read_text()
    assert TOKEN not in text
    frames = [json.loads(line[2:]) for line in text.splitlines()]
    outs = [f for f in frames if f["t"] == "out" and "chk" in f]
    assert outs and all(set(f["chk"]) == {"pid", "start", "want"} for f in outs)
    assert all("chk" not in json.dumps(f["line"]) for f in outs)
    regs = [f for f in frames if f["t"] == "reg"]
    assert regs and all(set(f) == {"t", "views", "read_age"} for f in regs)
    assert not any("messagingSocketPath" in json.dumps(f) or "sessionId" in json.dumps(f) for f in regs)
