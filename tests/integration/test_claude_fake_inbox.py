"""Claude inbox delivery end to end, against a fake inbox socket and a fake
sessions registry (DESIGN.md §12.2 "Fake Claude inbox", §9.2, §8.7).

The session is ``fakes/fake_claude.py``: a process named ``claude`` that
writes ``<sessions>/<pid>.json``, runs the real ``switchboard mcp`` as its child
and fires hooks as its children. The broker, the MCP server, the hook script,
the attach handshake, the guard and the post are all the real thing; the
test plays Claude's part (a frame starts a turn: UserPromptSubmit with the
frame body as the prompt; the registry file shows idle/busy/waiting).
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import InProcBroker
from fakes.fake_agent import ids_in
from fakes.fake_claude import TOKEN, FakeClaude, fixture
from switchboard.config import Config

IDLE_EXPIRE_S = 0.6
FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
FAST = FAST.replace(claude=dataclasses.replace(FAST.claude, inbox_idle_expire_s=IDLE_EXPIRE_S))


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    # the MCP server reads the same config file the broker was built from
    (tmp_home / "config.toml").write_text(f'[claude]\nsessions_dir = "{b.cfg.claude.sessions_dir}"\n')
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


@pytest.fixture
def claude(broker: InProcBroker):
    c = FakeClaude(broker, inbox=True)
    yield c
    c.close()


def say(b: InProcBroker, text: str) -> int:
    return b.web.post("/api/rooms/build/say", json={"text": text}, headers=b.write_headers()).json()["id"]


def q(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def part(b: InProcBroker) -> sqlite3.Row:
    [row] = q(b, "SELECT * FROM participants WHERE harness='claude'")
    return row


def state(b: InProcBroker, mid: int) -> str:
    return q(b, "SELECT state FROM deliveries WHERE message_id=?", mid)[0][0]


def member(b: InProcBroker, name: str = "claude-1") -> dict[str, Any]:
    ms = b.web.get("/api/rooms/build/members").json()["members"]
    return next(m for m in ms if m["name"] == name)


def wait_for(fn, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.03)
    return fn()


def joined_idle(claude: FakeClaude, broker: InProcBroker) -> None:
    """Join, then a turn ends: the session is idle with hooks seen."""
    j = claude.tool("join", room="#build", screen_name="claude-1")
    assert j["ok"] and j["tier"] == "claude:inbox", j
    claude.hook(fixture("PostToolUse_mcp"))
    claude.hook(fixture("Stop"))
    assert wait_for(lambda: part(broker)["status"] == "idle")


def turn_from(claude: FakeClaude, text: str) -> str:
    """Claude starts a turn from an inbox frame: UserPromptSubmit, prompt = the body."""
    return claude.hook(fixture("UserPromptSubmit", prompt=text))


# ----------------------------------------------------------------------------
def test_attach_gives_the_inbox_tier(broker: InProcBroker, claude: FakeClaude) -> None:
    j = claude.tool("join", room="#build", screen_name="claude-1")
    assert j["ok"] and j["tier"] == "claude:inbox"
    assert "arrive as a message from switchboard" in j["text"]
    assert member(broker)["tier"] == "claude:inbox"
    assert part(broker)["claude_socket"] == claude.inbox_path


def test_idle_wake_frame_format_and_confirmation(broker: InProcBroker, claude: FakeClaude) -> None:
    joined_idle(claude, broker)
    mid = say(broker, "please add validation to parse_port")
    [(conn, frame)] = claude.inbox.wait_frames(1)
    # the recipe: auth line with the session token, then one plain-string user frame
    assert conn.lines[0] == {"type": "auth", "token": TOKEN}
    assert set(frame) == {"type", "message", "from", "msg_id"}
    assert frame["message"]["role"] == "user" and isinstance(frame["message"]["content"], str)
    assert frame["from"] == "switchboard:#build/alice" and frame["msg_id"].startswith("yk-b")
    assert "priority" not in conn.raw.decode()
    body = frame["message"]["content"]
    assert body.startswith("[switchboard] #build: 1 message from alice") and ids_in(body) == [mid]
    assert wait_for(lambda: conn.held_s is not None and conn.held_s >= 0.25, 3)
    [b] = q(broker, "SELECT * FROM batches WHERE path='inbox'")
    assert b["state"] == "offered" and b["posted_at"] is not None and b["wake_kind"] == "idle_wake"
    # Claude starts the turn: its UserPromptSubmit carries the body, and so the token
    out = turn_from(claude, body)
    assert out == ""  # nothing else pending: no extra context
    [b] = q(broker, "SELECT * FROM batches WHERE path='inbox'")
    assert b["state"] == "confirmed" and b["evidence"] == "hook:UserPromptSubmit"
    assert b["turn_start_at"] is not None
    [m] = q(broker, "SELECT ts FROM messages WHERE id=?", mid)
    print(f"fake inbox: message -> frame {(conn.t_accept - m['ts']) * 1000:.1f} ms,"
          f" -> turn start (fake UPS) {(b['turn_start_at'] - m['ts']) * 1000:.1f} ms")
    assert state(broker, mid) == "in_context" and part(broker)["status"] == "busy"


def test_idle_wake_waits_for_the_registry_to_say_idle(broker: InProcBroker, claude: FakeClaude) -> None:
    joined_idle(claude, broker)
    claude.set_registry("busy")
    time.sleep(0.4)
    mid = say(broker, "x")
    time.sleep(1.0)
    assert claude.inbox.frames() == [] and state(broker, mid) == "pending"
    assert not member(broker)["parked"]
    claude.set_registry("idle")
    [(_c, frame)] = claude.inbox.wait_frames(1)
    assert ids_in(frame["message"]["content"]) == [mid]


def test_mid_task_prompting_member_gets_posttooluse_context_only(broker: InProcBroker,
                                                                 claude: FakeClaude) -> None:
    joined_idle(claude, broker)
    claude.hook(fixture("UserPromptSubmit"))  # the human typed: busy, default mode
    mid = say(broker, "also check the tests")
    time.sleep(0.5)
    assert claude.inbox.frames() == []
    out = json.loads(claude.hook(fixture("PostToolUse_bash")))
    assert ids_in(out["hookSpecificOutput"]["additionalContext"]) == [mid]
    assert wait_for(lambda: state(broker, mid) == "in_context")
    assert claude.inbox.frames() == []


def test_mid_task_bypass_member_gets_the_inbox_and_the_warning_badge(broker: InProcBroker,
                                                                    claude: FakeClaude) -> None:
    joined_idle(claude, broker)
    claude.hook(fixture("UserPromptSubmit", permission_mode="bypassPermissions"))
    claude.set_registry("busy")  # the turn is running (mid-task inbox needs a fresh busy read)
    claude.hook(fixture("PostToolUse_bypass"))
    assert part(broker)["approval_mode"] == "bypass"
    assert wait_for(lambda: member(broker)["approval_mode"] == "bypass")  # the ⚠ in the buddy list
    mid = say(broker, "stop and look at this")
    [(_c, frame)] = claude.inbox.wait_frames(1)
    body = frame["message"]["content"]
    assert ids_in(body) == [mid]
    [b] = q(broker, "SELECT * FROM batches WHERE path='inbox'")
    assert b["kind"] == "priority" and b["budget_counted"] == 0
    assert claude.hook(fixture("PostToolUse_bypass")) == ""  # never twice
    turn_from(claude, body)  # delivered at the next tool boundary as a queued prompt
    assert wait_for(lambda: state(broker, mid) == "in_context")


def test_registry_waiting_holds_delivery_until_the_prompt_clears(broker: InProcBroker,
                                                                 claude: FakeClaude) -> None:
    joined_idle(claude, broker)
    claude.hook(fixture("UserPromptSubmit"))
    claude.set_registry("waiting")  # a permission prompt is open
    assert wait_for(lambda: part(broker)["status"] == "waiting-approval")
    assert wait_for(lambda: member(broker)["status"] == "waiting-approval")
    mid = say(broker, "held while you decide")
    time.sleep(1.0)
    assert claude.inbox.frames() == [] and state(broker, mid) == "pending"
    assert q(broker, "SELECT COUNT(*) FROM batches")[0][0] == 0
    # declined (Esc): the registry goes idle and the turn is over; the message goes out now
    claude.set_registry("idle")
    [(_c, frame)] = claude.inbox.wait_frames(1)
    assert ids_in(frame["message"]["content"]) == [mid]
    assert part(broker)["status"] == "idle"
    ev = q(broker, "SELECT data FROM events WHERE kind='status' ORDER BY id")
    srcs = [json.loads(e["data"]).get("src") for e in ev]
    assert "claude:registry" in srcs


def test_three_idle_expiries_warn_the_human(broker: InProcBroker, claude: FakeClaude) -> None:
    joined_idle(claude, broker)
    mid = say(broker, "are you getting these?")
    frames = claude.inbox.wait_frames(3, timeout=12)
    assert len(frames) >= 3 and all(ids_in(f["message"]["content"]) == [mid] for _c, f in frames)

    def expired() -> list[tuple[str, str]]:
        rows = q(broker, "SELECT state, expire_reason FROM batches WHERE path='inbox' AND state='expired'")
        return [tuple(r) for r in rows]

    assert wait_for(lambda: len(expired()) >= 3, 8)
    assert set(expired()) == {("expired", "idle_no_token")}

    def warned() -> bool:
        msgs = broker.web.get("/api/rooms/build/messages").json()["messages"]
        return any(m["kind"] == "notice" and "not confirmed" in m["text"] for m in msgs)

    assert wait_for(warned)
    assert q(broker, "SELECT attempts FROM deliveries WHERE message_id=?", mid)[0][0] >= 3
    # the member shows parked while the next frame backs off
    assert wait_for(lambda: "not confirmed" in (member(broker)["parked_reason"] or ""))
    # the next frame is taken up (the registry says busy, so it can't expire first)
    fr = claude.inbox.wait_frames(4, timeout=10)
    claude.set_registry("busy")
    turn_from(claude, fr[3][1]["message"]["content"])
    # a confirmation clears the count
    assert wait_for(lambda: part(broker)["push_expiries"] == 0)


def test_an_unanswered_message_is_redelivered_once(broker: InProcBroker, claude: FakeClaude) -> None:
    joined_idle(claude, broker)
    mid = say(broker, "please answer this one")
    [(_c, f1)] = claude.inbox.wait_frames(1)
    turn_from(claude, f1["message"]["content"])
    assert wait_for(lambda: state(broker, mid) == "in_context")
    claude.hook(fixture("Stop"))  # the turn ended with no say() and no pass()
    fr = claude.inbox.wait_frames(2)
    assert len(fr) == 2
    body2 = fr[1][1]["message"]["content"]
    line = next(x for x in body2.splitlines() if x.startswith(f"- id={mid} "))
    assert "again=yes" in line
    turn_from(claude, body2)
    claude.hook(fixture("Stop"))
    time.sleep(1.5)
    assert len(claude.inbox.frames()) == 2  # once only
    assert q(broker, "SELECT redelivered FROM deliveries WHERE message_id=?", mid)[0][0] == 1


def test_answering_means_no_redelivery(broker: InProcBroker, claude: FakeClaude) -> None:
    joined_idle(claude, broker)
    mid = say(broker, "quick question")
    [(_c, f1)] = claude.inbox.wait_frames(1)
    turn_from(claude, f1["message"]["content"])
    r = claude.tool("say", room="#build", text="answer", reply_to=mid)
    assert r["ok"], r
    claude.hook(fixture("Stop"))
    time.sleep(1.0)
    assert len(claude.inbox.frames()) == 1 and state(broker, mid) == "handled"


def test_clear_keeps_the_binding_and_the_inbox(broker: InProcBroker, claude: FakeClaude) -> None:
    joined_idle(claude, broker)
    pid_before = part(broker)["id"]
    new_sid = "00000000-0000-4000-8000-00000000c1ff"
    claude.hook(fixture("SessionEnd_clear"))
    out = json.loads(claude.hook(fixture("SessionStart_clear", session_id=new_sid)))
    assert "#build as claude-1" in out["hookSpecificOutput"]["additionalContext"]
    p = part(broker)
    assert p["id"] == pid_before and p["session_id"] == new_sid and p["status"] == "idle" and p["ended_at"] is None
    mid = say(broker, "after the clear")
    [(_c, frame)] = claude.inbox.wait_frames(1)
    turn_from(claude, frame["message"]["content"])
    assert wait_for(lambda: state(broker, mid) == "in_context")


def test_a_stop_less_turn_end_is_read_from_the_registry(broker: InProcBroker, claude: FakeClaude) -> None:
    """Esc ends a turn without a Stop hook; the registry's idle ends it for switchboard."""
    joined_idle(claude, broker)
    claude.hook(fixture("UserPromptSubmit"))
    claude.set_registry("busy")
    time.sleep(0.4)
    assert part(broker)["status"] == "busy"
    claude.set_registry("idle")
    assert wait_for(lambda: part(broker)["status"] == "idle", 4)
    mid = say(broker, "now that you're idle")
    [(_c, frame)] = claude.inbox.wait_frames(1)
    assert ids_in(frame["message"]["content"]) == [mid]


