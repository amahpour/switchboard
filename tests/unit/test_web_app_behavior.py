"""The web UI's room bookkeeping, run for real: app.js in node against a fake DOM, fetch and
WebSocket (tests/web_app_harness.js). Closing, reopening, re-creating and deleting rooms
(DESIGN.md §28.4) must never leave a tab unsubscribed, stale or silent, nor lose the
/close reply. Skipped where node is missing (the CI runners have it)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

HARNESS = Path(__file__).resolve().parents[1] / "web_app_harness.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

CLOSE_REPLY = ("closed #build: 2 agent(s) removed (1 on fpga-pi); history kept."
               " The name is free again; reopen this room from Closed rooms in the web UI")
GONE = "*** #build is no longer open (closed or deleted); Closed rooms can reopen a closed room"


def run(scenario: str) -> dict[str, Any]:
    assert NODE is not None
    r = subprocess.run([NODE, str(HARNESS), scenario], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def texts(out: dict[str, Any]) -> list[str]:
    """The log lines without their [hh:mm:ss] stamp."""
    return [line.split("] ", 1)[1] if line.startswith("[") else line for line in out["log"]]


def hello_rooms(out: dict[str, Any]) -> list[list[str]]:
    return [h["rooms"] for h in out["hellos"]]


def test_every_rooms_frame_subscribes_the_tabs_again() -> None:
    # a close dropped this page's subscription; the reopen (same id) landed before the listing
    out = run("rooms_frame_hellos_again")
    assert out["tabs"] == ["#build"] and hello_rooms(out) == [["#build"]]


def test_a_failed_resync_on_open_still_subscribes_the_tabs() -> None:
    out = run("resync_fails_on_open")
    assert out["tabs"] == ["#build"] and hello_rooms(out) == [["#build"]]


@pytest.mark.parametrize("scenario", ["close_reply_frame_late", "close_reply_frame_first", "close_reply_last_room"])
def test_the_close_reply_stays_on_screen(scenario: str) -> None:
    out = run(scenario)
    assert not out["logHidden"]
    assert texts(out) == [GONE, "/close" + CLOSE_REPLY]
    assert out["tabs"] == ([] if scenario == "close_reply_last_room" else ["#ops"])


@pytest.mark.parametrize("scenario", ["replaced_active_room", "reused_id_room"])
def test_a_replaced_active_room_is_pruned_with_a_notice(scenario: str) -> None:
    # closed and re-created (another id), or deleted and re-created with its id reused
    # (another created_at): the old history goes, the notice says so, and the new room is
    # read from its start (no `after`)
    out = run(scenario)
    assert texts(out) == [GONE]
    assert out["tabs"] == ["#build"] and out["title"] == "switchboard — #build"
    assert out["hellos"] == [{"t": "hello", "rooms": ["#build"], "after": {}}]
