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


def test_only_a_confirmed_fix_gets_the_check_on_its_board_card() -> None:
    """#427: the check marks a fix that someone has confirmed (its raiser, or a person), so it means
    done. A fix still waiting for confirmation and every other state (raised, being fixed,
    contested, dropped, deferred, an open or answered question) have none, and a live frame that
    confirms a fix adds it to that card alone."""
    out = run("review_confirmed_check")
    assert out["cards"] == 10  # every card was drawn, so a missing check is the check's absence
    assert out["before"] == ["F1", "F2"]
    assert out["after"] == ["F1", "F2", "F3"]


def test_the_page_names_an_agents_owner_by_the_number_of_people() -> None:
    """#496: Members says "Owner: alice" under an agent only once the people frame names more than
    one person, and takes it away when the second leaves; a desktop broker, which sends no people
    frame, never shows it. The Inspector names the owner and the device whatever the number, and an
    agent whose owner the broker left null shows no owner line and no empty "Owner:". Fails if the
    line shows for one person, is not drawn when a second arrives, or is drawn for a null owner."""
    out = run("member_owner")
    assert out["desktop"] == [] and out["one"] == []
    assert out["two"] == ["Owner: alice", "Owner: bob"]  # devin-1 has none
    assert out["back"] == []
    assert out["inspector"] == {
        "alice": ["Owner: alice · device dev-0"],
        "bob": ["Owner: bob · device dev-1"],
        "none": [],
    }


def test_every_rooms_frame_subscribes_the_tabs_again() -> None:
    # a close dropped this page's subscription; the reopen (same id) landed before the listing
    out = run("rooms_frame_hellos_again")
    assert out["tabs"] == ["#build"] and hello_rooms(out) == [["#build"]]


def test_a_page_with_more_rooms_than_one_hello_may_name_says_hello_in_batches() -> None:
    """The broker refuses a hello that names more than 64 rooms (web.py's WS_MAX_ROOMS) and closes
    the socket, so a page with 65 rooms never stayed connected: it reconnected forever, and the
    e2e suite, whose one world holds a room per test when it runs serially, went red once it passed
    64. The page sends batches of at most 64, together naming every room once. Fails if one hello
    names them all."""
    out = run("hello_batches_many_rooms")
    assert out["sizes"] == [64, 6]
    assert out["all"] == out["names"]  # every room, once, in order


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


def test_room_badges_count_only_what_is_addressed_to_you() -> None:
    """Issue #109: the red badge, the rooms toggle badge and the title count only follow an
    @mention or a reply to your own message; other agent chatter gets a quiet dot instead, with
    no number, and opening the room clears both. Issue #138: an @humans counts too, from an agent
    or another person, but not alice's own."""
    out = run("room_badges_addressed_vs_quiet")

    quiet = out["quiet"]
    assert quiet["tab"]["badge"] is None
    assert quiet["tab"]["quietDot"] == {"label": "new activity"}
    assert quiet["title"] == "switchboard — #build"  # nothing addressed to alice: no count
    assert quiet["roomsBadge"] == {"text": "0", "hidden": True}

    mentioned = out["mentioned"]
    assert mentioned["tab"]["badge"] == {"text": "1", "label": "1 message for you"}
    assert mentioned["tab"]["quietDot"] is None  # the red badge replaces the quiet dot
    assert mentioned["title"] == "(1) switchboard — #build"
    assert mentioned["roomsBadge"] == {"text": "1", "hidden": False}

    replied = out["replied"]
    assert replied["tab"]["badge"] == {"text": "2", "label": "2 messages for you"}
    assert replied["title"] == "(2) switchboard — #build"
    assert replied["roomsBadge"] == {"text": "2", "hidden": False}

    # one agent replying to another agent's message is not "addressed to you"
    replied_other = out["repliedOther"]
    assert replied_other["tab"]["badge"] == {"text": "2", "label": "2 messages for you"}
    assert replied_other["roomsBadge"] == {"text": "2", "hidden": False}

    # an agent's own @humans counts too (issue #138), the same as an @mention
    humans = out["humans"]
    assert humans["tab"]["badge"] == {"text": "3", "label": "3 messages for you"}
    assert humans["title"] == "(3) switchboard — #build"
    assert humans["roomsBadge"] == {"text": "3", "hidden": False}

    # so does another person's @humans (bob's, on a hosted broker); alice's own doesn't
    assert out["personHumans"]["tab"]["badge"] == {"text": "4", "label": "4 messages for you"}
    assert out["personHumans"]["roomsBadge"] == {"text": "4", "hidden": False}
    assert out["ownHumans"]["tab"]["badge"] == {"text": "4", "label": "4 messages for you"}
    assert out["ownHumans"]["roomsBadge"] == {"text": "4", "hidden": False}

    opened = out["opened"]
    assert opened["tab"] == {"badge": None, "quietDot": None}
    assert opened["title"] == "switchboard — #ops"
    assert opened["roomsBadge"] == {"text": "0", "hidden": True}


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


