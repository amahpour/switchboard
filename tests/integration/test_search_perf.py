"""How long a search takes on a large database (#724, DESIGN.md §58.8).

The route runs on the broker's event loop, so a slow search stalls every page and agent. This plants 100,000
chat lines in 100 rooms (a quarter of them in private rooms the person is not in), then runs real searches
through ``broker.search.run``, which is the route's whole body: the rooms the person can see, the safe query,
the index and the snippets. The bound is relative to the machine: a search must take less than a share of the
time a plain read of every line's text takes to find one word in Python, so a slow runner does not
fail it and an index
that is not used (a scan, a filter applied after the count) does."""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from conftest import InProcBroker

from switchboard.broker import search as search_api

ROOMS, PER_ROOM = 100, 1000  # 100,000 lines
MAX_SHARE = 0.5
RARE, COMMON = "zebrafish", "upload"


def seed(b: InProcBroker) -> None:
    store = b.state.store
    store.con.execute("BEGIN")
    for r in range(ROOMS):
        room = store.create_room(f"#room-{r:03d}", "alice", 60, 0)
        if r % 4 == 3:  # private rooms that bob is not in
            store.con.execute("UPDATE rooms SET private=1 WHERE id=?", (room.id,))
        rows = []
        for i in range(PER_ROOM):
            text = f"line {i} about the {COMMON} limit and the old endpoint number {r * PER_ROOM + i}"
            if i == 500 and r % 10 == 0:
                text += f" {RARE}"
            rows.append((room.id, 1.0 + i, "claude-1", "agent", "mcp", "chat", text, "[]"))
        store.con.executemany(
            "INSERT INTO messages(room_id, ts, sender_name, sender_kind, via, kind, text, mentions)"
            " VALUES(?,?,?,?,?,?,?,?)",
            rows,
        )
    store.con.execute("COMMIT")


def test_searching_100k_lines_stays_fast(tmp_home: Path) -> None:
    """The median of a rare-word search, a common-word first page and a phrase is each under ``MAX_SHARE``
    of the time to read every line, and the answers are real: a first page of 50 with a count, only the
    visible rooms.
    Fails if the search scans the messages instead of using the index."""
    b = InProcBroker(tmp_home).start()
    try:
        # planting 100,000 lines through the index's triggers took more than the default 10 s on a slow runner
        b.on_loop(seed, b, timeout=300)
        store = b.state.store
        viewer = 7  # a person with no private rooms: three quarters of the lines are theirs

        def run(q: str) -> tuple[float, dict[str, object]]:
            t = time.perf_counter()
            out = search_api.run(b.state, viewer, q=q)
            return time.perf_counter() - t, out

        def scan() -> float:
            t = time.perf_counter()
            sum(1 for (text,) in store.con.execute("SELECT text FROM messages") if RARE in text)
            return time.perf_counter() - t

        scan_ms = statistics.median(b.on_loop(scan) for _ in range(5)) * 1000
        for q, check in (
            (RARE, lambda o: o["total"] == 10 - len([r for r in range(0, ROOMS, 10) if r % 4 == 3])),
            (
                COMMON + " limit",
                lambda o: (
                    len(o["hits"]) == 50 and o["total"] == 1000 and o["capped"]  # type: ignore[arg-type]
                ),
            ),
            (f'"the {COMMON} limit"', lambda o: len(o["hits"]) == 50),  # type: ignore[arg-type]
        ):
            runs = [b.on_loop(run, q) for _ in range(7)]
            assert check(runs[-1][1]), runs[-1][1]["total"]  # type: ignore[index]
            ms = statistics.median(t for t, _ in runs) * 1000
            print(f"search {q!r}: median {ms:.1f} ms; reading every line once {scan_ms:.1f} ms")
            assert ms < MAX_SHARE * scan_ms, (
                f"search {q!r} took {ms:.0f} ms, over {MAX_SHARE:.0%} of the {scan_ms:.0f} ms to read all"
            )
    finally:
        b.stop()
