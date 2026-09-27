"""rules.releasable and friends: quiet period, max hold, batch cap, human first,
one peer batch per turn boundary, busy = priority only, effective status (DESIGN.md §8.2)."""

from __future__ import annotations

import dataclasses

import pytest

from switchboard.delivery import rules
from switchboard.models import Item, Room

NOW = 1_790_000_000.0


def item(mid: int, prio: int, *, ts: float = NOW - 100, text: str = "x", notified: float | None = None,
         kind: str | None = None) -> Item:
    return Item(membership_id=1, message_id=mid, prio=prio, mentioned=prio == 1, state="pending",
                batch_id=None, attempts=0, notified_at=notified, ts=ts,
                sender_name="alice" if prio == 2 else "peer",
                sender_kind=kind or ("human" if prio == 2 else "agent"), sender_harness=None,
                text=text, reply_to=None)


def room(**kw) -> Room:
    base = dict(id=1, name="#build", created_at=0.0, created_by="alice", paused=False, paused_reason=None,
                budget_per_hour=60, budget_remaining=60, budget_window_start=NOW - 10,
                budget_notice_window=None, hop_count=0, hop_limit=6, last_msg_at=NOW - 100)
    base.update(kw)
    return Room(**base)


def rel(pending, *, eff="idle", r=None, pbb=-1, seq=0, now=NOW, quiet=3.0, hold=60.0, msgs=20, chars=6000):
    return rules.releasable(pending, room=r or room(), eff=eff, peer_batch_boundary=pbb, boundary_seq=seq,
                            now=now, quiet_s=quiet, max_hold_s=hold, batch_max_msgs=msgs, max_chars=chars)


def ids(r) -> list[int]:
    return [i.message_id for i in r.items]


def test_busy_gets_priority_only_and_is_not_counted() -> None:
    r = rel([item(1, 0), item(2, 1), item(3, 2)], eff="busy")
    assert r is not None and r.kind == "priority" and not r.counted
    assert ids(r) == [3, 2]  # human first, then the mention; never chatter
    assert rel([item(1, 0)], eff="busy") is None


def test_idle_priority_wakes_with_chatter_when_peer_ok() -> None:
    r = rel([item(1, 0), item(2, 1)], eff="idle", pbb=-1, seq=0)
    assert r.kind == "wake" and r.counted and ids(r) == [2, 1] and r.reason == "mention"
    r = rel([item(1, 0), item(2, 1)], eff="idle", pbb=0, seq=0)  # a peer batch already went out
    assert ids(r) == [2]


def test_starting_counts_as_idle_and_holds_block() -> None:
    assert rel([item(1, 2)], eff="starting").kind == "wake"
    assert rel([item(1, 2)], eff="waiting-approval") is None
    assert rel([item(1, 2)], eff="offline") is None


def test_quiet_period_and_max_hold_for_chatter() -> None:
    now = NOW
    chatter = [item(1, 0, ts=now - 1)]
    assert rel(chatter, r=room(last_msg_at=now - 1), now=now) is None  # room not quiet yet
    assert rel(chatter, r=room(last_msg_at=now - 3), now=now) is not None  # quiet 3 s
    # continuous chatter never goes quiet: the max hold releases it
    old = [item(1, 0, ts=now - 61), item(2, 0, ts=now - 0.5)]
    r = rel(old, r=room(last_msg_at=now - 0.2), now=now)
    assert r is not None and ids(r) == [1, 2]
    assert rel([item(1, 0, ts=now - 59)], r=room(last_msg_at=now - 0.2), now=now) is None


def test_human_first_ordering() -> None:
    items = [item(5, 0), item(1, 1), item(9, 2), item(3, 2), item(2, 0)]
    assert [i.message_id for i in rules.human_first(items)] == [3, 9, 1, 2, 5]


def test_cap_by_messages_and_characters() -> None:
    many = [item(i, 2) for i in range(1, 30)]
    assert len(rel(many, msgs=20).items) == 20
    big = [item(i, 2, text="y" * 1400) for i in range(1, 10)]
    r = rel(big, chars=3000)
    assert 1 <= len(r.items) <= 2
    # a single huge item always fits (the envelope truncates it)
    assert len(rel([item(1, 2, text="z" * 50000)], chars=100).items) == 1


def test_notified_stubs_never_wake_again() -> None:
    assert rel([item(1, 1, notified=NOW - 5)]) is None
    assert rel([item(1, 1, notified=NOW - 5)], eff="busy") is None
    got, more = rules.pull_items([item(2, 0), item(1, 1, notified=NOW - 5)], 10)
    assert [i.message_id for i in got] == [1, 2] and not more  # pulls include them, oldest first


def test_pull_items_limit_and_more() -> None:
    got, more = rules.pull_items([item(i, 0) for i in range(5, 0, -1)], 3)
    assert [i.message_id for i in got] == [1, 2, 3] and more


def test_effective_status_open_sink_is_idle() -> None:
    assert rules.effective_status("busy", True) == "idle"
    assert rules.effective_status("busy", False) == "busy"
    assert rules.effective_status("waiting-approval", True) == "waiting-approval"
    assert rules.effective_status("offline", True) == "offline"


def test_peer_ok_one_batch_per_boundary() -> None:
    assert rules.peer_ok(-1, 0) and rules.peer_ok(2, 3)
    assert not rules.peer_ok(3, 3)


def test_classify() -> None:
    assert rules.classify("human", "bob", []) == (2, False)
    assert rules.classify("human", "bob", ["bob"]) == (2, True)
    assert rules.classify("agent", "bob", ["BOB"]) == (1, True)
    assert rules.classify("agent", "bob", ["amy"]) == (0, False)


def test_clamp_wait() -> None:
    assert rules.clamp_wait(500, 50) == 50
    assert rules.clamp_wait(0, 50) == 1
    assert rules.clamp_wait(None, 110) == 50
    assert rules.clamp_wait("x", 110) == 50


@pytest.mark.parametrize("eff", ["idle", "busy"])
def test_release_items_are_a_subset_of_pending(eff: str) -> None:
    pend = [item(i, i % 3) for i in range(1, 12)]
    r = rel(pend, eff=eff)
    assert r is not None and {i.message_id for i in r.items} <= {i.message_id for i in pend}


def test_release_is_pure() -> None:
    pend = [item(1, 0), item(2, 2)]
    r1, r2 = rel(pend), rel(pend)
    assert r1 == r2 and pend == [item(1, 0), item(2, 2)]
    assert dataclasses.is_dataclass(r1)