def row_by_id(rows: list[dict[str, Any]], mid: str) -> dict[str, Any]:
    found = [r for r in rows if r["id"] == mid]
    assert found, (mid, rows)
    return found[0]


def test_focus_mode_collapses_agent_chat_not_addressed_to_you() -> None:
    """Issue #99: Focus collapses an agent chat message to one line unless it's addressed to
    alice (the same addressedToMe check behind the room badges, #109: an @mention or a reply to
    her own message). Her own messages never collapse, whether Focus is on or off. A click
    expands only the row clicked, and turning Focus off rebuilds every row back to normal.
    Issue #138: an agent's own @humans is addressed to her too, so it never collapses either."""
    out = run("focus_mode_collapsing")

    # Focus starts off: nothing is marked collapsible yet, even an agent message that would be
    before = out["before"]
    assert not any("collapsible" in classes(r) for r in before)

    on = out["on"]
    assert on["pressed"] == "true"
    rows = on["rows"]
    assert "collapsible" not in classes(row_by_id(rows, "9"))  # alice's own message
    assert "collapsible" not in classes(row_by_id(rows, "10"))  # a reply to alice: addressed
    assert "collapsible" not in classes(row_by_id(rows, "11"))  # an @alice mention: addressed
    assert "collapsible" in classes(row_by_id(rows, "12"))  # not addressed to alice: collapses
    assert "collapsible" in classes(row_by_id(rows, "13"))  # a reply to claude-1, not alice
    assert "collapsible" not in classes(row_by_id(rows, "14"))  # alice's own message
    assert "collapsible" not in classes(row_by_id(rows, "15"))  # claude-1's own @humans: addressed
    assert "mention" in classes(row_by_id(rows, "15"))
    assert "mention" not in classes(row_by_id(rows, "16"))  # alice's own @humans isn't to herself

    # the collapsed line reads "sender, first line of text", with no Markdown rendering
    assert out["expanded"]["summaryText"] == "devin-1nothing unusual here"

    # clicking the collapsed line expands only that row
    expanded_rows = out["expanded"]["rows"]
    assert "expanded" in classes(row_by_id(expanded_rows, "12"))
    assert "expanded" not in classes(row_by_id(expanded_rows, "13"))

    # Focus off rebuilds every row back to normal
    off = out["off"]
    assert off["pressed"] == "false"
    assert not any("collapsible" in classes(r) for r in off["rows"])


def test_a_warn_notice_is_a_red_alert_row() -> None:
    rows = [r for r in run("warn_notice")["rows"] if "k-notice" in classes(r)]
    assert "warn" in classes(rows[0]) and rows[0]["role"] == "alert"
    assert rows[0]["text"] == "loop guard: 30 agent messages in a row; #build paused"  # no *** prefix
    assert "warn" not in classes(rows[1]) and rows[1]["role"] == "note"


def test_the_members_list_keeps_every_flag() -> None:
    out = run("member_flags")
    b = out["buddies"]
    assert "Approvals off" not in b and "Parked — needs a poke" in b and "its turn ended without wait()" in b
    assert "@fpga-pi" in b and "claude:https" in b and "codex:https" in b and "devin:https" in b
    assert "2 queued" in b and "1 in flight" in b
    assert out["agentsTitle"] == "Agents (4)" and out["roomSub"] == "alice and 4 agents"
    assert not out["approvalsHidden"] and out["approvals"] == "Approvals off: codex-1"
    assert out["alert"] == "2"  # codex-1 (approvals off) and devin-1 (parked)
    assert out["roomEmptyHidden"]


