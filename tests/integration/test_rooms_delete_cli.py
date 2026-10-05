"""``switchboard rooms delete`` in a child process against a real broker process
(DESIGN.md §28.6; spec #16 §14 item 17)."""

from __future__ import annotations

import re
from pathlib import Path

from conftest import TEST_HUMAN, SubprocBroker
from fakes.fake_agent import FakeAgent

from switchboard import db
from switchboard.clock import SystemClock
from switchboard.paths import Paths
from switchboard.store import Store


def seed(home: Path, *names: str) -> None:
    """Rooms made in the database before the broker starts (no human command needed)."""
    paths = Paths.from_home(home)
    paths.ensure()
    con = db.open_db(paths.db)
    try:
        st = Store(con, SystemClock())
        for n in names:
            st.create_room(n, TEST_HUMAN, 60, 30)
    finally:
        con.close()


def backups(home: Path) -> list[Path]:
    return sorted(Paths.from_home(home).db.parent.glob("*.delete-*.bak"))


async def test_refused_with_a_member_then_close_and_delete(tmp_home: Path) -> None:
    seed(tmp_home, "#build")
    b = SubprocBroker(tmp_home).start()
    try:
        async with FakeAgent(tmp_home, "ka") as a:
            assert (await a.join("#build", "alpha"))["ok"]
            r = b.cli("rooms", "delete", "#build", "--yes")
            assert r.returncode == 1 and "close it first" in r.stderr, (r.stdout, r.stderr)
            assert "switchboard: conflict: #build has 1 agent(s) (alpha)" in r.stderr
            assert backups(tmp_home) == []
            r = b.cli("cmd", "#build", "/close")
            assert r.returncode == 0, r.stderr
        r = b.cli("rooms", "delete", "#build", "--yes")
        assert r.returncode == 0, (r.stdout, r.stderr)
        [bak] = backups(tmp_home)
        assert bak.name == "switchboard.db.delete-build-1.bak"
        lines = r.stdout.splitlines()
        assert lines[0] == "switchboard rooms delete #build~closed-1:"
        assert re.fullmatch(
            r"  #build~closed-1: was #build, closed \d{4}-\d\d-\d\d \d\d:\d\d by alice", lines[1]
        )
        counts = re.fullmatch(
            r"  removes (1 room, \d+ message\(s\), 1 membership\(s\), \d+ delivery row\(s\),"
            r" \d+ batch\(es\), \d+ event\(s\))",
            lines[2],
        )
        assert counts, lines[2]
        assert lines[3] == f"  a checked backup of the whole database is written first: {bak}"
        assert lines[4] == "  this can't be undone, except by restoring that backup"
        assert lines[5] == f"deleted #build~closed-1: {counts.group(1)}"
        assert lines[6] == (
            f"backup: {bak} (0600, checked); it still holds the room: remove it once you no longer need it"
        )
        assert len(lines) == 7
        r = b.cli("rooms", "--closed")
        assert r.returncode == 0 and r.stdout.strip() == "no closed rooms"
    finally:
        b.kill()


def test_without_yes_nothing_is_applied(tmp_home: Path) -> None:
    seed(tmp_home, "#scratch")
    b = SubprocBroker(tmp_home).start()
    try:
        r = b.cli("rooms", "delete", "#scratch")  # stdin is not a terminal
        assert r.returncode == 1 and r.stdout.splitlines()[-1] == "not applied", (r.stdout, r.stderr)
        assert re.fullmatch(
            r"  #scratch: open, no agents, created \d{4}-\d\d-\d\d \d\d:\d\d", r.stdout.splitlines()[1]
        )
        assert backups(tmp_home) == []
        r = b.cli("rooms")
        assert r.returncode == 0 and "#scratch" in r.stdout
    finally:
        b.kill()


def test_forbidden_without_a_terminal_and_when_the_broker_is_down(tmp_home: Path) -> None:
    seed(tmp_home, "#scratch")
    b = SubprocBroker(tmp_home, trust=False).start()
    try:
        r = b.cli("rooms", "delete", "#scratch", "--yes")
        assert r.returncode == 1 and "forbidden" in r.stderr and "room.delete" in r.stderr, (
            r.stdout,
            r.stderr,
        )
        assert backups(tmp_home) == []
        r = b.cli("rooms")
        assert r.returncode == 0 and "#scratch" in r.stdout
    finally:
        b.kill()
    r = b.cli("rooms", "delete", "#scratch", "--yes")
    assert r.returncode == 3 and "the broker is not running" in r.stderr
