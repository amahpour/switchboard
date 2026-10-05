"""The Inspector's member detail, ``GET /api/rooms/{slug}/members/{name}`` (DESIGN.md §29).

A real in-process broker with scripted test agents (``switchboard mcp --harness test``). The
route is human-only (the web session cookie), GET-only and read-only, and it never carries
message text: queued items and ``said`` entries are ids, and every timeline entry is rebuilt
from a per-kind whitelist, so engine-internal event data (and any path or email address in a
reason) never reaches the page as it was stored.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FakeClock, InProcBroker
from engine_world import World
from fakes.fake_agent import FakeAgent

from switchboard.broker.service import _last_seen
from switchboard.config import Config
from switchboard.delivery.engine import STOP_BUSY_SRCS

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
SECRET = "the quick secret text 7f3a"  # a message body that must never appear in a detail
MEMBER_KEYS = {
    "name",
    "harness",
    "status",
    "tier",
    "tier_note",
    "away",
    "approval_mode",
    "env_leak",
    "held",
    "queued",
    "inflight",
    "parked",
    "parked_reason",
    "host",
    "joined_at",
    "held_at",
    "status_at",
    "status_src",
    "last_seen",
    "last_seen_what",
}


@pytest.fixture
def broker(tmp_home: Path) -> Iterator[InProcBroker]:
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web  # type: ignore[attr-defined]
    try:
        yield b
    finally:
        web.close()
        b.stop()


def detail(b: InProcBroker, name: str) -> httpx.Response:
    return b.web.get(f"/api/rooms/build/members/{name}")  # type: ignore[attr-defined]


def web_cmd(b: InProcBroker, text: str) -> httpx.Response:
    return b.web.post(
        "/api/rooms/build/command",
        json={"text": text},  # type: ignore[attr-defined]
        headers=b.write_headers(),
    )


def q(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def membership_id(b: InProcBroker, name: str) -> int:
    return q(b, "SELECT id FROM memberships WHERE screen_name=? AND left_at IS NULL", name)[0]["id"]


async def until(pred: Any, what: str) -> None:
    """Poll a condition (never a fixed sleep): up to 10 s."""
    deadline = time.monotonic() + 10
    while not pred():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.02)


# -------------------------------------------------------------- the gate
def test_needs_the_web_session(broker: InProcBroker) -> None:
    c = httpx.Client(base_url=broker.base)
    assert c.get("/api/rooms/build/members/claude-1").status_code == 401
    # GET-only: with a session, origin and header, any write method is refused
    for m in ("POST", "PUT", "DELETE", "PATCH"):
        r = broker.web.request(
            m,
            "/api/rooms/build/members/claude-1",  # type: ignore[attr-defined]
            json={},
            headers=broker.write_headers(),
        )
        assert r.status_code == 405, (m, r.status_code)


def test_bad_names_and_rooms(broker: InProcBroker) -> None:
    assert detail(broker, "BAD NAME").status_code == 400
    assert detail(broker, "x" * 40).status_code == 400
    r = detail(broker, "nobody")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert detail(broker, "alice").status_code == 404  # the human is not a member
    assert broker.web.get("/api/rooms/nope/members/claude-1").status_code == 404  # type: ignore[attr-defined]


# ------------------------------------------------------------ the shape
async def test_shape_queue_timeline_and_no_text(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "a1") as a, FakeAgent(broker.home, "b1") as b:
        await a.join("#build", "claude-1")
        await b.join("#build", "codex-1")
        r = detail(broker, "@Claude-1")  # case and a leading @ are forgiven
        assert r.status_code == 200, r.text
        d = r.json()
        assert set(d) == {"room", "member", "session", "queued", "counts", "timeline"}
        assert d["room"] == "#build"
        assert set(d["member"]) == MEMBER_KEYS
        assert d["member"]["name"] == "claude-1" and d["member"]["harness"] == "test"
        joined = q(broker, "SELECT joined_at FROM memberships WHERE screen_name='claude-1'")[0][0]
        assert d["member"]["joined_at"] == joined
        # a test session has no session id to hand on (DESIGN.md §26)
        assert d["session"] == {"id": None, "why": "a test session", "where": "this machine"}
        assert d["queued"] == [] and d["timeline"] == []

        # held, a peer's message stays queued: its id (never its text) is listed
        assert web_cmd(broker, "/hold claude-1").status_code == 200
        posted = await b.say("#build", f"{SECRET} for @claude-1")
        mid = posted["posted_id"]
        d = detail(broker, "claude-1").json()
        assert d["member"]["held"] is True and d["member"]["held_at"] is not None
        assert {"id": mid, "prio": "mention"} in d["queued"]
        assert d["counts"].get("pending", 0) >= 1

        # the sender's own say() shows as a "said" entry, then its pass() as "pass"
        d = detail(broker, "codex-1").json()
        assert [e for e in d["timeline"] if e["kind"] == "said"] == [
            {"ts": d["timeline"][-1]["ts"], "kind": "said", "id": mid}
        ]
        assert d["member"]["last_seen_what"] == "said" and d["member"]["last_seen"] is not None
        await b.read("#build")
        res = await b.pass_("#build")
        assert res["ok"] is True, res
        d = detail(broker, "codex-1").json()
        assert [e["kind"] for e in d["timeline"]][-2:] == ["said", "pass"]
        assert d["timeline"][-1] == {"ts": d["timeline"][-1]["ts"], "kind": "pass"}
        assert d["member"]["last_seen_what"] in ("passed", "seen")

        # no message text anywhere, and no timeline entry has a text field
        for who in ("claude-1", "codex-1"):
            d = detail(broker, who).json()
            assert SECRET not in json.dumps(d)
            assert all("text" not in e and "data" not in e for e in d["timeline"])


async def test_offer_entry_names_senders(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "a2") as a, FakeAgent(broker.home, "b2") as b:
        await a.join("#build", "claude-1")
        await b.join("#build", "bench")
        task = a.wait_task("#build", 20)
        mid = membership_id(broker, "claude-1")
        await until(
            lambda: (
                mid
                in broker.on_loop(lambda: {s.membership_id for s in broker.state.engine.sinks.open_sinks()})
            ),
            "claude-1 never waited",
        )
        await b.say("#build", f"{SECRET} ping")
        got = await task
        assert got.get("messages") or got.get("text"), got
        offers = [e for e in detail(broker, "claude-1").json()["timeline"] if e["kind"] == "offer"]
        assert offers, "no offer entry"
        o = offers[-1]
        assert set(o) == {"ts", "kind", "path", "n", "counted", "prio", "from"}
        assert o["path"] == "wait" and o["n"] == 1 and o["from"] == ["bench"] and o["prio"] == "chatter"


# ------------------------------------------------------ the whitelist
async def test_timeline_whitelists_event_data(broker: InProcBroker) -> None:
    """Engine events carry internal data. Each kind comes back with only its listed fields,
    and free-form reasons are scrubbed of paths and email addresses and capped."""
    leak = "stuck in /Users/x/secret/repo, ask a@b.com"
    async with FakeAgent(broker.home, "a3") as a:
        await a.join("#build", "devin-1")
        mid = membership_id(broker, "devin-1")
        st = broker.state

        def add(kind: str, **data: Any) -> None:
            broker.on_loop(
                lambda: st.store.add_event(
                    kind, room_id=None, membership_id=mid, data={"text": SECRET, "ids": [1, 2], **data}
                )
            )

        add("parked", reason=leak)
        add("unparked", seconds=12.5)
        add("rearm", n=2)
        add("requeue", reason="redeliver", n=3)
        add("expire", path="inbox", reason="x" * 500)
        add("cancel", path="/etc/passwd", reason="kick")
        tl = detail(broker, "devin-1").json()["timeline"]
        assert [e["kind"] for e in tl] == ["parked", "unparked", "rearm", "requeue", "expire", "cancel"]
        by = {e["kind"]: {k: v for k, v in e.items() if k != "ts"} for e in tl}
        assert by["parked"] == {"kind": "parked", "reason": "stuck in <path>, ask <email>"}
        assert by["unparked"] == {"kind": "unparked", "seconds": 12.5}
        assert by["rearm"] == {"kind": "rearm", "n": 2}
        assert by["requeue"] == {"kind": "requeue", "reason": "redeliver", "n": 3}
        assert by["expire"]["path"] == "inbox" and len(by["expire"]["reason"]) == 200
        assert by["cancel"] == {"kind": "cancel", "path": "other", "reason": "kick"}

        add("watchdog_remind", n=1)
        add("watchdog_escalate", why="parked", n=True, status="busy")
        add("offer", path="steer", n=4, counted=1, ids=["x", True])
        add("login", what="session")  # not a timeline kind
        tl = detail(broker, "devin-1").json()["timeline"]
        by = {e["kind"]: {k: v for k, v in e.items() if k != "ts"} for e in tl}
        assert "login" not in by
        assert by["watchdog_remind"] == {"kind": "watchdog_remind", "n": 1, "why": None}
        assert by["watchdog_escalate"] == {"kind": "watchdog_escalate", "n": None, "why": "parked"}
        # the "ids" the events carry are ids of no delivery of this member: nobody is named
        assert by["offer"] == {
            "kind": "offer",
            "path": "steer",
            "n": 4,
            "counted": True,
            "prio": None,
            "from": [],
        }
        body = detail(broker, "devin-1").text
        assert SECRET not in body and "/Users/x" not in body and "a@b.com" not in body


async def test_parked_reason_is_scrubbed(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "a4") as a:
        await a.join("#build", "devin-1")
        mid = membership_id(broker, "devin-1")
        broker.on_loop(
            lambda: broker.state.engine.parked.__setitem__(mid, "wedged at /Users/x/secret for a@b.com")
        )
        d = detail(broker, "devin-1").json()
        assert d["member"]["parked"] is True
        assert d["member"]["parked_reason"] == "wedged at <path> for <email>"
        assert "/Users/x/secret" not in json.dumps(d) and "a@b.com" not in json.dumps(d)


async def test_a_departed_member_is_404(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "a5") as a:
        await a.join("#build", "claude-1")
        assert detail(broker, "claude-1").status_code == 200
        await a.leave("#build")
        r = detail(broker, "claude-1")
        assert r.status_code == 404 and "claude-1 is not in #build" in r.json()["message"]


async def test_the_members_frame_is_unchanged(broker: InProcBroker) -> None:
    """The detail's extra keys stay out of the broadcast members list (and so out of
    ``switchboard tail`` and every WebSocket frame)."""
    async with FakeAgent(broker.home, "a6") as a:
        await a.join("#build", "claude-1")
        rows = broker.web.get("/api/rooms/build/members").json()["members"]  # type: ignore[attr-defined]
        assert set(rows[0]) == MEMBER_KEYS - {
            "joined_at",
            "held_at",
            "status_at",
            "status_src",
            "last_seen",
            "last_seen_what",
        }


def test_last_seen_names_the_newest_sign_of_life() -> None:
    assert _last_seen({}) == (None, None)
    assert _last_seen({"last_seen": 5.0, "last_say_at": 5.0, "last_pass_at": 1.0}) == (5.0, "said")
    assert _last_seen({"last_seen": 3.0, "last_say_at": 1.0, "last_pass_at": 3.0}) == (3.0, "passed")
    # a turn end is what the engine writes for a Stop or an Interrupt hook (set_status, "hook:{E}")
    assert _last_seen({"last_seen": 9.0, "last_say_at": 1.0, "status_src": "hook:Stop"}) == (
        9.0,
        "turn ended",
    )
    assert _last_seen({"last_seen": 9.0, "status_src": "hook:Interrupt"}) == (9.0, "turn ended")
    # review finding: the stop:* sources mean switchboard kept the turn going (busy), not an end
    for src in STOP_BUSY_SRCS:
        assert _last_seen({"last_seen": 9.0, "status_src": src}) == (9.0, "seen")
    assert _last_seen({"last_seen": 9.0, "status_src": "hook:PreToolUse"}) == (9.0, "seen")


@pytest.mark.parametrize("event", ["Stop", "Interrupt"])
def test_a_real_turn_end_reads_turn_ended(tmp_path: Path, clock: FakeClock, event: str) -> None:
    """Review finding: the label keyed on ``stop*`` sources, which the engine writes only when it
    keeps a turn going. Drive the engine's own hook path and feed what it stored to _last_seen."""
    w = World(tmp_path, clock)
    p, _m = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    clock.advance(5)
    w.hook(p, event)
    got = w.p(p)
    assert got.status == "idle" and got.status_src == f"hook:{event}"
    assert _last_seen({"last_seen": got.last_seen, "status_src": got.status_src}) == (
        got.last_seen,
        "turn ended",
    )