def test_no_token_means_the_hook_tier_and_nothing_is_posted(broker: InProcBroker) -> None:
    c = FakeClaude(broker, inbox=True, token=False)
    try:
        j = c.tool("join", room="#build", screen_name="claude-1")
        assert j["ok"] and j["tier"] == "claude:hook", j
        c.hook(fixture("Stop"))
        say(broker, "hello?")
        assert wait_for(lambda: member(broker)["parked"])
        time.sleep(0.5)
        assert c.inbox.frames() == []
    finally:
        c.close()


def test_a_registry_that_names_another_socket_is_never_posted_to(broker: InProcBroker) -> None:
    c = FakeClaude(broker, inbox=True, reg_socket="/tmp/yk-not-this-one.sock")
    try:
        j = c.tool("join", room="#build", screen_name="claude-1")
        assert j["ok"] and j["tier"] != "claude:inbox", j
        c.hook(fixture("Stop"))
        say(broker, "hello?")
        time.sleep(1.0)
        assert c.inbox.frames() == []
    finally:
        c.close()


def test_pause_stops_idle_wakes(broker: InProcBroker, claude: FakeClaude) -> None:
    joined_idle(claude, broker)
    r = broker.web.post("/api/rooms/build/command", json={"text": "/pause"}, headers=broker.write_headers())
    assert r.status_code == 200
    mid = say(broker, "while paused")
    time.sleep(1.0)
    assert claude.inbox.frames() == [] and state(broker, mid) == "pending"
    broker.web.post("/api/rooms/build/command", json={"text": "/resume"}, headers=broker.write_headers())
    [(_c, frame)] = claude.inbox.wait_frames(1)
    assert ids_in(frame["message"]["content"]) == [mid]


