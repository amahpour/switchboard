"""#304 (DESIGN.md §6.1): an agent restarted into a new session takes its name back with the join
code its earlier join answered with, in each room it was in; without that code, or with any other,
the name stays reserved; and a code never moves a name to another machine, harness or a kicked
agent. Codex sessions over HTTPS (§49) against an in-process broker."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from test_agents_http import THREAD_A, THREAD_B, Codex, make_token

from switchboard.config import Config
from switchboard.models import join_code_hash, session_key

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
CODE = re.compile(r"\byk:j[0-9a-f]{16}\b")
RESTARTED = THREAD_B


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


def codex_session(b: InProcBroker) -> Codex:
    """A Codex process on the device ``laptop`` (one token for it per broker): its calls name their
    thread in ``_meta``."""
    if not hasattr(b, "laptop_token"):
        b.laptop_token = make_token(b, "laptop")  # type: ignore[attr-defined]
    c = Codex(b, b.laptop_token)  # type: ignore[attr-defined]
    assert c.start().status_code == 200
    return c


def first_session_ends(b: InProcBroker) -> dict[str, str]:
    """A Codex thread joins #build and #docs as planner, then leaves both: its codes, by room."""
    old = codex_session(b)
    codes = {}
    for room in ("#build", "#docs"):
        r = old.call("join", THREAD_A, room=room, screen_name="planner")
        assert r["ok"], r
        codes[room] = join_code(r)
    for room in ("#build", "#docs"):
        assert old.call("leave", THREAD_A, room=room)["ok"]
    assert wait_for(lambda: len(q(b, "SELECT 1 FROM memberships WHERE left_reason='leave'")) == 2)
    return codes


def test_a_restarted_session_takes_its_name_back_in_each_room(broker: InProcBroker) -> None:
    """The bug: a restarted agent came back as planner-2 and peers briefed it from the start. With
    each room's code from its earlier join, the new session is planner again in both rooms, and the
    room is told it's back, not new."""
    codes = first_session_ends(broker)
    new = codex_session(broker)
    for room in ("#build", "#docs"):
        r = new.call("join", RESTARTED, room=room, screen_name="planner", resume=codes[room])
        assert r["ok"] and r["screen_name"] == "planner", r
        assert CODE.search(r["text"]).group(0) != codes[room]  # a new code for the next restart
    msgs = broker.web.get("/api/rooms/build/messages").json()["messages"]
    joins = [m["text"] for m in msgs if m["kind"] == "join"]
    assert len(joins) == 2 and not joins[0].endswith("restart"), joins
    assert joins[-1].startswith("joined (codex on laptop, ") and joins[-1].endswith(
        ", back after a restart"
    ), joins
    members = broker.web.get("/api/rooms/build/members").json()["members"]
    assert [m["name"] for m in members] == ["planner"]
    rows = q(broker, "SELECT DISTINCT participant_id FROM memberships WHERE screen_name='planner'")
    assert len(rows) == 2  # really another session, not the old one re-bound


def test_without_the_code_or_with_another_the_name_stays_reserved(broker: InProcBroker) -> None:
    """The reservation still does its job: no code, a made-up code, the other room's code or a
    malformed one never hand the name over. The refusal says how to reclaim it."""
    codes = first_session_ends(broker)
    new = codex_session(broker)
    r = new.call("join", RESTARTED, room="#build", screen_name="planner")
    assert r["code"] == "name_reserved" and "resume=" in r["error"], r
    for wrong in ("yk:j" + "0" * 16, codes["#docs"]):
        r = new.call("join", RESTARTED, room="#build", screen_name="planner", resume=wrong)
        assert r["code"] == "name_reserved" and "isn't this name's" in r["error"], (wrong, r)
    for bad in ("yk:jXYZ", "0" * 64, "yk:j" + "A" * 16):
        r = new.call("join", RESTARTED, room="#build", screen_name="planner", resume=bad)
        assert r["code"] == "bad_request", (bad, r)
    assert q(broker, "SELECT COUNT(*) FROM memberships WHERE left_at IS NULL")[0][0] == 0


