"""The host namespace in the store (DESIGN.md §6.3, §49.3).

A participant's host is its agent's device ('' only for the scripted test agent), and its session key
names that host, so one session id on two devices is two sessions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.models import HOST_RE, session_key, valid_host
from switchboard.store import Store

PI = "fpga-pi"


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "y.db"), clock)


def part(store: Store, harness: str, host: str, rest: str, **kw):
    fields = dict(status="idle")
    fields.update(kw)
    return store.upsert_participant(harness, session_key(harness, host, rest), host=host, **fields)


def test_session_key_helper() -> None:
    assert session_key("claude", "", "4242@1727000000.51") == "claude:4242@1727000000.51"
    assert session_key("claude", PI, "4242@1727000000.51") == "claude@fpga-pi:4242@1727000000.51"
    assert session_key("codex", PI, "thread-1") == "codex@fpga-pi:thread-1"
    assert session_key("cursor", PI, "agent:7@1.00") == "cursor@fpga-pi:agent:7@1.00"
    assert session_key("test", "", "a") == "test:a"
    # host names never hold '@' or ':' (so a key can't be forged across hosts)
    for bad in ("Fpga", "fpga@pi", "fpga:pi", "-pi", "9pi", "a" * 25, "pi pi", " "):
        assert not valid_host(bad)
        with pytest.raises(ValueError):
            session_key("claude", bad, "x")
    assert valid_host("a") and valid_host("pi-2") and valid_host("a" * 24)
    assert HOST_RE.pattern == r"^[a-z][a-z0-9-]{0,23}$"


def test_session_keys_and_hosts_must_agree(store: Store) -> None:
    with pytest.raises(ValueError, match="not a claude key of host"):
        store.upsert_participant("claude", "claude:4242@1.00", host=PI)
    with pytest.raises(ValueError, match="not a claude key of host"):
        store.upsert_participant("claude", "claude@fpga-pi:4242@1.00")  # host '' by default
    with pytest.raises(ValueError, match="not a host name"):
        store.upsert_participant("claude", "claude@Bad:1", host="Bad")
    p = part(store, "codex", PI, "thread-1")
    with pytest.raises(ValueError, match="never moves between hosts"):
        store.upsert_participant("codex", p.session_key, host="")
    with pytest.raises(ValueError, match="never changes"):
        store.update_participant(p.id, host="other")
    # a refresh from its own host is fine
    again = store.upsert_participant("codex", p.session_key, host=PI, status="busy")
    assert again.id == p.id and again.host == PI and again.status == "busy"


def test_members_and_messages_carry_host(store: Store) -> None:
    room = store.create_room("#build", "alice", 60, 6)
    lp = part(store, "test", "", "vivado")
    rp = part(store, "claude", PI, "s-1")
    lm = store.create_membership(room.id, lp.id, "vivado")
    rm = store.create_membership(room.id, rp.id, "bench")
    assert {m.name: m.host for m in store.members(room.id)} == {"vivado": "", "bench": PI}
    a = store.insert_message(
        room.id,
        sender_name="vivado",
        sender_kind="agent",
        via="mcp",
        text="hi",
        sender_membership_id=lm.id,
        sender_harness="test",
    )
    b = store.insert_message(
        room.id,
        sender_name="bench",
        sender_kind="agent",
        via="mcp",
        text="hi",
        sender_membership_id=rm.id,
        sender_harness="claude",
        sender_host=PI,
    )
    assert a.sender_host is None and b.sender_host == PI
    assert [m.sender_host for m in store.history(room.id)] == [None, PI]
    with pytest.raises(ValueError):
        store.insert_message(
            room.id, sender_name="x", sender_kind="agent", via="mcp", text="t", sender_host="Not A Host"
        )