def test_a_broker_restart_reattaches_and_a_starting_session_is_woken(broker: InProcBroker,
                                                                    claude: FakeClaude) -> None:
    """After a restart the MCP server reconnects on its own, says hello (the
    member is 'starting') and attaches again; an idle wake goes out, and a frame
    that isn't taken up expires even though no hook ever says idle."""
    joined_idle(claude, broker)
    broker.web.close()
    broker.restart()
    broker.web = broker.web_client()
    assert wait_for(lambda: part(broker)["status"] == "starting" and part(broker)["tier"] == "claude:inbox", 15)
    assert wait_for(lambda: member(broker)["tier"] == "claude:inbox")
    mid = say(broker, "after the restart")
    [(_c, f1)] = claude.inbox.wait_frames(1)
    assert ids_in(f1["message"]["content"]) == [mid]
    assert wait_for(lambda: q(broker, "SELECT 1 FROM batches WHERE path='inbox' AND state='expired'"
                                      " AND expire_reason='idle_no_token'"), 5)
    fr = claude.inbox.wait_frames(2, timeout=8)
    claude.set_registry("busy")
    turn_from(claude, fr[1][1]["message"]["content"])
    assert wait_for(lambda: state(broker, mid) == "in_context")
    assert part(broker)["status"] == "busy"
