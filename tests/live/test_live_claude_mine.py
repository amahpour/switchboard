"""Live ``/meeting <link> mine``: a real Claude Code hosts a real call for its person (DESIGN.md §54, #623).

Opt-in with the Claude switch and a Zoom link in a private file::

    SWITCHBOARD_LIVE=claude SWITCHBOARD_MEETING_URL_FILE=<0600 file> \\
      uv run pytest -m live tests/live/test_live_claude_mine.py -s -q

The ``meeting-self-test`` skill makes the throwaway Zoom meeting and writes the file; nobody needs to be on
the call. One real ``claude --model haiku`` session, signed in through the browser, with the plugin from
this checkout and a throwaway broker (the host run's ``HostRun``). Then phase 5's handoff runs for real:

1. The person types ``/meeting <link> mine``. The broker holds the link in memory for that person and room
   and wakes only their own agent with a message that has no link in it.
2. The model runs the plugin's ``host-meeting-bot`` skill with ``--from-room`` and an empty stdin. The script
   registers the call and gets the link back once, privately, in the registration's answer.
3. The relay and joinly join the real call, and the hosted row goes live as a Zoom call from this machine.
4. The meeting's id is in no Claude Code transcript, database file, room message or relay log.
5. End call makes the relay leave and exit 0, and ``meeting-host stop`` removes what is left.

It costs a few cents and about a minute. It needs what the host run needs (Docker, the joinly image, a
headless Chromium) and a Zoom link: the leak check looks for the meeting's id, which only a Zoom link names.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from harness import relay, scenarios
from harness.claude import plugin_dir
from harness.world import wait_for
from test_live_claude_host import HostRun, hosted_row  # skips this module too when Claude isn't switched on

pytestmark = pytest.mark.live

_link_file = Path(os.environ.get("SWITCHBOARD_MEETING_URL_FILE", "/nonexistent"))
if not _link_file.is_file() or _link_file.stat().st_mode & 0o077:
    pytest.skip(
        "needs SWITCHBOARD_MEETING_URL_FILE: a Zoom link in a 0600 file (the meeting-self-test skill)",
        allow_module_level=True,
    )
LINK = _link_file.read_text().strip()
_id = re.search(r"zoom\.us/j/(\d{9,12})\b", LINK)
if _id is None:
    pytest.skip("SWITCHBOARD_MEETING_URL_FILE must hold a Zoom link", allow_module_level=True)
MEETING_ID = _id.group(1)


def scrub(text: str) -> str:
    """What the run may print: never the link or the meeting's id."""
    return text.replace(LINK, "<link>").replace(MEETING_ID, "<id>")


@pytest.fixture(scope="module")
def run() -> Iterator[HostRun]:
    r = HostRun("claude-3")
    try:
        r.start()
        yield r
    finally:
        r.stop()
    assert r.results["drift_fail"] == [], f"the user's own Claude config changed: {r.results['drift_fail']}"
    assert r.results["leftover_processes"] == [], "processes of the run outlived it"
    assert r.new_projects() == [], "a hosted call's containers outlived the run"


def project_of(r: HostRun) -> str:
    row = hosted_row(r)
    assert row is not None, "no hosted call was registered"
    return f"switchboard-call-{str(row['key_hash'])[:8]}"


def test_1_sign_in_through_the_browser_then_join(run: HostRun) -> None:
    """No token anywhere: the session signs in, joins the room and speaks as ``claude@<device>``."""
    scenarios.join_and_say(run)


def test_2_mine_hands_the_call_to_the_persons_own_agent(run: HostRun) -> None:
    """The person types the command and does nothing else; their agent hosts the call from the room's message,
    and the call goes live as Zoom from this machine. Fails if the message doesn't wake the agent, if the
    model passes a link itself, or if the script doesn't get the link from its registration."""
    run.wait_settled()
    relay.seed_fixes(run.data_dir() / "joinly-fixes")
    reply = run.broker.command(f"/meeting {LINK} mine")
    assert "asked your own agent" in reply["text"], scrub(str(reply))
    row = run.wait(
        lambda: (r := hosted_row(run)) is not None and r["state"] == "live" and r, 360, "the call to go live"
    )
    assert row["platform"] == "zoom" and row["host"] == run.device, row
    start = next((c for c in run.host_commands() if " start " in c), "")
    assert "--from-room" in start and "</dev/null" in start.replace(" ", ""), scrub(start)
    assert MEETING_ID not in start, "the model passed the link itself"
    log = relay.logs(project_of(run))
    assert "in the call" in log, scrub(log)
    scenarios.record(
        run,
        "mine",
        state=row["state"],
        platform=row["platform"],
        command=scrub(re.sub(re.escape(str(Path(run.base))), "<run>", start))[:400],
    )


def test_3_the_link_reached_no_model_message_database_or_log(run: HostRun) -> None:
    """The link went from the person's command to the script and nowhere else. Fails naming each place the
    meeting's id was found, never quoting it."""
    needle = MEETING_ID.encode()
    leaks = [f"Claude Code's {f.name}" for f in (run.claude_home / "projects").rglob("*") if f.is_file()
             and needle in f.read_bytes()]  # fmt: skip
    db = Path(run.broker.paths.db)
    leaks += [f"the database's {f.name}" for f in db.parent.glob(db.name + "*") if needle in f.read_bytes()]
    if any(MEETING_ID in str(m["text"]) for m in run.broker.q("SELECT text FROM messages")):
        leaks.append("a room message")
    if MEETING_ID in relay.logs(project_of(run)):
        leaks.append("the relay's log")
    assert leaks == [], f"the meeting's id is in: {', '.join(leaks)}"


def test_4_end_call_then_stop_removes_everything(run: HostRun) -> None:
    """End call makes the relay leave the real call and exit 0, joinly ends with it, and ``stop`` removes the
    rest. Fails if a container survives either."""
    project = project_of(run)
    reply = run.broker.command("/meeting leave")
    assert "leaving" in reply["text"], reply
    wait_for(
        lambda: not relay.running(project),
        90,
        "both containers to stop",
        on_fail=lambda: scrub(relay.logs(project)),
    )
    cs = {c["service"]: c for c in relay.projects()[project]}
    assert cs["relay"]["exit"] == "0", scrub(relay.logs(project))
    assert cs["joinly"]["state"] == "exited"
    done = subprocess.run(
        ["sh", str(plugin_dir() / "bin" / "meeting-host"), "stop", "--data", str(run.data_dir())],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": run.path, "HOME": str(run.base), "LANG": "C.UTF-8"},
    )
    assert done.returncode == 0, done.stderr
    assert run.new_projects() == []
    scenarios.record(run, "end_call", relay_exit=cs["relay"]["exit"], joinly_state=cs["joinly"]["state"])
