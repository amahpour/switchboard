"""#304 (DESIGN.md §6.1): an agent restarted into a new session takes its name back with the join
code its earlier join answered with, in each room it was in; without that code, or with any other,
the name stays reserved; and a code never moves a name to another machine, harness or a kicked
agent. The refusal tells a session to use ``resume`` only if its MCP server has it (#340).
Stand-in Claude sessions (tests/fakes/fake_claude.py) against an in-process broker."""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_claude import FakeClaude

from switchboard import __version__
from switchboard.broker.agents import cred_hash
from switchboard.config import Config
from switchboard.mcp.client import RpcError, Stream
from switchboard.models import session_key

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
CODE = re.compile(r"\byk:j[0-9a-f]{16}\b")
RESTARTED = "00000000-0000-4000-8000-00000000c2b0"


def wait_for(fn: Any, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    (tmp_home / "config.toml").write_text(f'[claude]\nsessions_dir = "{b.cfg.claude.sessions_dir}"\n')
    web = b.web_client()
    for room in ("#build", "#docs"):
        assert web.post("/api/rooms", json={"name": room}, headers=b.write_headers()).status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


def q(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def join_code(res: dict[str, Any]) -> str:
    got = CODE.search(res["text"])
    assert got is not None, res["text"]
    return got.group(0)


def first_session_ends(b: InProcBroker) -> dict[str, str]:
    """A Claude session joins #build and #docs as planner, then dies: its codes, by room."""
    old = FakeClaude(b)
    codes = {}
    for room in ("#build", "#docs"):
        r = old.tool("join", room=room, screen_name="planner")
        assert r["ok"], r
        codes[room] = join_code(r)
    old.p.kill()
    old.p.wait(5)
    assert wait_for(lambda: len(q(b, "SELECT 1 FROM memberships WHERE left_reason='session_end'")) == 2)
    return codes


def test_a_restarted_session_takes_its_name_back_in_each_room(broker: InProcBroker) -> None:
    """The bug: a restarted agent came back as planner-2 and peers briefed it from the start. With
    each room's code from its earlier join, the new session is planner again in both rooms, and the
    room is told it's back, not new."""
    codes = first_session_ends(broker)
    new = FakeClaude(broker, env={"CLAUDE_CODE_SESSION_ID": RESTARTED})
    try:
        for room in ("#build", "#docs"):
            r = new.tool("join", room=room, screen_name="planner", resume=codes[room])
            assert r["ok"] and r["screen_name"] == "planner", r
            assert CODE.search(r["text"]).group(0) != codes[room]  # a new code for the next restart
        msgs = broker.web.get("/api/rooms/build/messages").json()["messages"]
        joins = [m["text"] for m in msgs if m["kind"] == "join"]
        assert len(joins) == 2 and not joins[0].endswith("restart"), joins
        assert joins[-1].startswith("joined (claude, ") and joins[-1].endswith(", back after a restart"), (
            joins
        )
        members = broker.web.get("/api/rooms/build/members").json()["members"]
        assert [m["name"] for m in members] == ["planner"]
        rows = q(broker, "SELECT DISTINCT participant_id FROM memberships WHERE screen_name='planner'")
        assert len(rows) == 2  # really another session, not the old one re-bound
    finally:
        new.close()


def test_without_the_code_or_with_another_the_name_stays_reserved(broker: InProcBroker) -> None:
    """The reservation still does its job: no code, a made-up code, the other room's code or a
    malformed one never hand the name over. The refusal says how to reclaim it."""
    codes = first_session_ends(broker)
    new = FakeClaude(broker, env={"CLAUDE_CODE_SESSION_ID": RESTARTED})
    try:
        r = new.tool("join", room="#build", screen_name="planner")
        assert r["code"] == "name_reserved" and "resume=" in r["error"], r
        for wrong in ("yk:j" + "0" * 16, codes["#docs"]):
            r = new.tool("join", room="#build", screen_name="planner", resume=wrong)
            assert r["code"] == "name_reserved" and "isn't this name's" in r["error"], (wrong, r)
        for bad in ("yk:jXYZ", "0" * 64, "yk:j" + "A" * 16):
            r = new.tool("join", room="#build", screen_name="planner", resume=bad)
            assert r["code"] == "bad_request", (bad, r)
        assert q(broker, "SELECT COUNT(*) FROM memberships WHERE left_at IS NULL")[0][0] == 0
    finally:
        new.close()


def test_a_code_never_moves_a_name_to_another_machine_harness_or_a_kicked_agent(
    broker: InProcBroker, tmp_home: Path
) -> None:
    """The join code is bound to who it was given to: on another machine (another person's), from
    another harness, or once the agent was kicked, the right code still doesn't reclaim the name."""
    codes = first_session_ends(broker)
    [(old_id,)] = q(broker, "SELECT DISTINCT participant_id FROM memberships WHERE screen_name='planner'")
    st = broker.state.store
    # a Claude agent on another machine ("pi") that held "bench" here and left: its code is known
    far = st.upsert_participant("claude", session_key("claude", "pi", "s-far"), host="pi")
    room = st.get_room("#build")
    assert room is not None
    gone = st.create_membership(room.id, far.id, "bench", cred_hash("x"), cred_hash("ab" * 8))
    st.con.execute("UPDATE memberships SET left_at=? WHERE id=?", (st.clock.now(), gone.id))
    st.con.commit()
    new = FakeClaude(broker, env={"CLAUDE_CODE_SESSION_ID": RESTARTED})
    try:
        r = new.tool("join", room="#build", screen_name="bench", resume="yk:j" + "ab" * 8)
        assert r["code"] == "name_reserved" and "on another machine" in r["error"], r

        async def other_harness() -> dict[str, Any]:
            async with FakeAgent(broker.home, "k9") as a:  # the test harness, not claude
                return await a.call("join", room="#build", screen_name="planner", resume=codes["#build"])

        import asyncio

        r = asyncio.run(other_harness())
        assert r["code"] == "name_reserved" and "another harness" in r["error"], r
        st.con.execute(
            "UPDATE memberships SET kicked=1 WHERE participant_id=? AND room_id="
            "(SELECT id FROM rooms WHERE name='#docs')",
            (old_id,),
        )
        st.con.commit()
        r = new.tool("join", room="#docs", screen_name="planner", resume=codes["#docs"])
        assert r["code"] == "name_reserved" and "was kicked" in r["error"], r
        # a wrong code learns nothing about the holder: not that it was kicked (#370)
        r = new.tool("join", room="#docs", screen_name="planner", resume="yk:j" + "0" * 16)
        assert r["code"] == "name_reserved" and "current one" in r["error"], r
        assert "kicked" not in r["error"], r
        r = new.tool("join", room="#build", screen_name="planner", resume=codes["#build"])
        assert r["ok"], r  # the right code, same machine and harness, not kicked here
    finally:
        new.close()


def test_a_join_from_before_join_codes_says_so(broker: InProcBroker) -> None:
    """#370: a membership with no join code on record (made before join codes could bring a name
    back) refused every code with the same words as a wrong one, so the agent couldn't tell its
    code was fine and the name simply can't come back. It now says so. Fails if the refusal still
    reads as a wrong code."""
    st = broker.state.store
    old = st.upsert_participant("claude", session_key("claude", "", "s-old"))
    room = st.get_room("#build")
    assert room is not None
    gone = st.create_membership(room.id, old.id, "planner", cred_hash("x"), None)
    st.con.execute("UPDATE memberships SET left_at=? WHERE id=?", (st.clock.now(), gone.id))
    st.con.commit()
    new = FakeClaude(broker, env={"CLAUDE_CODE_SESSION_ID": RESTARTED})
    try:
        r = new.tool("join", room="#build", screen_name="planner", resume="yk:j" + "ab" * 8)
        assert r["code"] == "name_reserved" and "no code on record" in r["error"], r
    finally:
        new.close()


# ----------------------------------------------- the refusal's hint follows the caller (#340)
OLDER = "this session's switchboard is older than the broker's"


def planner_left(b: InProcBroker) -> None:
    """Another session held ``planner`` in #build and left, so the name is reserved for a day."""
    with Stream(b.paths.sock, timeout=5) as holder:
        holder.call("mcp.hello", {"harness": "test", "test_session": "holder"}, 5)
        joined = holder.call("agent.join", {"room": "#build", "screen_name": "planner"}, 5)
        holder.call("agent.leave", {"cred": joined["cred"]}, 5)


def refusal_for(b: InProcBroker, claim: dict[str, Any], **join: Any) -> str:
    """The ``name_reserved`` text a session gets when its MCP server's ``mcp.hello`` claims
    ``claim`` (a version, or nothing) and it joins as ``planner`` after the holder left."""
    planner_left(b)
    with Stream(b.paths.sock, timeout=5) as s:
        s.call("mcp.hello", {"harness": "test", "test_session": "caller", **claim}, 5)
        with pytest.raises(RpcError) as refused:
            s.call("agent.join", {"room": "#build", "screen_name": "planner", **join}, 5)
    assert refused.value.code == "name_reserved", refused.value
    return refused.value.message


@pytest.mark.parametrize("version", [__version__, "0.25.0", "0.25.7", "1.0.0"])
def test_a_session_that_has_resume_is_told_to_use_it(broker: InProcBroker, version: str) -> None:
    """A session whose MCP server is from 0.25.0 or later has the ``resume`` parameter, so the
    refusal still tells it to use that, as it has since #304. A fix that always dropped the hint
    would leave current sessions with no way to learn how to take their name back."""
    text = refusal_for(broker, {"version": version})
    assert "join again with resume= set to the code" in text and "pick another" in text, text
    assert OLDER not in text, text


@pytest.mark.parametrize("version", ["0.24.9", "0.24.0", "0.23.0", "0.1.0"])
def test_a_session_from_before_resume_is_not_told_to_use_it(broker: InProcBroker, version: str) -> None:
    """The bug (#340): a session whose MCP server is from before 0.25.0 was told to join again
    with ``resume=``, a parameter its ``join`` tool doesn't have, so the call was refused and the
    agent joined under another name. It is told its switchboard is older than the broker's, that
    a restart updates it, and that it can still pick another name; ``resume`` isn't named."""
    text = refusal_for(broker, {"version": version})
    assert "resume" not in text, text
    assert OLDER in text and "Ask your user to restart this session" in text and "pick another" in text, text


@pytest.mark.parametrize(
    "claim",
    [
        pytest.param({}, id="absent"),
        pytest.param({"version": None}, id="null"),
        pytest.param({"version": 25}, id="not a string"),
        pytest.param({"version": "not a version!"}, id="not a version"),
        pytest.param({"version": "0.25"}, id="no patch number"),
        pytest.param({"version": "dev"}, id="a word"),
        pytest.param({"version": "9" * 40}, id="too long"),
    ],
)
def test_a_session_that_does_not_say_its_version_is_not_told_to_use_resume(
    broker: InProcBroker, claim: dict[str, Any]
) -> None:
    """A claim is not trusted to be there or to parse. A version the broker can't read counts as
    too old: the conservative answer names no parameter the session may not have, and the cost of
    being wrong is one restart, not an agent stuck on a call its tool refuses."""
    text = refusal_for(broker, claim)
    assert "resume" not in text, text
    assert OLDER in text and "pick another" in text, text


def test_a_wrong_code_is_answered_the_same_whatever_the_version(broker: InProcBroker) -> None:
    """A caller that passes ``resume`` has the parameter, whatever version it claims, so its
    refusal says the code isn't this name's, as before, and doesn't tell it to update."""
    text = refusal_for(broker, {"version": "0.24.0"}, resume="yk:j" + "0" * 16)
    assert "isn't this name's" in text and OLDER not in text, text