def test_approvals_chip_counts_bypass_and_unknown_without_listing_many_names() -> None:
    out = run("approvals_chip_cases")
    assert out == {
        "one": "Approvals off: codex-1",
        "many": "Approvals off for 3 agents",
        "mixed": "4 agents may act without asking",
        "mixedTip": (
            "Approvals off: claude-1, codex-1; Unknown: devin-1, bench@fpga-pi."
            " Unknown agents may run commands and edit files without asking."
        ),
    }


def test_approval_flags_explain_the_risk_without_extra_member_rows() -> None:
    out = run("member_flag_tooltips")
    assert out["warnings"] == 0
    assert out["flags"] == [
        {
            "role": "button",
            "tabindex": "0",
            "label": "Approvals off: this agent can run commands and edit files without asking.",
        },
        {
            "role": "button",
            "tabindex": "0",
            "label": "Approval mode unknown: this agent may run commands and edit files without asking.",
        },
    ]


def test_unknown_approval_mode_says_the_agent_may_act_without_asking() -> None:
    out = run("unknown_approval_mode")
    assert "Approval mode unknown" not in out["row"]
    assert (
        out["flagLabel"]
        == "Approval mode unknown: this agent may run commands and edit files without asking."
    )
    assert "Unknown: codex-1" in out["chipTip"]
    assert "may run commands and edit files without asking" in out["chipTip"]
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
    assert not out["all"]["hidden"] and len(out["all"]["items"]) == 17
    assert "/rename" in out["all"]["items"]
    assert "/wake" in out["all"]["items"]  # wake an agent past the hold on chatter (#137)
    assert "/review" in out["all"]["items"]  # a person starts a review board (#248)
    assert out["ho"] == ["/hops", "/hold"] and out["active"] == "pal-hold"
    assert out["completed"] == {"value": "/hold ", "hidden": True}
    assert out["slashText"]  # //text never opens the palette
    assert out["men"] == ["codex-1"] and out["mention"] == {"value": "hi @codex-1 ", "hidden": True}
    assert out["esc"] == {"value": "/wh", "hidden": True}
    assert out["commands"] == [] and out["said"] == []


def test_mention_popover_matches_any_part_of_a_name() -> None:
    """Issue #112: @skill must find darius-skills-agent (a word prefix), not just a name that
    starts with "skill"; the match is case-insensitive; a prefix match ranks above a mid-name
    one even when the mid-name match is online and the prefix match isn't; and the matched part
    is its own node, not plain text baked into the name."""
    out = run("mention_match_anywhere")
    assert out["skill"] == {"options": ["men-darius-skills-agent"], "hits": ["skill"]}
    assert out["skillCased"] == ["men-darius-skills-agent"]  # "@Skill" behaves like "@skill"
    assert out["owner"] == ["men-mr-owner-2"]  # another word prefix, split on "-"
    assert out["substring"] == ["men-eval-reviewer-resumed"]  # "esumed" is inside "resumed" only
    assert out["rev"] == ["men-rev-helper", "men-eval-reviewer-resumed"]  # prefix before word prefix
    assert out["call"] == ["men-xb-call-alpha", "men-xa-call-beta"]  # a tie: online lists first


def test_the_composer_highlights_only_known_mentions() -> None:
    """Issue #110: #input-mirror wraps @claude-1 (an active member) and @alice (the signed-in
    human) in their own .mention-hl span, the same names delivery.rules.parse_mentions would
    match, while @nope-one (nobody by that name) stays plain text with no style of its own; and
    @codex-1, picked from the @ popover rather than typed, is highlighted too. Issue #111:
    @everyone and @here highlight the same way, unconditionally -- the composer is always a
    person's, so there's no "plain text from an agent" case to tell apart here -- while @all
    (reserved, but not a broadcast keyword) stays plain, like any other unknown name. Issue
    #138: @humans highlights too, for the same reason (both a person's and an agent's @humans
    are real, so the composer never needs to tell them apart either)."""
    out = run("mention_highlight_in_composer")
    assert out["typed"]["text"] == "ask @claude-1 and @nope-one, cc @alice"
    assert out["typed"]["hl"] == ["@claude-1", "@alice"]
    assert out["picked"]["value"] == "hi @codex-1 " and out["picked"]["hl"] == ["@codex-1"]
    assert out["broadcast"]["text"] == "@humans needs eyes, @everyone look, then @here too, but not @all"
    assert out["broadcast"]["hl"] == ["@humans", "@everyone", "@here"]


