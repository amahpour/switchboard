"""Idle-wake latency to a Claude session on a remote host, over the exec link (DESIGN.md
§27.4.9, M8d). Marker ``perf`` (deselected by default; ``uv run pytest -m perf
tests/integration/test_remote_perf.py``).

Each round: the human posts; the broker routes on the relayed registry view and pushes
``deliver`` with ``chk``; the satellite re-reads the Pi registry and relays it; the Pi
MCP server posts it into the Pi session's (fake) inbox; the test plays Claude and fires
UserPromptSubmit with the frame's body through the Pi hook and the satellite. The
latency is the confirmed batch's ``turn_start_at`` (the hook's start, rebased to the
broker's clock) minus the message's time: p50 under 150 ms over 20 rounds.
"""

from __future__ import annotations

import sqlite3
import statistics
import time
from typing import Any

import pytest
from fakes.fake_claude import FakeClaude, fixture
from fakes.fake_link import FakeLink, wait_for

from switchboard.config import Config

ROUNDS = 20
P50_MAX_S = 0.150
FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)


def q(link: FakeLink, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{link.desk_paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def status(link: FakeLink) -> str:
    return q(link, "SELECT status FROM participants WHERE harness='claude'")[0][0]


def relayed_idle(link: FakeLink) -> bool:
    ad = link.broker.state.engine.adapters["claude"]
    return any(k[0] == link.name and v.status == "idle" for k, v in ad.registry.items())


@pytest.mark.perf
def test_idle_wake_over_exec_link() -> None:
    link = FakeLink(kind="inproc", broker_cfg=FAST)
    fc = None
    try:
        link.start()
        fc = FakeClaude(None, inbox=True, home=link.pi, sessions_dir=link.pi_sessions)
        assert fc.tool("join", room="#fpga", screen_name="bench")["tier"] == "claude:inbox"
        fc.hook(fixture("Stop"))
        wait_for(lambda: status(link) == "idle" and relayed_idle(link), what="idle")
        turn: list[float] = []
        frame: list[float] = []
        for i in range(ROUNDS):
            mid = link.call("human.say", {"room": "#fpga", "text": f"round {i}: build and flash"})["id"]
            got = fc.inbox.wait_frames(i + 1, timeout=10)
            assert len(got) == i + 1, f"round {i}: no frame"
            conn, f = got[i]
            fc.hook(fixture("UserPromptSubmit", prompt=f["message"]["content"]))  # the turn starts
            b = wait_for(
                lambda: (
                    (x := q(link, "SELECT * FROM batches WHERE path='inbox' ORDER BY id")[-1])["state"]
                    == "confirmed"
                    and x
                ),
                what="confirmed",
            )
            [m] = q(link, "SELECT ts FROM messages WHERE id=?", mid)
            turn.append(b["turn_start_at"] - m["ts"])
            frame.append(conn.t_accept - m["ts"])
            assert fc.tool("pass", room="#fpga")["ok"]
            fc.hook(fixture("Stop"))
            wait_for(lambda: status(link) == "idle", what="idle")
            time.sleep(0.05)
        p50 = statistics.median(turn)
        print(
            f"remote idle wake over the exec link, n={ROUNDS}: message -> inbox frame p50"
            f" {statistics.median(frame) * 1000:.1f} ms; -> turn start p50 {p50 * 1000:.1f} ms,"
            f" max {max(turn) * 1000:.1f} ms"
        )
        assert p50 < P50_MAX_S, sorted(turn)
    finally:
        if fc is not None:
            fc.close()
        link.close()
