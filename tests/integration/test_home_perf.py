"""How long the home page's data takes to build (#649, DESIGN.md §55.5). Every open tab asks for it again
after a
frame, and the build runs on the broker's event loop inside an async route, so a slow build is a stall for
every page and every agent. This builds the setup the issue measured, one person with 50 rooms of 300 messages
each, and fails if the median is past a bound well above what it takes now. It fails on the code before #649
(a median of about 260 ms here). The bound is generous so a slow runner does not fail it; a real slowdown
does."""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from conftest import InProcBroker

from switchboard.broker import home
from switchboard.models import session_key

ROOMS, MESSAGES = 50, 300
BOUND_MS = 100  # about four times the 23 ms median measured when this was written (see the pull request)


def seed(b: InProcBroker) -> None:
    """50 rooms, three agents in each, 300 chat messages in each (one in ten names alice), and a review board
    with five open questions in every fifth room. Nobody reads, so every agent has its messages pending."""
    store = b.state.store
    for r in range(ROOMS):
        room = store.create_room(f"#room-{r:02d}", "alice", 60, 0)
        members = []
        for n in ("claude-1", "codex-1", "devin-1"):
            p = store.upsert_participant("test", session_key("test", "", f"{n}-{r}"), host="")
            members.append(store.create_membership(room.id, p.id, n))
        for i in range(MESSAGES):
            a = members[i % 3]
            store.insert_message(
                room.id,
                sender_name=a.screen_name,
                sender_kind="agent",
                via="mcp",
                text=f"message {i} about the upload limit and the old endpoint\nsecond line",
                sender_membership_id=a.id,
                sender_harness="claude",
                mentions=["alice"] if i % 10 == 0 else [],
            )
        if r % 5 == 0:
            rv = store.open_review(room.id, f"https://example.com/shop/pull/{r}", "", "claude-1")
            for k in range(5):
                store.add_review_item(
                    rv["id"], "question", "open", title=f"Q{k}", raised_by="claude-1", options=["a", "b"]
                )


def test_building_home_for_50_rooms_of_300_messages_stays_fast(tmp_home: Path) -> None:
    """The median of ten builds is under ``BOUND_MS``, and what is built is the page's real answer: a question
    card and a board card in the rooms that have them, not an empty page that is quick because it is empty."""
    b = InProcBroker(tmp_home).start()
    try:
        b.on_loop(seed, b)

        def once() -> tuple[float, dict[str, object]]:
            t = time.perf_counter()
            out = home.build(b.state, "alice", None, True)
            return time.perf_counter() - t, out

        runs = [b.on_loop(once) for _ in range(10)]
        out = runs[-1][1]
        assert out["waiting_total"] == ROOMS + ROOMS // 5  # a question in every room, a board in every fifth
        assert len(out["live"]) == 0 and len(out["done"]) > 0  # type: ignore[arg-type]
        median_ms = statistics.median(t for t, _ in runs) * 1000
        print(f"home.build median {median_ms:.1f} ms over {len(runs)} builds")
        assert median_ms < BOUND_MS, f"home.build took {median_ms:.0f} ms (bound {BOUND_MS} ms)"
    finally:
        b.stop()
