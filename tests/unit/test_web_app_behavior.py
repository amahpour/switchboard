"""The web UI, run for real: md.js and app.js in node against a fake DOM, fetch and WebSocket
(tests/web_app_harness.js). Closing, reopening, re-creating and deleting rooms (DESIGN.md
§28.4) must never leave a room unsubscribed, stale or silent, nor lose the /close reply.
The native UI (#19, DESIGN.md §29): Markdown bodies with inert links, grouped rows, warn
rows, the Members flags, the header chips, the Inspector, the palette and @mentions, and
the first-run form. Skipped where node is missing, except under SWITCHBOARD_REQUIRE_NODE=1
(CI), where that fails."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import node_for_tests

HARNESS = Path(__file__).resolve().parents[1] / "web_app_harness.js"
NODE, pytestmark = node_for_tests()  # a skip without node; a failure under SWITCHBOARD_REQUIRE_NODE=1

CLOSE_REPLY = (
    "closed #build: 2 agent(s) removed (1 on fpga-pi); history kept."
    " The name is free again; reopen this room from Closed rooms in the web UI"
)
GONE = "#build is no longer open (closed or deleted); Closed rooms can reopen a closed room"


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


@pytest.mark.parametrize(
    "scenario", ["close_reply_frame_late", "close_reply_frame_first", "close_reply_last_room"]
)
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


# ------------------------------------------------------------------ the native UI (#19)


def classes(row: dict[str, Any]) -> set[str]:
    return set(row["cls"].split())


def chat_rows(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in out["rows"] if "k-chat" in classes(r)]


def test_markdown_renders_and_raw_html_and_bad_links_stay_inert() -> None:
    out = run("markdown_in_log")
    assert out["strong"] == ["hi"]
    # the non-http(s) link is text with a "link blocked" pill: no anchor, no href anywhere
    assert out["anchors"] == 0 and out["hrefs"] == 0 and out["blocked"] == 1
    assert len(out["bodyText"]) == 1 and "<b>x</b>" in out["bodyText"][0]


def test_a_quick_second_message_is_a_continuation_row() -> None:
    rows = chat_rows(run("grouped_continuation"))
    assert [("cont" in classes(r)) for r in rows] == [False, True, False, False]
    assert rows[1]["text"].endswith("second") and "claude-1" not in rows[1]["text"]  # no repeated header
    assert "has-reply" in classes(rows[3]) and "first" in rows[3]["text"]  # the parent's first line


def test_a_warn_notice_is_a_red_alert_row() -> None:
    rows = [r for r in run("warn_notice")["rows"] if "k-notice" in classes(r)]
    assert "warn" in classes(rows[0]) and rows[0]["role"] == "alert"
    assert rows[0]["text"] == "loop guard: 30 agent messages in a row; #build paused"  # no *** prefix
    assert "warn" not in classes(rows[1]) and rows[1]["role"] == "note"


def test_the_members_list_keeps_every_flag() -> None:
    out = run("member_flags")
    b = out["buddies"]
    assert "Approvals off" in b and "Parked — needs a poke" in b and "its turn ended without wait()" in b
    assert "Approvals off: acts without asking" in b and "what it reads can steer it" not in b
    assert "@fpga-pi" in b and "claude:inbox" in b and "codex:daemon" in b and "devin:wait-loop" in b
    assert "2 queued" in b and "1 in flight" in b
    assert out["agentsTitle"] == "Agents (4)" and out["roomSub"] == "alice and 4 agents"
    assert not out["approvalsHidden"] and out["approvals"] == "Approvals off: codex-1"
    assert out["alert"] == "2"  # codex-1 (approvals off) and devin-1 (parked)
    assert out["roomEmptyHidden"]


def test_unknown_approval_mode_says_the_agent_may_act_without_asking() -> None:
    out = run("unknown_approval_mode")
    assert "Approval mode unknown: may act without asking" in out["row"]
    assert out["flagTitle"] == "approval mode unknown: may act without asking"
    assert out["chipTitle"] == "approval mode unknown: may act without asking"
    assert "It may run commands and edit files without asking" in out["inspector"]


def test_the_status_chips_show_paused_and_the_loop_guard_off() -> None:
    out = run("status_chips")
    f = out["first"]
    assert f["state"] == "Paused" and f["stateBad"]
    assert f["hops"] == "loop guard off ⚠" and f["hopsBad"]
    assert f["budget"] == "Budget0/60" and f["budgetBad"]
    assert not f["pausedHidden"] and "#build is paused (loop guard)" in f["paused"]
    assert f["pauseLabel"] == "Resume room"
    s = out["second"]
    assert s["state"] == "Running" and s["hops"] == "Hops3/30" and not s["hopsBad"]
    assert s["budget"] == "Budget47/60" and s["fill"] == "meter-fill w8"
    assert s["pausedHidden"] and s["pauseLabel"] == "Pause room"


def test_an_empty_room_shows_how_to_join() -> None:
    out = run("room_empty_hint")
    assert out["before"] == {"hidden": False, "line": "join switchboard room #build"}
    assert out["after"] is True


def test_the_inspector_shows_the_member_detail_and_closes_when_it_leaves() -> None:
    out = run("inspector")
    # at once, from member_dict; the detail is still loading
    assert (
        out["early"]["inspecting"] and "codex-1" in out["early"]["body"] and "loading" in out["early"]["body"]
    )
    assert "Approvals off" in out["early"]["body"] and out["sel"] == ["codex-1"]
    # codex-1's late answer (held) never replaced devin-1's view
    assert out["devin"].startswith("DVdevin-1") and "a test session" in out["devin"]
    assert (
        "read #build and go back to wait()" in out["devin"] and "codex" not in out["devin"].split("Catch")[0]
    )
    c = out["codex"]["body"]
    assert "It runs commands and edits files without asking" in c
    assert "anything it reads (tool output, web pages, room messages) can make it act on its own" in c
    assert out["codex"]["pos"] == "Agent 2 of 4"
    assert (
        "3f2a…c91e" in c
        and "said" in c
        and "please review parse_port" in c
        and "message #999 (not loaded)" in c
    )
    assert "Turn start inbox: 2 from claude-1, bench@fpga-pi" in c and "Passed called pass()" in c
    # commands take bare names; the label keeps the host
    assert "/catchup codex-1 on bench" in c and "bench@fpga-pi’s work" in c and "bench@fpga-pi on" not in c
    # it left: back to Members, and the log says so
    assert out["after"] == {"inspecting": False, "membersOff": False}
    assert texts(out)[-1] == "codex-1 left #build"


def test_the_inspector_actions_use_the_command_path() -> None:
    out = run("inspector_actions")
    assert out["commands"] == ["/hold claude-1", "/kick claude-1"]
    assert out["confirmShown"]
    assert not out["inspecting"]
    assert texts(out) == ["/holdok", "/kickok"]


def test_a_catchup_entry_keeps_the_draft_and_gives_it_back_after_the_command() -> None:
    out = run("catchup_keeps_the_draft")
    assert out["filled"] == "/catchup claude-1"
    assert out["refilled"] == '/catchup claude-1 on ""'
    assert out["commands"] == ['/catchup claude-1 on ""']
    assert out["afterSend"] == "half-written note to codex-1"  # the draft is back
    assert out["said"] == ["hello"] and out["afterPlain"] == ""
    assert "Your draft is kept: it comes back after this command is sent (or press Esc)." in texts(out)


def test_a_typed_kick_asks_first_and_declining_gives_the_text_back() -> None:
    out = run("kick_typed_declined")
    assert out["commands"] == [] and out["input"] == "/kick codex-1"
    assert out["title"] == "Kick codex-1 from #build?" and out["action"] == "Kick"


def test_the_palette_and_mentions_complete_without_sending() -> None:
    out = run("palette_and_mentions")
    assert not out["all"]["hidden"] and len(out["all"]["items"]) == 12
    assert out["ho"] == ["/hops", "/hold"] and out["active"] == "pal-hold"
    assert out["completed"] == {"value": "/hold ", "hidden": True}
    assert out["slashText"]  # //text never opens the palette
    assert out["men"] == ["codex-1"] and out["mention"] == {"value": "hi @codex-1 ", "hidden": True}
    assert out["esc"] == {"value": "/wh", "hidden": True}
    assert out["commands"] == [] and out["said"] == []


def test_an_argument_less_palette_command_runs() -> None:
    out = run("palette_runs")
    assert out["commands"] == ["/help"] and texts(out) == ["/helphelp text"]


def test_the_welcome_form_creates_the_named_room() -> None:
    out = run("welcome_create")
    assert out["shown"] and out["label"] == "Create #ops" and out["preview"] == "join switchboard room #ops"
    assert out["posted"] == "#ops" and out["tabs"] == ["#ops"]
    assert out["placeholder"] == "Message #ops — @ to mention, / for commands"