def test_broadcast_mentions_lead_the_popover_above_the_member_list() -> None:
    """Issue #111: @here and @everyone show first in the @ popover with a one-line description
    each, not counted in "Agents in #build <n>"; narrowing the query to one neither matches
    leaves it out like any other candidate; picking one completes it like a member would.
    Issue #138: @humans leads both of them, with its own description, and its own prefix "hu"
    (shared with no member, and not with "here"/"everyone") matches only it."""
    out = run("broadcast_popover")
    assert out["all"]["ids"] == [
        "men-humans",
        "men-here",
        "men-everyone",
        "men-claude-1",
        "men-codex-1",
        "men-devin-1",
        "men-bench",
    ]
    assert out["all"]["count"] == "4"  # the 4 agents, not 7: the broadcasts aren't "agents in #build"
    assert out["all"]["humansDesc"] and out["all"]["hereDesc"] and out["all"]["everyoneDesc"]
    assert len({out["all"]["humansDesc"], out["all"]["hereDesc"], out["all"]["everyoneDesc"]}) == 3
    assert out["humansFiltered"] == ["men-humans"]  # "hu": only @humans' own prefix matches
    assert out["filtered"] == ["men-everyone"]  # "ever": only @everyone's own prefix matches
    assert out["picked"]["value"] == "hi @everyone "


def test_the_mention_popover_lists_people_after_the_agents_that_match() -> None:
    """Issue #418: on a hosted broker the @ popover lists the room's people after the agents that
    match, so a person can be @mentioned from the page at all. Everyone the `people` frame names
    is offered in a public room, those with a page open now before those away at the same rank,
    never the viewer (alice); a person has the round person avatar and a presence dot, and no tier
    chip (nothing wakes a person); the agents keep their own head and count and the people get
    theirs. Picking one inserts @name, which the composer highlights without the admin's people
    list. Fails if the popover still walks only the room's agents: no person is ever listed."""
    out = run("mention_people_popover")
    assert out["all"]["ids"] == [
        "men-humans",
        "men-here",
        "men-everyone",
        "men-claude-1",
        "men-codex-1",
        "men-devin-1",
        "men-bench",
        "men-helper-bob",
        "men-bobby",  # the people who are online first, in the frame's order,
        "men-carol",
        "men-bob",  # then bob, who is away; alice, the viewer, is left out
    ]
    assert out["all"]["heads"] == ["Agents in #build5", "People in #build3"]
    assert out["all"]["foot"] == "An @mention wakes an agent at once; a person gets a red badge."
    assert out["bo"]["ids"] == ["men-helper-bob", "men-bobby", "men-bob"]  # agents, then people, online first
    assert out["bo"]["heads"] == ["Agents in #build1", "People in #build2"]
    person = out["person"]
    assert person["bobbyAvatar"] == "avatar human" and person["bobbyDot"] == "dot s-human"
    assert person["bobbyStatus"] == "Online · Person"
    assert person["bobDot"] == "dot s-offline" and person["bobStatus"] == "Offline · Person"
    assert person["bobbyTier"] == 0 and person["helperTier"] == 1  # an agent has its tier chip, a person none
    assert out["exact"] == ["men-helper-bob", "men-bob", "men-bobby"]  # the exact name, then the longer one
    assert out["car"] == {"ids": ["men-carol"], "heads": ["People in #build1"]}  # no agent: one head
    assert out["viewer"] == {"hidden": True}  # "@ali" is only the viewer: nothing to offer
    assert out["picked"] == {"value": "hi @carol ", "hl": ["@carol"], "hidden": True}
    assert out["said"] == []  # choosing someone sends nothing


