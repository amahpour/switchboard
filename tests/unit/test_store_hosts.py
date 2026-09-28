"""The host namespace in the store (DESIGN.md §27.5.4, §27.5.6, §27.6).

A participant's pids are pids on its own host ('' is this machine). Every
pid-keyed lookup is scoped to one host, restart recovery probes local rows only,
session keys name their host, and the ``remotes`` table holds the owner's consent.
"""

from __future__ import annotations

import hashlib
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
    fields = dict(agent_pid=4242, agent_start=100.0, mcp_pid=4250, mcp_start=101.0, status="idle")
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


def test_participants_by_mcp_is_host_scoped(store: Store) -> None:
    local = part(store, "claude", "", "4242@100.00")
    remote = part(store, "claude", PI, "4242@100.00")  # the same pids, on the Pi
    assert local.host == "" and remote.host == PI and local.id != remote.id
    assert [p.id for p in store.participants_by_mcp("", 4250, 101.0)] == [local.id]
    assert [p.id for p in store.participants_by_mcp(PI, 4250, 101.0)] == [remote.id]
    assert store.participants_by_mcp("other-pi", 4250, 101.0) == []
    assert store.participants_by_mcp(PI, 4250, 999.0) == []  # a recycled pid on the Pi


def test_active_participants_by_agent_is_host_scoped(store: Store) -> None:
    local = part(store, "cursor", "", "agent:4242@100.00")
    remote = part(store, "cursor", PI, "agent:4242@100.00")
    assert [p.id for p in store.active_participants_by_agent("cursor", "", 4242)] == [local.id]
    assert [p.id for p in store.active_participants_by_agent("cursor", PI, 4242)] == [remote.id]
    assert store.active_participants_by_agent("claude", PI, 4242) == []


def test_recover_on_start_probes_local_rows_only(store: Store, clock: FakeClock) -> None:
    room = store.create_room("#build", "alice", 60, 6)
    local = part(store, "claude", "", "4242@100.00", status="busy")
    remote = part(store, "claude", PI, "4242@100.00", status="busy")
    store.create_membership(room.id, local.id, "vivado", "h1")
    store.create_membership(room.id, remote.id, "bench", "h2")
    asked: list[tuple[int, float | None]] = []

    def alive(pid: int, start: float | None) -> bool:
        asked.append((pid, start))
        return False  # every pid looks dead on this machine

    res = store.recover_on_start(alive)
    assert asked == [(4242, 100.0)]  # once: the local row; the Pi's pid is never looked up here
    assert res["participants_ended"] == 1
    lp, rp = store.get_participant(local.id), store.get_participant(remote.id)
    assert lp is not None and not lp.active
    assert rp is not None and rp.active and rp.status == "offline"  # waits for its link
    assert [m.name for m in store.members(room.id)] == ["bench"]


def test_recover_on_start_never_ends_on_unknown(store: Store) -> None:
    local = part(store, "claude", "", "4242@100.00")
    store.recover_on_start(lambda pid, start: None)
    p = store.get_participant(local.id)
    assert p is not None and p.active


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
    lp = part(store, "claude", "", "1@1.00")
    rp = part(store, "claude", PI, "1@1.00")
    lm = store.create_membership(room.id, lp.id, "vivado", "h1")
    rm = store.create_membership(room.id, rp.id, "bench", "h2")
    assert {m.name: m.host for m in store.members(room.id)} == {"vivado": "", "bench": PI}
    a = store.insert_message(room.id, sender_name="vivado", sender_kind="agent", via="mcp", text="hi",
                             sender_membership_id=lm.id, sender_harness="claude")
    b = store.insert_message(room.id, sender_name="bench", sender_kind="agent", via="mcp", text="hi",
                             sender_membership_id=rm.id, sender_harness="claude", sender_host=PI)
    assert a.sender_host is None and b.sender_host == PI
    assert [m.sender_host for m in store.history(room.id)] == [None, PI]
    with pytest.raises(ValueError):
        store.insert_message(room.id, sender_name="x", sender_kind="agent", via="mcp", text="t",
                             sender_host="Not A Host")


def test_remotes_rows(store: Store, clock: FakeClock) -> None:
    h1 = hashlib.sha256(b"config one").hexdigest()
    h2 = hashlib.sha256(b"config two").hexdigest()
    assert store.remote_row(PI) is None and store.remote_rows() == []
    assert store.set_remote_blocked(PI, "auth") is False  # nothing to block before an enable
    r = store.set_remote_enabled(PI, h1, "cli")
    assert r.enabled_for(h1) and not r.enabled_for(h2) and r.enabled_via == "cli"
    assert r.enabled_at == clock.now() and not r.blocked
    clock.advance(5)
    assert store.touch_remote_up(PI)
    assert store.set_remote_blocked(PI, "host_key")
    r = store.remote_row(PI)
    assert r is not None and r.blocked and r.blocked_reason == "host_key" and r.last_up_at == clock.now()
    assert r.enabled_for(h1)  # blocked is separate from consent
    assert not r.may_dial(h1)  # but a blocked remote is never dialed (no retry until an enable)
    # enabling (for a changed config) clears the block
    clock.advance(5)
    r = store.set_remote_enabled(PI, h2, "web")
    assert r.enabled_for(h2) and not r.enabled_for(h1) and not r.blocked and r.enabled_via == "web"
    assert r.may_dial(h2) and not r.may_dial(h1)
    assert store.set_remote_disabled(PI)
    r = store.remote_row(PI)
    assert r is not None and not r.enabled_for(h2) and not r.may_dial(h2) and r.config_hash == h2
    assert [x.name for x in store.remote_rows()] == [PI]
    assert store.clear_remote(PI) and store.remote_row(PI) is None
    assert store.clear_remote(PI) is False


@pytest.mark.parametrize("bad", [
    dict(name="Bad Name"), dict(config_hash="abc"), dict(config_hash="A" * 64), dict(via="ssh"),
])
def test_remotes_rows_refuse_bad_values(store: Store, bad: dict[str, str]) -> None:
    args = dict(name=PI, config_hash="a" * 64, via="cli")
    args.update(bad)
    with pytest.raises(ValueError):
        store.set_remote_enabled(args["name"], args["config_hash"], args["via"])
    with pytest.raises(ValueError):
        store.set_remote_blocked(PI, "Host Key Changed!")
    with pytest.raises(ValueError):
        store.clear_remote("../x")


@pytest.mark.parametrize("reason", ["host_key", "auth", "replaced"])
def test_a_blocked_remote_is_not_dialable_until_the_next_enable(store: Store, reason: str) -> None:
    """``may_dial`` is the one dial gate (§27.4.7, §27.5.8): a block, for a host-key
    mismatch above all (a possible man in the middle), is never retried by itself."""
    h = hashlib.sha256(b"config").hexdigest()
    store.set_remote_enabled(PI, h, "cli")
    assert store.remote_row(PI).may_dial(h)
    store.set_remote_blocked(PI, reason)
    r = store.remote_row(PI)
    assert r.enabled_for(h) and r.blocked and not r.may_dial(h)
    store.touch_remote_up(PI)  # a later link-up record doesn't lift it
    assert not store.remote_row(PI).may_dial(h)
    assert store.set_remote_enabled(PI, h, "web").may_dial(h)  # the owner enabled it again
