"""``switchboard report`` (DESIGN.md §12.5, §12.6): unreadable databases, damaged event
rows, a /hold that is never released, tiers that change over time, a member outside
the window and park events that name no member."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard import report
from switchboard.models import Message


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock)


def build(w: World, **kw: Any) -> dict[str, Any]:
    return report.build(w.store.con, "#build", now=w.clock.now(), **kw)


def row(rows: list[dict[str, Any]], **match: Any) -> dict[str, Any]:
    got = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
    assert len(got) == 1, (match, rows)
    return got[0]


def quiet_msg(w: World, text: str) -> Message:
    """A human message with its delivery rows but no engine action (offered by hand)."""
    return w.store.insert_message(w.room.id, sender_name="alice", sender_kind="human", via="web", text=text)


def offer(
    w: World, m: Any, msgs: list[Message], *, path: str, kind: str, wake_kind: str | None = None
) -> int:
    ids = [x.id for x in msgs]
    b = w.store.create_batch(
        m.id,
        path=path,
        kind=kind,
        items=[(i, True) for i in ids],
        wake_kind=wake_kind,
        wake_reason="human" if kind == "wake" else None,
        counted=kind == "wake",
    )
    w.store.add_event(
        "offer",
        room_id=w.room.id,
        membership_id=m.id,
        participant_id=m.participant_id,
        data={"batch_id": b.id, "path": path, "n": len(ids), "counted": kind == "wake", "ids": ids},
    )
    return b.id


def raw_event(w: World, kind: str, data: str, **cols: Any) -> None:
    """An events row written as-is (a damaged or foreign row: not through ``add_event``)."""
    w.store.con.execute(
        "INSERT INTO events(ts, room_id, membership_id, participant_id, kind, data) VALUES(?,?,?,?,?,?)",
        (
            w.clock.now(),
            cols.get("room_id"),
            cols.get("membership_id"),
            cols.get("participant_id"),
            kind,
            data,
        ),
    )
    w.store.con.commit()


# ------------------------------------------------------------------ helpers
def test_iso_times() -> None:
    assert report.iso(None) is None
    assert report.iso(0) == "1970-01-01T00:00:00Z"
    assert report.iso(1_790_344_800.0) == "2026-09-25T14:00:00Z"


def test_a_file_that_is_not_a_database_is_a_report_error(tmp_path: Path) -> None:
    junk = tmp_path / "switchboard.db"
    junk.write_bytes(b"this is not an SQLite database, just some bytes" * 100)
    with pytest.raises(report.ReportError, match="^can't read the switchboard database: "):
        report.open_ro(junk)


def test_a_database_without_the_tables_is_a_report_error(tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()
    with pytest.raises(report.ReportError, match="no such table: rooms"):
        report.open_ro(other)


def test_damaged_event_data_reads_as_empty(w: World) -> None:
    raw_event(w, "expire", "{not json", room_id=w.room.id)
    raw_event(w, "expire", "[1, 2]", room_id=w.room.id)  # JSON, but not an object
    raw_event(w, "watchdog_escalate", "", room_id=w.room.id)
    w.store.add_event("expire", room_id=w.room.id, data={"path": "inbox", "reason": "ttl"})
    rep = build(w)
    assert rep["rules"]["expired"] == {"?:?": 2, "inbox:ttl": 1}
    assert rep["rules"]["watchdog_escalate"] == {"?": 1}
    assert report._data(None) == {} and report._data(7) == {} and report._data('"text"') == {}
    assert report._data('{"a": 1}') == {"a": 1}


# ------------------------------------------------------------- holds, tiers
def test_a_hold_never_released_holds_until_now(w: World, clock: FakeClock) -> None:
    """A /hold without a /release still holds at the end: a message that waited on it is listed apart."""
    px, mx = w.agent("codex-1", harness="codex", status="idle", hooks=True)
    w.store.add_event("hold", room_id=w.room.id, data={"via": "web", "membership_id": mx.id})
    raw_event(w, "hold", "[3]", room_id=w.room.id)  # names no membership: ignored
    clock.advance(1.0)
    msg = quiet_msg(w, "while held")
    clock.advance(4.0)
    b = offer(w, mx, [msg], path="turn_start", kind="wake", wake_kind="idle_wake")
    w.store.set_batch_times(b, turn_start_at=clock.now())
    w.store.confirm_batch(b, "rpc:turn/start")
    clock.advance(30.0)
    rep = build(w)
    assert rep["latency"]["detail"] == []
    held = row(rep["latency"]["held"], harness="codex", path="turn_start")
    assert held["n"] == 1 and held["p50_ms"] == 4000.0
    assert rep["rules"]["hold"] == 2 and rep["rules"]["release"] == 0


def test_each_batch_gets_the_tier_of_its_time_and_the_agent_the_tier_at_the_end(
    w: World, clock: FakeClock
) -> None:
    pu, mu = w.agent("cursor-1", harness="cursor", status="idle", hooks=True)
    w.store.add_event(
        "join",
        room_id=w.room.id,
        membership_id=mu.id,
        participant_id=pu.id,
        data={"harness": "cursor", "tier": "cursor:stop-park"},
    )
    a = quiet_msg(w, "first")
    clock.advance(0.1)
    b1 = offer(w, mu, [a], path="hook_ctx", kind="priority")
    clock.advance(0.2)
    w.store.confirm_batch(b1, "hook_ack")
    clock.advance(1.0)
    w.store.add_event("tier", participant_id=pu.id, data={"tier": "mcp-only", "what": "degraded"})
    clock.advance(1.0)
    c = quiet_msg(w, "second")
    clock.advance(0.5)
    b2 = offer(w, mu, [c], path="hook_ctx", kind="priority")
    w.store.confirm_batch(b2, "hook_ack")
    # after the room's last activity: outside the window, so neither the agent's tier nor the rules see it
    clock.advance(60.0)
    w.store.add_event("tier", participant_id=pu.id, data={"tier": "cursor:stop-park"})
    w.store.update_participant(pu.id, tier="cursor:stop-park")
    rep = build(w)
    d = rep["latency"]["detail"]
    early = row(d, tier="cursor:stop-park")
    late = row(d, tier="mcp-only")
    assert (early["n"], early["p50_ms"]) == (1, 300.0)
    assert (late["n"], late["p50_ms"]) == (1, 500.0)
    [ag] = rep["agents"]
    assert ag["tier"] == "mcp-only" and ag["tier_note"] is None
    assert rep["rules"]["degraded"] == 1


def test_a_batch_for_a_member_outside_the_window_is_skipped(w: World, clock: FakeClock) -> None:
    """A wall-clock step back can leave a batch stamped after its member left: with a window that
    starts after the leave, the member is not in the report and neither is that batch."""
    px, mx = w.agent("codex-1", harness="codex", status="idle", hooks=True)
    t0 = clock.now()
    clock.advance(100.0)
    msg = quiet_msg(w, "task")
    clock.advance(0.2)
    b = offer(w, mx, [msg], path="turn_start", kind="wake", wake_kind="idle_wake")
    w.store.set_batch_times(b, turn_start_at=clock.now())
    w.store.confirm_batch(b, "rpc:turn/start")
    clock.advance(-60.0)  # the clock steps back
    w.store.end_membership(mx.id, "left")
    everything = build(w)
    assert row(everything["latency"]["detail"], path="turn_start")["n"] == 1
    assert everything["traffic"]["members"] == 1
    windowed = build(w, since=t0 + 50.0)
    assert windowed["traffic"]["members"] == 0 and windowed["agents"] == []
    assert windowed["latency"]["detail"] == [] and windowed["latency"]["held"] == []
    assert windowed["latency"]["first_delivery_paths"] == {}
    assert windowed["traffic"]["human_messages"] == 1  # the message itself is in the window


def test_park_events_without_a_member_are_ignored(w: World, clock: FakeClock) -> None:
    pc, mc = w.agent("claude-1", harness="claude", status="idle", hooks=True)
    w.store.add_event("parked", room_id=w.room.id, data={"reason": "no way to wake it"})
    w.store.add_event("parked", room_id=w.room.id, membership_id=mc.id, data={"reason": "detached"})
    clock.advance(12.0)
    w.store.add_event("unparked", room_id=w.room.id)
    w.store.add_event("unparked", room_id=w.room.id, membership_id=mc.id)
    rep = build(w)
    [ag] = rep["agents"]
    assert ag["parked"] == {"spells": 1, "seconds": 12.0, "reasons": {"detached": 1}}
    assert rep["rules"]["parked"] == 1
