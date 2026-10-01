"""A scripted FakeAgent (real ``switchboard mcp --harness test`` over stdio) against a real
in-process broker: join, read, say, pass, wait, unwait (DESIGN.md §6, §8, §12.2)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import InProcBroker, child_env
from fakes.fake_agent import FakeAgent, ids_in
from switchboard.config import Config
from switchboard.envelope import NONCE_RE, TOKEN_RE
from switchboard.mcp.client import RpcError, Stream

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)

SEND_HOOK = r"""
import json, socket, sys
path, params, do_ack = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3] == "1"
s = socket.socket(socket.AF_UNIX); s.connect(path)
s.sendall((json.dumps({"id": 1, "method": "hook.event", "params": params}) + "\n").encode())
buf = b""
while b"\n" not in buf:
    c = s.recv(65536)
    if not c:
        break
    buf += c
res = json.loads(buf.split(b"\n")[0])["result"]
if do_ack and res.get("batch_id"):
    s.sendall((json.dumps({"id": 2, "method": "hook.ack",
                           "params": {"batch_id": res["batch_id"], "ack": res["ack"]}}) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        c = s.recv(65536)
        if not c:
            break
        buf += c
print(json.dumps(res))
"""


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    r = web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers())
    assert r.status_code == 200
    b.web = web
    b.creds = []
    orig = b.state.agents.join

    def spy(conn, params):  # record every credential the broker issues
        res = orig(conn, params)
        b.creds.append(res["cred"])
        return res

    b.state.agents.join = spy
    yield b
    web.close()
    b.stop()


def say(b: InProcBroker, text: str) -> int:
    r = b.web.post("/api/rooms/build/say", json={"text": text}, headers=b.write_headers())
    assert r.status_code == 200, r.text
    return r.json()["id"]


def command(b: InProcBroker, text: str) -> dict[str, Any]:
    r = b.web.post("/api/rooms/build/command", json={"text": text}, headers=b.write_headers())
    assert r.status_code == 200, r.text
    return r.json()


def q(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def states(b: InProcBroker, name: str) -> dict[int, str]:
    rows = q(b, "SELECT d.message_id, d.state FROM deliveries d JOIN memberships m ON m.id=d.membership_id"
                " WHERE m.screen_name=? AND m.left_at IS NULL", name)
    return {r[0]: r[1] for r in rows}


def hook(b: InProcBroker, params: dict[str, Any], ack: bool = False) -> dict[str, Any]:
    r = subprocess.run([sys.executable, "-c", SEND_HOOK, str(b.paths.sock), json.dumps(params), "1" if ack else "0"],
                       capture_output=True, text=True, env=child_env(), timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def all_outputs(*agents: FakeAgent) -> str:
    return json.dumps([c[2] for a in agents for c in a.calls])


# ---------------------------------------------------------------- joining
async def test_join_gives_rules_catchup_nonce_banner_and_never_the_cred(broker: InProcBroker) -> None:
    ids = [say(broker, f"history {i}") for i in range(35)]
    async with FakeAgent(broker.home, "k1") as a:
        j = await a.join("#build", "tester")
        assert j["ok"] and j["room"] == "#build" and j["tier"] == "mcp-only"
        text = j["text"]
        assert "Room rules:" in text and "untrusted" in text and "worktree" in text
        assert ids_in(text) == ids[-30:]
        assert NONCE_RE.search(text) and "[TEST MODE]" in text
        await a.read("#build")
        await a.say("#build", "hello")
        await a.who("#build")
        cred = broker.creds[-1]
        assert len(cred) >= 40 and cred not in all_outputs(a) and "cred" not in json.dumps(j)
    rows = q(broker, "SELECT cred_hash FROM memberships WHERE screen_name='tester'")
    assert rows[0][0] in (hashlib.sha256(cred.encode()).hexdigest(), None)


async def test_rejoin_rotates_the_credential(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        j2 = await a.join("#build", "tester")
        assert j2["ok"] and "You rejoined #build" in j2["text"]
        old, new = broker.creds[-2:]
        [row] = q(broker, "SELECT cred_hash FROM memberships WHERE screen_name='tester' AND left_at IS NULL")
        assert row[0] == hashlib.sha256(new.encode()).hexdigest() != hashlib.sha256(old.encode()).hexdigest()
        assert (await a.read("#build"))["ok"]  # the MCP server switched to the new one


async def test_a_copied_credential_is_useless_from_another_process(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        cred = broker.creds[-1]
        with Stream(broker.paths.sock) as s:  # this pytest process, not the MCP server
            s.call("mcp.hello", {"harness": "test", "test_session": "thief"})
            with pytest.raises(RpcError) as e:
                s.call("agent.say", {"cred": cred, "text": "I am tester"})
            assert e.value.code == "unauthorized" and "another process" in e.value.message
        with Stream(broker.paths.sock) as s:
            with pytest.raises(RpcError) as e:
                s.call("agent.read", {"cred": cred})
            assert e.value.code == "unauthorized"


async def test_names_taken_and_reserved(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a, FakeAgent(broker.home, "k2") as b:
        assert (await a.join("#build", "alpha"))["ok"]
        r = await b.join("#build", "alpha")
        assert r["ok"] is False and r["code"] == "name_taken"
        for bad in ("alice", "alice2", "system", "switchboard-bot", "human"):
            r = await b.join("#build", bad)
            assert r["ok"] is False and r["code"] == "name_reserved", bad
        r = await b.join("#build", "Bad Name!")
        assert r["code"] == "bad_request"
        r = await b.join("#nosuch", "beta")
        assert r["code"] == "not_found" and "ask your user" in r["error"]
        await a.leave("#build")
        r = await b.join("#build", "alpha")  # another agent used it today
        assert r["code"] == "name_reserved"


# ------------------------------------------------------------ read / say
async def test_read_never_skips_with_ack_never(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1", ack="never") as a:
        await a.join("#build", "tester")
        ids = [say(broker, f"m{i}") for i in range(3)]
        r1 = await a.read("#build")
        assert ids_in(r1["text"]) == ids
        r2 = await a.read("#build")  # never confirmed: they come back
        assert ids_in(r2["text"]) == ids
        assert set(states(broker, "tester").values()) == {"offered"}


async def test_read_with_next_call_ack_moves_on(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        ids = [say(broker, f"m{i}") for i in range(3)]
        assert ids_in((await a.read("#build"))["text"]) == ids
        assert (await a.read("#build"))["count"] == 0
        assert set(states(broker, "tester").values()) == {"in_context"}
        r = await a.read("#build", limit=1)
        assert r["count"] == 0


async def test_say_returns_earlier_unread_and_is_rate_limited(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        h1, h2 = say(broker, "first"), say(broker, "second")
        r = await a.say("#build", "my answer")
        assert r["ok"] and r["posted_id"] > h2
        assert ids_in(r["unread_text"]) == [h1, h2]
        # those two human messages are now in context and unanswered: that exempts the next say
        r_ex = await a.say("#build", "answering the unread ones")
        assert r_ex["posted_id"]
        await a.pass_("#build")
        r2 = await a.say("#build", "one more thought")
        assert r2["ok"] and r2["posted_id"] is None and r2["reason"] == "rate_limited"
        assert 0 < r2["retry_after_s"] <= 10
        r3 = await a.say("#build", "replying to you", reply_to=h1)  # replies to the human are exempt
        assert r3["posted_id"]
        [row] = q(broker, "SELECT COUNT(*) FROM events WHERE kind='rate_limited'")
        assert row[0] == 1
        msgs = broker.web.get("/api/rooms/build/messages").json()["messages"]
        mine = [m for m in msgs if m["from"] == "tester" and m["kind"] == "chat"]
        assert [m["text"] for m in mine] == ["my answer", "answering the unread ones", "replying to you"]
        assert mine[0]["sender_kind"] == "agent" and mine[0]["via"] == "mcp" and mine[0]["harness"] == "test"


async def test_say_rejects_reply_to_an_overflowing_id_or_join_line(broker: InProcBroker) -> None:
    """Reply validation rejects SQLite overflow and non-chat ids on the agent path too."""
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        messages = broker.web.get("/api/rooms/build/messages").json()["messages"]
        join_id = next(m["id"] for m in messages if m["kind"] == "join" and m["from"] == "tester")
        for bad in (2**63, join_id):
            result = await a.say("#build", "bad reply", reply_to=bad)
            assert result["ok"] is False and result["code"] == "bad_request", (bad, result)


async def test_pass_is_logged_not_posted(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1", ack="immediate") as a:
        await a.join("#build", "tester")
        say(broker, "anything?")
        await a.read("#build")
        before = len(broker.web.get("/api/rooms/build/messages").json()["messages"])
        r = await a.pass_("#build", note="nothing to add")
        assert r["ok"] and r["text"] == "[switchboard] logged, not posted."
        assert len(broker.web.get("/api/rooms/build/messages").json()["messages"]) == before
        assert set(states(broker, "tester").values()) == {"handled"}
        [row] = q(broker, "SELECT data FROM events WHERE kind='pass'")
        assert json.loads(row[0])["note_len"] == len("nothing to add")
        assert (await a.pass_())["ok"]  # no room: every room this session joined


async def test_an_agent_slash_is_stored_literally(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        await a.say("#build", "/pause")
        assert broker.web.get("/api/rooms/build/members").json()["settings"]["paused"] is False
        last = broker.web.get("/api/rooms/build/messages").json()["messages"][-1]
        assert last["text"] == "/pause" and last["sender_kind"] == "agent"


# ------------------------------------------------------------------ wait
async def test_wait_returns_on_a_human_message(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        t = a.wait_task("#build", 20)
        await asyncio.sleep(0.4)
        t0 = time.monotonic()
        mid = say(broker, "wake up")
        r = await asyncio.wait_for(t, 5)
        dt = time.monotonic() - t0
        assert r["status"] == "messages" and ids_in(r["text"]) == [mid] and TOKEN_RE.search(r["text"])
        assert dt < 1.0, dt
        print(f"wait wake latency {dt*1000:.1f} ms")


async def test_wait_returns_paused_on_pause(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        t = a.wait_task("#build", 20)
        await asyncio.sleep(0.4)
        command(broker, "/pause")
        r = await asyncio.wait_for(t, 5)
        assert r["status"] == "paused" and "End your turn" in r["text"]


async def test_wait_during_a_pause_returns_paused_at_its_timeout(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        command(broker, "/pause")
        t0 = time.monotonic()
        say(broker, "held back by the pause")
        r = await a.wait("#build", 2)
        assert r["status"] == "paused" and 1.5 < time.monotonic() - t0 < 6
        # read() still works while paused: it is an explicit pull, not a wake
        assert (await a.read("#build"))["count"] == 1


async def test_wait_times_out_and_is_capped(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        r = await a.wait("#build", 1)
        assert r["status"] == "timeout" and r["timeout_s"] == 1
        t = a.wait_task("#build", 500)
        await asyncio.sleep(0.4)
        say(broker, "x")
        r = await asyncio.wait_for(t, 5)
        assert r["timeout_s"] == 50  # the test harness cap


async def test_a_newer_wait_supersedes(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        t1 = a.wait_task("#build", 20)
        await asyncio.sleep(0.4)
        t2 = a.wait_task("#build", 20)
        r1 = await asyncio.wait_for(t1, 5)
        assert r1["status"] == "superseded"
        mid = say(broker, "for the newer wait")
        r2 = await asyncio.wait_for(t2, 5)
        assert r2["status"] == "messages" and ids_in(r2["text"]) == [mid]


async def test_cancelled_wait_sends_unwait_and_loses_nothing(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await asyncio.wait_for(a.raw("wait", {"room": "#build", "timeout_s": 20}), 0.8)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not broker.on_loop(lambda: broker.state.engine.sinks.open_sinks()):
                break
            await asyncio.sleep(0.05)
        assert broker.on_loop(lambda: broker.state.engine.sinks.open_sinks()) == []
        mid = say(broker, "after the cancel")
        await asyncio.sleep(0.3)
        assert states(broker, "tester")[mid] == "pending"  # nobody was listening: still pending
        assert ids_in((await a.read("#build"))["text"]) == [mid]


# ----------------------------------------------------------------- hooks
async def test_synthetic_posttooluse_confirms_and_foreign_tokens_do_not(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "ka", ack="never") as a, FakeAgent(broker.home, "kb", ack="never") as b:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        mid = say(broker, "hello both")
        ra = await a.read("#build")
        rb = await b.read("#build")
        ta, tb = TOKEN_RE.search(ra["text"]), TOKEN_RE.search(rb["text"])
        # beta's hook presents alpha's token: nothing is confirmed
        hook(broker, {"harness": "test", "event": "PostToolUse", "sid": "kb", "ok": True,
                      "tokens": [[int(ta.group(1)), ta.group(2)]]})
        assert states(broker, "alpha")[mid] == "offered"
        hook(broker, {"harness": "test", "event": "PostToolUse", "sid": "ka", "ok": True,
                      "tokens": [[int(ta.group(1)), ta.group(2)]]})
        assert states(broker, "alpha")[mid] == "in_context"
        assert states(broker, "beta")[mid] == "offered"
        hook(broker, {"harness": "test", "event": "PostToolUse", "sid": "kb", "ok": True,
                      "tokens": [[int(tb.group(1)), tb.group(2)]]})
        assert states(broker, "beta")[mid] == "in_context"


async def test_synthetic_hook_gets_priority_context_and_acks_it(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "ka", ack="never") as a, FakeAgent(broker.home, "kb") as b:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        chatter = (await b.say("#build", "just chatting"))["posted_id"]
        ment = (await b.say("#build", "@alpha your turn", reply_to=None))
        assert ment["reason"] == "rate_limited"  # beta just spoke
        hid = say(broker, "@alpha human here")
        res = hook(broker, {"harness": "test", "event": "PostToolUse", "sid": "ka", "ok": True}, ack=True)
        assert res["out"]["kind"] == "context" and ids_in(res["out"]["text"]) == [hid]
        assert chatter not in ids_in(res["out"]["text"])  # mid-task: priority only
        assert states(broker, "alpha")[hid] == "in_context" and states(broker, "alpha")[chatter] == "pending"
        # no joined session above an unrelated process: inert
        assert hook(broker, {"harness": "test", "event": "PostToolUse", "sid": "nope", "ok": True})["out"] is None


async def test_hooks_from_an_unjoined_process_are_inert(broker: InProcBroker) -> None:
    res = hook(broker, {"harness": "test", "event": "PostToolUse", "sid": "x", "ok": True})
    assert res == {"out": None}
    res = hook(broker, {"harness": "claude", "event": "PermissionRequest", "sid": "x"})
    assert res == {"out": None}


# ------------------------------------------------ read before pass (DESIGN.md §24)
def stub_by_hook(b: InProcBroker, sid: str, mid: int) -> str:
    """A synthetic PostToolUse puts the peer @mention in hook context as a stub (the
    elevated-path rendering Codex's turn/start and steer get too), and acks it."""
    res = hook(b, {"harness": "test", "event": "PostToolUse", "sid": sid, "ok": True}, ack=True)
    text = res["out"]["text"]
    assert ids_in(text) == [mid] and 'text=(not shown here; call read("#build"))' in text
    return text


async def test_a_stubbed_peer_message_must_be_read_before_pass(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "ka") as a, FakeAgent(broker.home, "kb") as b:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        mid = (await b.say("#build", "@alpha the parser drops port 0"))["posted_id"]
        text = stub_by_hook(broker, "ka", mid)
        assert "drops port 0" not in text and ", or pass(" not in text
        assert 'Call read("#build") now to see it; after reading, reply with say() or pass()' in text
        assert states(broker, "alpha")[mid] == "pending"  # a notified stub: read() only
        r = await a.pass_("#build")
        assert r["ok"] is False and r["code"] == "read_first"
        assert r["error"].startswith('[switchboard] pass("#build") refused:') and 'Call read("#build") now' in r["error"]
        assert r["rooms"] == [{"room": "#build", "ok": False, "code": "read_first", "unread": 1, "error": r["error"]}]
        assert q(broker, "SELECT COUNT(*) FROM events WHERE kind='pass'")[0][0] == 0
        [row] = q(broker, "SELECT data FROM events WHERE kind='pass_refused'")
        assert json.loads(row[0]) == {"reason": "read_first", "n": 1, "ids": [mid]}
        rd = await a.read("#build")
        assert ids_in(rd["text"]) == [mid] and "drops port 0" in rd["text"]
        # the next call confirms the read (--ack next_call); the pass then goes through
        r = await a.pass_("#build")
        assert r["ok"] and r["text"] == "[switchboard] logged, not posted."
        assert states(broker, "alpha")[mid] == "handled"
        assert q(broker, "SELECT COUNT(*) FROM events WHERE kind='pass'")[0][0] == 1


async def test_pass_in_every_room_passes_where_it_can_and_names_the_rest(broker: InProcBroker) -> None:
    assert broker.web.post("/api/rooms", json={"name": "#side"}, headers=broker.write_headers()).status_code == 200
    async with FakeAgent(broker.home, "ka") as a, FakeAgent(broker.home, "kb") as b:
        await a.join("#build", "alpha")
        await a.join("#side", "alpha")
        await b.join("#build", "beta")
        mid = (await b.say("#build", "@alpha look at this"))["posted_id"]
        stub_by_hook(broker, "ka", mid)
        r = await a.pass_()  # no room: every room this session joined
        assert r["ok"] is False and r["code"] == "read_first"
        assert [x["room"] for x in r["rooms"]] == ["#build", "#side"]
        assert [x["ok"] for x in r["rooms"]] == [False, True]
        assert r["text"].startswith("[switchboard] passed in #side (logged, not posted). [switchboard] pass(\"#build\") refused")
        await a.read("#build")
        r = await a.pass_()
        assert r["ok"] and [x["ok"] for x in r["rooms"]] == [True, True]


async def test_pass_works_at_once_after_an_inline_wait_delivery(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "ka") as a, FakeAgent(broker.home, "kb") as b:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        t = a.wait_task("#build", 20)
        await asyncio.sleep(0.4)
        mid = (await b.say("#build", "@alpha inline for you"))["posted_id"]
        r = await asyncio.wait_for(t, 5)
        assert r["status"] == "messages" and ids_in(r["text"]) == [mid] and "inline for you" in r["text"]
        assert "not shown here" not in r["text"]
        p = await a.pass_("#build")
        assert p["ok"] and states(broker, "alpha")[mid] == "handled"


async def test_a_wait_returns_an_unread_stub_at_once(broker: InProcBroker) -> None:
    """A wait loop that skipped the read(): wait() is a pull, so it shows the stub's text
    at once instead of sitting on it until the timeout; then pass() goes through."""
    async with FakeAgent(broker.home, "ka") as a, FakeAgent(broker.home, "kb") as b:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        mid = (await b.say("#build", "@alpha one more"))["posted_id"]
        stub_by_hook(broker, "ka", mid)
        r = await asyncio.wait_for(a.wait("#build", 30), 5)
        assert r["status"] == "messages" and ids_in(r["text"]) == [mid] and "one more" in r["text"]
        assert "not shown here" not in r["text"]
        p = await a.pass_("#build")  # the call confirms the wait() answer (--ack next_call)
        assert p["ok"] and states(broker, "alpha")[mid] == "handled"


# ------------------------------------------------------- membership edges
async def test_kick_ends_waits_and_blocks_rejoin(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        t = a.wait_task("#build", 20)
        await asyncio.sleep(0.4)
        assert command(broker, "/kick tester")["ok"]
        r = await asyncio.wait_for(t, 5)
        assert r["status"] == "kicked"
        r = await a.join("#build", "tester")
        assert r["ok"] is False and r["code"] == "kicked"
        r = await a.read("#build")
        assert r["ok"] is False


async def test_leave_and_who_and_away(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a, FakeAgent(broker.home, "k2") as b:
        await a.join("#build", "alpha")
        await b.join("#build", "beta")
        assert (await a.away("running tests <b>"))["away"] == "running tests <b>"
        w = await b.who("#build")
        assert "alpha" in w["text"] and 'away="running tests \\u003cb\\u003e"' in w["text"]
        members = broker.web.get("/api/rooms/build/members").json()["members"]
        assert {m["name"]: m["away"] for m in members}["alpha"] == "running tests <b>"
        assert {m["name"]: m["tier"] for m in members} == {"alpha": "mcp-only", "beta": "mcp-only"}
        assert (await a.away())["away"] is None
        await a.leave("#build")
        r = await a.read("#build")
        assert r["ok"] is False and r["code"] == "not_member"
        assert [m["name"] for m in broker.web.get("/api/rooms/build/members").json()["members"]] == ["beta"]


async def test_closing_the_mcp_server_marks_the_session_offline(broker: InProcBroker) -> None:
    a = await FakeAgent(broker.home, "k1").start()
    await a.join("#build", "tester")
    await a.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        [row] = q(broker, "SELECT status FROM participants WHERE session_key='test:k1'")
        if row[0] == "offline":
            break
        await asyncio.sleep(0.05)
    assert row[0] == "offline"
    # the membership survives (a reconnect or re-join picks it up)
    assert q(broker, "SELECT COUNT(*) FROM memberships WHERE screen_name='tester' AND left_at IS NULL")[0][0] == 1


async def test_claude_like_env_never_touches_the_inbox_socket(broker: InProcBroker) -> None:
    d = tempfile.mkdtemp(prefix="yk-cc-", dir="/tmp")
    path = os.path.join(d, "inbox.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)
    srv.setblocking(False)
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_MESSAGING_SOCKET": path,
           "CLAUDE_CODE_MESSAGING_TOKEN": "tok-canary-123", "CLAUDE_CODE_SESSION_ID": "sess-1"}  # gitleaks:allow (fake canary)
    try:
        async with FakeAgent(broker.home, "k1", env=env) as a:
            await a.join("#build", "tester")
            t = a.wait_task("#build", 10)
            await asyncio.sleep(0.3)
            say(broker, "hello")
            assert (await asyncio.wait_for(t, 5))["status"] == "messages"
            await a.say("#build", "hi")
            await a.read("#build")
            await asyncio.sleep(0.5)
        with pytest.raises(BlockingIOError):
            srv.accept()  # nobody ever connected
    finally:
        srv.close()
        os.unlink(path)
        os.rmdir(d)
    [row] = q(broker, "SELECT harness, claude_socket FROM participants WHERE session_key='test:k1'")
    assert row[0] == "test" and row[1] is None
    assert "tok-canary-123" not in (broker.home / "logs").joinpath("broker.log").read_text(errors="replace") \
        if (broker.home / "logs" / "broker.log").exists() else True


async def test_broker_down_is_reported_plainly(tmp_home: Path) -> None:
    async with FakeAgent(tmp_home, "k1") as a:
        r = await a.join("#build", "tester")
        assert r == {"ok": False, "error": "switchboard broker not running — ask your user to run: switchboard start"}


async def test_non_test_broker_refuses_the_test_harness(tmp_home: Path) -> None:
    b = InProcBroker(tmp_home, FAST, test_mode=False).start()
    try:
        async with FakeAgent(tmp_home, "k1") as a:
            r = await a.join("#build", "tester")
            assert r["ok"] is False and r["code"] == "forbidden" and "test-mode" in r["error"]
    finally:
        b.stop()


async def test_mcp_server_reconnects_after_a_broker_restart(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as a:
        await a.join("#build", "tester")
        await asyncio.get_running_loop().run_in_executor(None, broker.restart)
        broker.web = broker.web_client()
        mid = say(broker, "after the restart")
        deadline = time.monotonic() + 15
        r: dict[str, Any] = {}
        while time.monotonic() < deadline:
            r = await a.read("#build")
            if r.get("ok"):
                break
            await asyncio.sleep(0.3)
        # the credential persisted and works from the same MCP process
        assert r.get("ok") and ids_in(r["text"]) == [mid], r