def test_a_private_room_offers_only_its_own_people() -> None:
    """Issue #418: in a private room (DESIGN.md §44) the @ popover lists the room's own people,
    from the room listing, not everyone the `people` frame names; the public room still lists
    everyone. Fails if a private room offers people who can't see it."""
    out = run("mention_people_private_room")
    assert out["publicRoom"] == ["men-bob", "men-bobby"]
    assert out["secretCarol"] == {"hidden": True}  # carol is not in #secret
    assert out["secretBo"] == ["men-bob"]  # and bobby is not either
    assert out["secretAll"]["ids"] == ["men-humans", "men-here", "men-everyone", "men-bob"]
    assert out["secretAll"]["heads"] == ["Agents in #secret0", "People in #secret1"]


def test_another_persons_mention_of_you_counts_but_not_your_own() -> None:
    """Issue #418: bob's @mention of alice raises the same red badge as an agent's -- the room's
    tab, the phone's Rooms badge, the title count -- and marks his row as a mention. Bob not
    naming her, bob naming carol, and alice naming herself do none of it. Focus mode leaves a
    person's message alone and still collapses an agent's unaddressed chatter. Fails if a person's
    mention is dropped because a person sent it (mentionsMe used to return false for any human)."""
    out = run("person_mention_badges")
    nothing = {
        "tab": {"badge": None, "quietDot": None},
        "title": "switchboard — #build",
        "roomsBadge": {"text": "0", "hidden": True},
    }
    assert out["plain"] == out["other"] == out["own"] == nothing  # not even a quiet dot

    mentioned = out["mentioned"]
    assert mentioned["tab"] == {"badge": {"text": "1", "label": "1 message for you"}, "quietDot": None}
    assert mentioned["title"] == "(1) switchboard — #build"
    assert mentioned["roomsBadge"] == {"text": "1", "hidden": False}
    assert out["agent"]["tab"]["badge"] == {"text": "2", "label": "2 messages for you"}  # an agent's adds
    assert out["agent"]["title"] == "(2) switchboard — #build"
    assert out["opened"]["tab"] == {"badge": None, "quietDot": None}
    assert out["opened"]["title"] == "switchboard — #ops"

    # the row's mention style: bob's @alice (13) and the agent's (14), not the others
    marked = {i: r["mention"] for i, r in out["rows"].items()}
    assert marked == {"10": False, "11": False, "12": False, "13": True, "14": True, "15": False}
    assert not any(r["collapsible"] for r in out["rows"].values())  # Focus is off
    collapsed = {i for i, r in out["focus"].items() if r["collapsible"]}
    assert collapsed == {"15"}  # only the agent's unaddressed chatter; bob's rows are people's


BLOCKED_WORDS = {
    "empty": "No items yet",
    "question_and_fixes": "1 question to answer, 2 fixes to confirm",
    "one_fix": "1 fix to confirm",
    "contested_and_working": "1 finding to decide, 3 findings in progress",
    "one_working": "1 finding in progress",
    "everything": "2 questions to answer, 1 finding to decide, 1 fix to confirm, 1 finding in progress",
    "not_my_agents": "None of your agents is in this room",
}


def test_hand_off_is_in_the_boards_header_and_says_what_is_left() -> None:
    """#425: Hand off was a card at the top of the Settled lane, so it read as one of the settled
    findings. It is in the header now, in no lane, and while it is disabled the line beside it
    says what is left, in plain words and with the right singular and plural, most-yours first:
    questions, then contested findings, fixes to confirm, and findings still in progress. An
    empty board says so, and so does a settled one with none of your agents in the room (the
    rules the button always had). Fails if Hand off is drawn in a lane, if a blocked board still
    shows an enabled button, or if the words name the wrong things."""
    out = run("board_handoff_header")
    for name, words in BLOCKED_WORDS.items():
        c = out["cases"][name]
        assert (c["text"], c["button"], c["disabled"], c["inHead"], c["inLane"]) == (
            words,
            True,
            True,
            True,
            False,
        ), name
        assert c["group"] == "board-handoff is-blocked" and c["describedby"] == "board-handoff-status", name
    assert out["cases"]["first"]["text"] == "1 question to answer"  # the board as it first opens
    assert all(c["lanes"] == 3 and not c["inLane"] for c in out["cases"].values())


