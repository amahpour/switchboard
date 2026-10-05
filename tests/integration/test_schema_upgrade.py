"""A 0.2.0 home under the new broker (DESIGN.md §27.6): ``switchboard start`` migrates
its database once, after a verified 0600 backup, and the rooms and history carry on."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from conftest import SubprocBroker

from switchboard.mcp.client import call_sync
from switchboard.paths import Paths

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_2_0.sql"


def dump(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return list(con.iterdump())
    finally:
        con.close()


def test_a_0_2_0_home_starts_after_a_verified_backup(tmp_home: Path) -> None:
    paths = Paths.from_home(tmp_home)
    paths.ensure()
    con = sqlite3.connect(paths.db)
    con.executescript(FIXTURE.read_text(encoding="utf-8"))
    con.close()
    os.chmod(paths.db, 0o600)
    original = dump(paths.db)

    b = SubprocBroker(tmp_home).start()
    try:
        bak = tmp_home / "switchboard.db.v1.bak"
        assert (os.stat(bak).st_mode & 0o777) == 0o600
        assert dump(bak) == original
        rooms = {r["name"] for r in call_sync(paths.sock, "room.list", {}, 5)["rooms"]}
        assert rooms == {"#build", "#review"}
        hist = call_sync(paths.sock, "room.history", {"room": "#build"}, 5)["messages"]
        texts = [m["text"] for m in hist]
        assert "@vivado build blinky and hand it to @bot-a" in texts and "thanks all" in texts
        said = call_sync(paths.sock, "human.say", {"room": "#build", "text": "after the upgrade"}, 5)
        assert said["id"] > max(m["id"] for m in hist)
        # a second start finds schema 2: no second backup
        b.kill()
        b = SubprocBroker(tmp_home).start()
        assert sorted(p.name for p in tmp_home.glob("switchboard.db.v1.bak*")) == ["switchboard.db.v1.bak"]
        after = call_sync(paths.sock, "room.history", {"room": "#build"}, 5)["messages"]
        assert after[-1]["text"] == "after the upgrade"
    finally:
        b.kill()
    assert dump(tmp_home / "switchboard.db.v1.bak") == original  # the backup is never touched again
