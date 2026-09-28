"""SinkRegistry lookups and closing (DESIGN.md §8.2), @mention parsing (§8.1),
and the chatter release rule with nothing queued (§8.2)."""

from __future__ import annotations

from typing import Any

from switchboard.delivery import rules
from switchboard.delivery.sinks import SinkRegistry
from switchboard.models import Room


def wait_sink(reg: SinkRegistry, participant_id: int, wait_id: str) -> Any:
    return reg.open(participant_id=participant_id, membership_id=participant_id * 10, room_id=1, kind="wait",
                    path="wait", wait_id=wait_id, opened_at=0.0, deadline=50.0)


def test_find_wait_matches_participant_and_wait_id_open_or_recently_closed() -> None:
    reg = SinkRegistry()
    s = wait_sink(reg, 1, "w1")
    assert reg.find_wait(1, "w1") is s
    assert reg.find_wait(1, "w2") is None
    assert reg.find_wait(2, "w1") is None  # another participant's wait id
    reg.close(s.id, {"status": "timeout"}, "timeout")
    assert reg.find_wait(1, "w1") is s  # a late unwait still finds it


def test_closing_an_unknown_or_closed_sink_returns_none_and_keeps_the_first_result() -> None:
    reg = SinkRegistry()
    s = wait_sink(reg, 1, "w1")
    assert reg.close(s.id + 1, {"status": "timeout"}, "timeout") is None
    assert reg.close(s.id, {"status": "messages"}, "filled") is s
    assert reg.close(s.id, {"status": "timeout"}, "timeout") is None
    assert (s.closed, s.close_reason, s.result) == (True, "filled", {"status": "messages"})


def test_only_the_most_recent_closed_sinks_are_remembered() -> None:
    reg = SinkRegistry()
    n = SinkRegistry.RECENT_MAX + 1
    sinks = [wait_sink(reg, 1, f"w{i}") for i in range(n)]
    for s in sinks:
        reg.close(s.id, None, "unwait")
    assert reg.get(sinks[0].id) is None and reg.find_wait(1, "w0") is None  # the oldest is forgotten
    assert reg.get(sinks[1].id) is sinks[1] and reg.get(sinks[-1].id) is sinks[-1]
    assert reg.open_sinks() == []


def test_no_chatter_is_never_due() -> None:
    room = Room(id=1, name="#build", created_at=0.0, created_by="alice", paused=False, paused_reason=None,
                budget_per_hour=60, budget_remaining=60, budget_window_start=0.0, budget_notice_window=None,
                hop_count=0, hop_limit=6, last_msg_at=None)  # a quiet room
    assert rules.chatter_due(room, [], now=1000.0, quiet_s=0.0, max_hold_s=0.0) is False


def test_mentions_are_active_names_once_each_lower_cased() -> None:
    text = "@Bob and @bob, @carol: see @nobody, mail@carol.dev and @bob-2"
    assert rules.parse_mentions(text, ["bob", "Carol", "bob-2"]) == ["bob", "carol", "bob-2"]
    assert rules.parse_mentions("", ["bob"]) == []
    assert rules.parse_mentions("@all hands", ["bob"]) == []  # there is no @all