def test_a_settled_board_shows_how_far_it_got_and_an_enabled_hand_off() -> None:
    """#425: ready is "3 of 3 settled" (a confirmed fix, a dropped finding and an answered
    question all count) beside an enabled button; handed off is "Handed off by claude-1 at
    12:34" in place of the button, with the day when it wasn't today. Fails if a dropped or
    answered item isn't counted as settled, or if the button stays after the handoff."""
    out = run("board_handoff_header")["cases"]
    ready = out["ready"]
    assert (ready["text"], ready["button"], ready["disabled"], ready["group"]) == (
        "3 of 3 settled",
        True,
        False,
        "board-handoff is-ready",
    )
    assert not ready["inLane"] and ready["inHead"]
    today, before = out["done_today"], out["done_before"]
    assert today["text"] == today["expected"] == "Handed off by claude-1 at 12:34"
    assert before["text"].startswith("Handed off by claude-1 on ") and before["text"].endswith(" at 14:13")
    for done in (today, before):
        assert done["button"] is False and done["group"] == "board-handoff is-done" and not done["inLane"]


def test_the_nudge_lasts_a_moment_after_the_last_item_settles() -> None:
    """#425: when the last item settles, Hand off gets a brief highlight (a class the stylesheet
    animates: no toast, no sound). A person's own move redraws the board twice, the reply and the
    live frame, so the second redraw keeps it; a redraw after the window, and a board that is
    ready when it is opened, get none. Fails if the highlight goes with the first redraw, if
    every redraw of a ready board flashes, or if a board that opened ready is treated as news."""
    out = run("board_handoff_header")["cases"]
    assert out["ready"]["nudge"] is True
    assert out["ready_again"]["nudge"] is True  # the same board again, straight after
    assert out["ready_later"]["nudge"] is False  # and again once the window has passed
    assert out["ready_reopened"]["nudge"] is False  # closed and opened again, already ready
    assert not any(out[name]["nudge"] for name in BLOCKED_WORDS)  # nothing flashes while it is disabled


def test_an_argument_less_palette_command_runs() -> None:
    out = run("palette_runs")
    assert out["commands"] == ["/help"] and texts(out) == ["/helphelp text"]


def test_the_welcome_form_creates_the_named_room() -> None:
    out = run("welcome_create")
    assert out["shown"] and out["label"] == "Create #ops" and out["preview"] == "join switchboard room #ops"
    assert out["posted"] == "#ops" and out["tabs"] == ["#ops"]
    assert out["placeholder"] == "Message #ops — @ to mention, / for commands"


def test_a_room_with_several_open_boards_shows_tabs_and_one_board_at_a_time() -> None:
    """#445: with one open board the page is as it always was (no tab list); with two or more the
    header has a tab list, the first board is on show, a tab shows another with its own cards, a
    redraw keeps the chosen board, and the board that closes lets the first take its place. Fails
    if tabs show for one board, a switch keeps the old board's cards, a members frame jumps back to
    the first board, or a closed board stays on show."""
    out = run("board_tabs")
    assert out["one"]["list"] == 0 and out["one"]["tabs"] == [] and out["one"]["cards"] == ["F1"]
    three = out["three"]
    assert three["list"] == 1
    assert [(t["text"], t["selected"], t["tabindex"], t["role"]) for t in three["tabs"]] == [
        ("shop #7", "true", 0, "tab"),
        ("shop #81", "false", -1, "tab"),  # the second has one item that needs a person (the "1")
        ("git.example.com/pr/3", "false", -1, "tab"),
    ]
    assert three["tabs"][1]["label"] == "shop #8, 1 needs you"
    assert three["tabs"][1]["title"] == "https://github.com/example/shop/pull/8"
    assert three["cards"] == ["F1"] and three["shown"] == "https://github.com/example/shop/pull/7"
    second = out["second"]
    assert sorted(second["cards"]) == ["F1", "Q1"]  # the second board's own cards
    assert [t["selected"] for t in second["tabs"]] == ["false", "true", "false"]
    assert second["shown"] == "https://github.com/example/shop/pull/8"
    assert out["redrawn"]["cards"] == second["cards"] and out["redrawn"]["tabs"] == second["tabs"]
    assert [t["selected"] for t in out["fell_back"]["tabs"]] == ["true", "false"]  # the first is shown again
    assert out["fell_back"]["cards"] == ["F1"] and out["fell_back"]["shown"].endswith("/pull/7")
    assert out["last"]["list"] == 0 and out["last"]["tabs"] == []  # one left: no tabs
    assert out["none"] == {"hidden": True}