def test_a_code_never_moves_a_name_to_another_machine_harness_or_a_kicked_agent(
    broker: InProcBroker, tmp_home: Path
) -> None:
    """The join code is bound to who it was given to: on another machine (another person's), from
    another harness, or once the agent was kicked, the right code still doesn't reclaim the name."""
    codes = first_session_ends(broker)
    [(old_id,)] = q(broker, "SELECT DISTINCT participant_id FROM memberships WHERE screen_name='planner'")
    st = broker.state.store
    # a Codex agent on another machine ("pi") that held "bench" here and left: its code is known
    far = st.upsert_participant("codex", session_key("codex", "pi", "s-far"), host="pi")
    room = st.get_room("#build")
    assert room is not None
    gone = st.create_membership(room.id, far.id, "bench", join_code_hash("ab" * 8))
    st.con.execute("UPDATE memberships SET left_at=? WHERE id=?", (st.clock.now(), gone.id))
    st.con.commit()
    new = codex_session(broker)
    r = new.call("join", RESTARTED, room="#build", screen_name="bench", resume="yk:j" + "ab" * 8)
    assert r["code"] == "name_reserved" and "on another machine" in r["error"], r

    async def other_harness() -> dict[str, Any]:
        async with FakeAgent(broker.home, "k9") as a:  # the test harness, not codex
            return await a.call("join", room="#build", screen_name="planner", resume=codes["#build"])

    r = asyncio.run(other_harness())
    assert r["code"] == "name_reserved" and "another harness" in r["error"], r
    st.con.execute(
        "UPDATE memberships SET kicked=1 WHERE participant_id=? AND room_id="
        "(SELECT id FROM rooms WHERE name='#docs')",
        (old_id,),
    )
    st.con.commit()
    r = new.call("join", RESTARTED, room="#docs", screen_name="planner", resume=codes["#docs"])
    assert r["code"] == "name_reserved" and "was kicked" in r["error"], r
    # a wrong code learns nothing about the holder: not that it was kicked (#370)
    r = new.call("join", RESTARTED, room="#docs", screen_name="planner", resume="yk:j" + "0" * 16)
    assert r["code"] == "name_reserved" and "current one" in r["error"], r
    assert "kicked" not in r["error"], r
    r = new.call("join", RESTARTED, room="#build", screen_name="planner", resume=codes["#build"])
    assert r["ok"], r  # the right code, same machine and harness, not kicked here


def test_a_join_from_before_join_codes_says_so(broker: InProcBroker) -> None:
    """#370: a membership with no join code on record (made before join codes could bring a name
    back) refused every code with the same words as a wrong one, so the agent couldn't tell its
    code was fine and the name simply can't come back. It now says so. Fails if the refusal still
    reads as a wrong code."""
    st = broker.state.store
    old = st.upsert_participant("codex", session_key("codex", "laptop", "s-old"), host="laptop")
    room = st.get_room("#build")
    assert room is not None
    gone = st.create_membership(room.id, old.id, "planner", None)
    st.con.execute("UPDATE memberships SET left_at=? WHERE id=?", (st.clock.now(), gone.id))
    st.con.commit()
    new = codex_session(broker)
    r = new.call("join", RESTARTED, room="#build", screen_name="planner", resume="yk:j" + "ab" * 8)
    assert r["code"] == "name_reserved" and "no code on record" in r["error"], r


# ----------------------------------------------------- the refusal tells the way back
def test_the_refusal_names_resume_and_another_name(broker: InProcBroker) -> None:
    """A session that joins under a name its holder left is told what to do: join again with
    ``resume=`` set to the code at the end of its earlier join, or pick another name. Fails if the
    refusal drops the way back, and an agent can only give up the name."""

    async def go() -> str:
        async with FakeAgent(broker.home, "holder") as holder, FakeAgent(broker.home, "caller") as caller:
            await holder.join("#build", "planner")
            await holder.leave("#build")
            r = await caller.call("join", room="#build", screen_name="planner")
            assert r["code"] == "name_reserved", r
            return str(r["error"])

    text = asyncio.run(go())
    assert "join again with resume= set to the code" in text and "pick another" in text, text


def test_a_wrong_code_is_told_it_isnt_the_names(broker: InProcBroker) -> None:
    """A caller that passes ``resume`` already has the parameter: its refusal says the code isn't
    this name's, and doesn't send it round again."""

    async def go() -> str:
        async with FakeAgent(broker.home, "holder") as holder, FakeAgent(broker.home, "caller") as caller:
            await holder.join("#build", "planner")
            await holder.leave("#build")
            r = await caller.call("join", room="#build", screen_name="planner", resume="yk:j" + "0" * 16)
            assert r["code"] == "name_reserved", r
            return str(r["error"])

    text = asyncio.run(go())
    assert "isn't this name's" in text, text
