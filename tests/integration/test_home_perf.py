"""How long the home page's data takes to build (#649, DESIGN.md §55.5.1).

Every open tab asks for it again after a frame, and the build runs on the broker's event loop inside an
async route, so a slow build is a stall for every page and every agent. This builds the setup the issue
measured, one person with 50 rooms of 300 messages each, and fails if the best build is past a bound that the
code before #649 (about 260 ms here) was over. The bound is relative to the machine, so a slow runner does
not fail it and a real slowdown does.
"""

from __future__ import annotations

import time
from pathlib import Path

from conftest import InProcBroker

from switchboard import db
from switchboard.broker import home
from switchboard.models import session_key
from switchboard.store import Store

ROOMS, MESSAGES = 50, 300
# A build must take less than this share of the time it takes to read the same 15,000 messages into
# ``Message`` objects, which the build did twice over before #649, and which is as slow or fast as the
# machine is. Measured when this was written: a build of 23 ms against 55 ms for the read here (0.42),
# 109 ms for the build on a slow macOS runner (where a fixed 100 ms bound failed), and about 4.7 before.
# Those were medians; a macOS runner later measured medians of 130 ms against 126 ms (1.03) and failed,
# which is why the best of interleaved rounds is compared now (#944).
MAX_SHARE = 1.0
# Rounds of a build then a read; the best of each is compared (#944).
ROUNDS = 15


def seed(b: InProcBroker) -> None:
    """50 rooms, three agents in each, 300 chat messages in each (one in ten names alice), and a review board
    with five open questions in every fifth room. Nobody reads, so every agent has its messages pending."""
    store = b.state.store
    # One transaction for all of it. Every store write opens its own ``db.tx`` and nested ones join the outer,
    # so without this the seed made about 16,000 commits, each a write to the WAL: 10 s was not enough for
    # that on a loaded runner (the flake), and with it the seed takes a fraction of a second.
    with db.tx(store.con):
        _plant(store)


def _plant(store: Store) -> None:
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
    """The best of 15 builds is under ``MAX_SHARE`` of the best of 15 reads of every message in the setup,
    each build followed by a read so that noise on a busy runner falls on both,
    and what is built is the page's real answer: a question card and a board card in the rooms that have
    them, not an empty page that is quick because it is empty."""
    b = InProcBroker(tmp_home).start()
    try:
        b.on_loop(seed, b, timeout=300)  # a backstop for a very slow runner; the seed is one transaction

        def build() -> tuple[float, dict[str, object]]:
            t = time.perf_counter()
            out = home.build(b.state, "alice", None, True)
            return time.perf_counter() - t, out

        def read_all() -> float:
            t = time.perf_counter()
            for room in b.state.store.list_rooms():
                b.state.store.history(room.id, None, MESSAGES)
            return time.perf_counter() - t

        # Build and read in turn, so a stretch of interference (another job, a throttled core) lands on both,
        # and compare the best of each: the fastest run is the cost with the least noise in it. A median of
        # ten builds against a median of five reads taken after them let one busy stretch decide (#944).
        builds: list[float] = []
        reads: list[float] = []
        out: dict[str, object] = {}
        for _ in range(ROUNDS):
            took, out = b.on_loop(build)
            builds.append(took)
            reads.append(b.on_loop(read_all))
        assert out["waiting_total"] == ROOMS + ROOMS // 5  # a question in every room, a board in every fifth
        assert len(out["live"]) == 0 and len(out["done"]) > 0  # type: ignore[arg-type]
        build_ms, read_ms = min(builds) * 1000, min(reads) * 1000
        print(
            f"home.build best {build_ms:.1f} ms; reading every message best {read_ms:.1f} ms "
            f"(ratio {build_ms / read_ms:.2f}, bound {MAX_SHARE:.2f}, best of {ROUNDS} interleaved)"
        )
        assert build_ms < MAX_SHARE * read_ms, (
            f"home.build took {build_ms:.0f} ms at best, over {MAX_SHARE:.0%} of the {read_ms:.0f} ms "
            "it takes at best to read every message in the rooms"
        )
    finally:
        b.stop()
