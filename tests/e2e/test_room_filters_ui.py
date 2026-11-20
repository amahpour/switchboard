"""A room's filters in a real browser (#914, DESIGN.md §64): the All, Mentions and Files row over the log,
each showing exactly its messages, ones older than the history the page loaded included; Show older; going
back to All, by the button and by posting; the keyboard; a phone and both themes. The seeded desktop broker
(the person is alice); the lines and files are planted straight into the store, since the page reads both
from the API. Any console error, page error or CSP violation fails the test. Marker ``e2e``."""

from __future__ import annotations

import hashlib
import re
import secrets
import shutil
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import PHASE_REPORTS, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHONE, UI, no_horizontal_scroll
from ui_world import UIWorld

from switchboard.broker.web import WS_BACKLOG

pytestmark = pytest.mark.e2e

ROOM, MANY = "e2e-filters", "e2e-filters-many"
CHATTER = WS_BACKLOG + 20  # more than the page loads, so the oldest lines are only the broker's to find


@pytest.fixture(scope="module")
def world(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[UIWorld]:
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        w = UIWorld().start()
        try:
            yield w
        finally:
            w.stop()


@pytest.fixture
def ui(browser: Browser, world: UIWorld, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, world)
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def seed(world: UIWorld) -> dict[str, int]:
    """#e2e-filters: a mention of alice and a file at the very start, then more chatter than the page loads,
    then @humans, a mention of someone else, a person naming alice, a call's snapshot and a last line.
    #e2e-filters-many: 53 mentions of alice, more than one page. Planted once; returns the ids by name."""
    if getattr(world, "_filter_ids", None):
        return world._filter_ids  # type: ignore[attr-defined,no-any-return]
    world.create_room("#" + ROOM)
    world.create_room("#" + MANY)
    ids: dict[str, int] = {}

    def plant() -> None:
        store = world.broker.state.store

        def add(room: str, key: str, name: str, kind: str, text: str, **kw: Any) -> int:
            r = store.get_room("#" + room)
            assert r is not None
            m = store.insert_message(
                r.id,
                sender_name=name,
                sender_kind=kind,
                via={"human": "web", "agent": "mcp"}.get(kind, "meeting"),
                text=text,
                sender_harness="claude" if kind == "agent" else None,
                **kw,
            )
            ids[key] = m.id
            return m.id

        def attach(room: str, key: str, name: str, kind: str, file: str) -> None:
            r = store.get_room("#" + room)
            assert r is not None
            fid = secrets.token_hex(16)
            digest = "0" * 64
            line = f"artifact: {file} | 3 | sha256:{digest} | switchboard:file/{fid}"
            mid = add(room, key, name, kind, line)
            store.file_insert(
                file_id=fid, room_id=r.id, name=file, size=3, sha256=digest, content_type="text/plain",
                uploader_name=name, uploader_kind="human" if kind == "human" else "meeting", state="stored",
                created_at=1.0, stored_at=1.0, message_id=mid,
            )  # fmt: skip

        first = "@alice the oldest question: which branch?"
        add(ROOM, "old_mention", "claude-1", "agent", first, mentions=["alice"])
        attach(ROOM, "old_file", "alice", "human", "first-notes.txt")
        for i in range(CHATTER):
            add(ROOM, f"chat{i}", "codex-1", "agent", f"chatter {i}")
        add(ROOM, "humans", "claude-1", "agent", "@humans the release notes are ready", mentions=["humans"])
        add(ROOM, "other", "claude-1", "agent", "@codex-1 your turn", mentions=["codex-1"])
        add(ROOM, "bob", "bob", "human", "@alice can you look?", mentions=["alice"])
        attach(ROOM, "snapshot", "call", "meeting", "screen-101500.png")
        add(ROOM, "last", "codex-1", "agent", "the last line")
        for i in range(53):
            add(MANY, f"many{i}", "claude-1", "agent", f"@alice question {i}", mentions=["alice"])

    world.broker.on_loop(plant)
    world._filter_ids = ids  # type: ignore[attr-defined]
    return ids


def shown(page: Page) -> list[int]:
    ids = page.eval_on_selector_all("#log .line[data-id]", "rs => rs.map(r => r.dataset.id)")
    return [int(x) for x in ids]


def button(page: Page, show: str) -> Any:
    return page.locator(f'#room-filters .room-filter[data-show="{show}"]')


def test_each_filter_shows_exactly_its_messages_older_history_included(ui: UI) -> None:
    """All is the live log, which does not reach the room's first lines. Mentions shows the three messages
    addressed to alice (the oldest one among them) and nothing else; Files the two with a stored file, the
    call's snapshot included, each as a download card; All brings the live log back. The room stays open
    throughout. Fails if a filter filters the loaded page (the oldest lines would be missing), shows another
    message, or All does not return to the latest."""
    ids = seed(ui.world)
    page = ui.open(ROOM, color_scheme="light")
    expect(page.locator(f'#log .line[data-id="{ids["last"]}"]')).to_have_count(1)
    expect(button(page, "all")).to_have_attribute("aria-pressed", "true")
    assert ids["old_mention"] not in shown(page) and ids["old_file"] not in shown(page)
    button(page, "mentions").click()
    expect(button(page, "mentions")).to_have_attribute("aria-pressed", "true")
    expect(button(page, "all")).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#room-filter-note")).to_have_text("3 messages addressed to you")
    want = [ids["old_mention"], ids["humans"], ids["bob"]]
    expect(page.locator("#log .line[data-id]")).to_have_count(3)
    assert shown(page) == want
    expect(page.locator("#room-title")).to_have_text(ROOM)
    button(page, "files").click()
    expect(page.locator("#room-filter-note")).to_have_text("2 messages with files")
    expect(page.locator("#log")).to_contain_text("first-notes.txt")
    expect(page.locator("#log")).to_contain_text("screen-101500.png")
    assert ids["old_file"] in shown(page) and ids["last"] not in shown(page)
    button(page, "all").click()
    expect(page.locator(f'#log .line[data-id="{ids["last"]}"]')).to_have_count(1)
    expect(page.locator("#room-filter-note")).to_have_text("")
    assert ids["old_mention"] not in shown(page)
    expect(page.locator("#room-title")).to_have_text(ROOM)


def test_show_older_pages_back_and_posting_returns_to_all(ui: UI) -> None:
    """With 53 mentions, Mentions shows the newest 50 and Show older; clicking it adds the 3 older ones above,
    with no duplicate, and the button goes. Posting from the composer leaves the filter: the line lands in
    the live log and All is pressed again. Fails if the cursor repeats or drops a message, or a post is
    hidden under a filter."""
    ids = seed(ui.world)
    page = ui.open(MANY)
    button(page, "mentions").click()
    expect(page.locator("#room-filter-note")).to_have_text("The newest 50 messages addressed to you")
    expect(page.locator("#log .filter-older")).to_be_visible()
    expect(page.locator("#log .line[data-id]")).to_have_count(50)
    page.locator("#log .filter-older").click()
    expect(page.locator("#room-filter-note")).to_have_text("53 messages addressed to you")
    expect(page.locator("#log .filter-older")).to_have_count(0)
    assert shown(page) == [ids[f"many{i}"] for i in range(53)]
    page.locator("#input").fill("thanks, all read")
    page.locator("#input").press("Enter")
    expect(button(page, "all")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#log .line.k-chat").last).to_contain_text("thanks, all read")


def test_the_row_works_by_keyboard_with_a_visible_focus(ui: UI) -> None:
    """Tab reaches All, Mentions and Files in order; Enter and Space choose; the focus stays on the row and
    shows a ring; choosing All keeps it there. Fails if a button is not reachable, the focus jumps away when
    the log redraws, or the ring is suppressed."""
    seed(ui.world)
    page = ui.open(ROOM, color_scheme="dark")
    button(page, "all").focus()
    page.keyboard.press("Tab")
    expect(button(page, "mentions")).to_be_focused()
    page.keyboard.press("Enter")
    expect(button(page, "mentions")).to_have_attribute("aria-pressed", "true")
    expect(button(page, "mentions")).to_be_focused()
    style = page.evaluate(
        "() => { const s = getComputedStyle(document.activeElement);"
        " return [s.outlineStyle, s.outlineWidth]; }"
    )
    assert style[0] != "none" and style[1] != "0px", style
    page.keyboard.press("Tab")
    expect(button(page, "files")).to_be_focused()
    page.keyboard.press("Space")
    expect(button(page, "files")).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Shift+Tab")
    expect(button(page, "all")).to_be_focused()
    page.keyboard.press("Enter")
    expect(button(page, "all")).to_have_attribute("aria-pressed", "true")
    expect(button(page, "all")).to_be_focused()


def test_on_a_phone_the_row_fits_and_its_buttons_are_tall_enough(ui: UI) -> None:
    """At 390 px the row and its note fit with no sideways scroll, each button is at least 44 px tall, and
    a filter works as on a desktop; opening another room returns to All. Fails if the row overflows the
    phone or its targets are too small to tap."""
    ids = seed(ui.world)
    page = ui.open(ROOM, color_scheme="dark", **PHONE)
    expect(page.locator("#room-filters")).to_be_visible()
    for show in ("all", "mentions", "files"):
        box = button(page, show).bounding_box()
        assert box is not None and box["height"] >= 44, (show, box)
    button(page, "mentions").click()
    expect(page.locator("#room-filter-note")).to_have_text("3 messages addressed to you")
    assert shown(page)[0] == ids["old_mention"]
    no_horizontal_scroll(page)
    page.click("#rooms-toggle")
    page.locator(f'#tabs .room[data-room="#{MANY}"]').click()
    expect(page.locator("#room-title")).to_have_text(MANY)
    expect(button(page, "all")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#app")).not_to_have_class(re.compile("search-open"))


# ------------------------------------------------- what arrives while a filter is on (the review of #916)
FILTER_NONE = "Nothing in this room is addressed to you yet."


def fresh_room(world: UIWorld, room: str, mentions: int = 0, agent: str | None = None) -> list[int]:
    """A room of this test's own: ``mentions`` planted @alice lines, oldest first, and a scripted agent named
    ``agent`` joined to speak live. Returns the planted ids."""
    world.create_room("#" + room)
    ids: list[int] = []

    def plant() -> None:
        store = world.broker.state.store
        r = store.get_room("#" + room)
        assert r is not None
        for i in range(mentions):
            m = store.insert_message(
                r.id, sender_name="claude-1", sender_kind="agent", via="mcp", text=f"@alice question {i}",
                sender_harness="claude", mentions=["alice"],
            )  # fmt: skip
            ids.append(m.id)

    world.broker.on_loop(plant)
    if agent:
        world.add_agents("#" + room, (agent,))
    return ids


def test_a_live_match_keeps_the_focus_on_show_older(ui: UI) -> None:
    """Show older has the keyboard when a new mention arrives: the log is drawn again with the new line, and
    the focus is on the new Show older, not dropped to the page. Fails if the redraw removes the focused
    button without giving the focus back."""
    room = "e2e-filters-focus"
    fresh_room(ui.world, room, mentions=53, agent="scout")
    try:
        page = ui.open(room)
        button(page, "mentions").click()
        expect(page.locator("#room-filter-note")).to_have_text("The newest 50 messages addressed to you")
        older = page.locator("#log .filter-older")
        older.focus()
        expect(older).to_be_focused()
        mid = ui.world.agent_say("#" + room, "scout", "@alice one more question?")
        expect(page.locator(f'#log .line[data-id="{mid}"]')).to_have_count(1)
        expect(page.locator("#room-filter-note")).to_have_text("The newest 51 messages addressed to you")
        expect(page.locator("#log .filter-older")).to_be_focused()
    finally:
        ui.world.close_extra_agents()


def test_a_live_reply_to_an_older_message_of_mine_shows_under_mentions(ui: UI) -> None:
    """alice's question is older than the history the page loaded; an agent replies to it, naming nobody,
    while Mentions is on. The reply is addressed to alice (the broker's rule: an agent's reply to something
    alice said), so it shows at once, as reopening Mentions would show it. Fails if the page judges the reply
    only by the parents it holds and drops it."""
    room = "e2e-filters-reply"
    fresh_room(ui.world, room, agent="helper")
    asked = ui.world.say(room, "which branch should ship?")

    def chatter() -> None:
        store = ui.world.broker.state.store
        r = store.get_room("#" + room)
        assert r is not None
        for i in range(CHATTER):
            store.insert_message(
                r.id, sender_name="codex-1", sender_kind="agent", via="mcp", text=f"chatter {i}",
                sender_harness="codex",
            )  # fmt: skip

    ui.world.broker.on_loop(chatter)
    try:
        page = ui.open(room)
        expect(page.locator("#log .line[data-id]").last).to_contain_text(f"chatter {CHATTER - 1}")
        assert asked not in shown(page)  # the question is beyond what the page holds
        button(page, "mentions").click()
        expect(page.locator("#room-filter-note")).to_have_text(FILTER_NONE)
        mid = ui.world.agent_say("#" + room, "helper", "main, once CI is green", reply_to=asked)
        expect(page.locator(f'#log .line[data-id="{mid}"]')).to_have_count(1)
        expect(page.locator("#room-filter-note")).to_have_text("1 message addressed to you")
        button(page, "all").click()
        button(page, "mentions").click()
        expect(page.locator("#room-filter-note")).to_have_text("1 message addressed to you")
        assert shown(page) == [mid]
    finally:
        ui.world.close_extra_agents()


UPLOAD = re.compile(r"(https?://\S+/api/upload/[A-Za-z0-9_-]+)")


def test_an_agents_upload_shows_under_files_once_it_is_stored(ui: UI) -> None:
    """An agent's artifact line arrives while its upload is pending, so Files leaves it out; when the upload
    is stored, the message is in Files at once, with its download card, without choosing Files again. Fails
    if the stored-file event only updates a row the full log holds."""
    room = "e2e-filters-upload"
    fresh_room(ui.world, room, agent="uploader")
    agent = ui.world._extra_named[("#" + room, "uploader")]
    body = b"notes"
    sha = hashlib.sha256(body).hexdigest()
    line = f"artifact: report.txt | {len(body)} | sha256:{sha} | /Users/someone/report.txt"
    try:
        page = ui.open(room)
        button(page, "files").click()
        expect(page.locator("#room-filter-note")).to_have_text("No files in this room yet.")
        said = ui.world._scripted(agent.say("#" + room, line))
        assert said["ok"] and said.get("share"), said
        found = UPLOAD.search(said["share"])
        assert found, said["share"]
        assert shown(page) == []
        put = httpx.put(found.group(1), content=body, timeout=10.0, trust_env=False)
        assert put.status_code == 201, put.text
        expect(page.locator(f'#log .line[data-id="{said["posted_id"]}"]')).to_have_count(1)
        expect(page.locator("#log .md-artifact-name")).to_have_text("report.txt")
        expect(page.locator("#room-filter-note")).to_have_text("1 message with a file")
    finally:
        ui.world.close_extra_agents()


def live_file(world: UIWorld, room: str, name: str) -> int:
    """A person's stored file and the message bound to it, sent to the open pages as the broker sends one."""

    def run() -> int:
        state = world.broker.state
        r = state.store.get_room("#" + room)
        assert r is not None
        fid = secrets.token_hex(16)
        line = f"artifact: {name} | 3 | sha256:{'0' * 64} | switchboard:file/{fid}"
        m = state.store.insert_message(r.id, sender_name="bob", sender_kind="human", via="web", text=line)
        state.store.file_insert(
            file_id=fid, room_id=r.id, name=name, size=3, sha256="0" * 64, content_type="text/plain",
            uploader_name="bob", uploader_kind="human", state="stored", created_at=1.0, stored_at=1.0,
            message_id=m.id,
        )  # fmt: skip
        state.service.hub.message(r.name, state.service.message_frame(m))
        return m.id

    return int(world.broker.on_loop(run))


def test_a_mention_that_came_under_files_is_still_waiting_on_home(ui: UI) -> None:
    """With Files on, an agent's @mention of alice arrives and is not shown; then a file arrives and is.
    Opening Home marks the room seen only as far as All had shown it, so the mention is still waiting on alice
    there. Fails if the page marks seen the newest line it holds, which the filter left out."""
    room = "e2e-filters-seen"
    fresh_room(ui.world, room, agent="asker")
    try:
        page = ui.open(room)
        button(page, "files").click()
        expect(page.locator("#room-filter-note")).to_have_text("No files in this room yet.")
        asked = ui.world.agent_say("#" + room, "asker", "@alice which branch should ship?")
        fid = live_file(ui.world, room, "plan.txt")  # after the mention on the same socket
        expect(page.locator(f'#log .line[data-id="{fid}"]')).to_have_count(1)
        assert asked not in shown(page)
        page.locator("#home-nav").click()
        expect(page.locator("#room-title")).to_have_text("Home")
        card = page.locator("#home .home-card", has_text="#" + room)
        expect(card).to_have_count(1)
        expect(card).to_contain_text("which branch should ship?")
    finally:
        ui.world.close_extra_agents()


def test_opening_search_returns_the_room_to_all(ui: UI) -> None:
    """Search lies over the room; opening it leaves the filter, so dismissing Search shows the live log with
    All pressed, not the filtered one. Fails if the filter survives under Search."""
    ids = seed(ui.world)
    page = ui.open(ROOM)
    button(page, "mentions").click()
    expect(page.locator("#room-filter-note")).to_have_text("3 messages addressed to you")
    page.locator("#log").click(position={"x": 5, "y": 5})
    page.keyboard.press("/")
    expect(page.locator("#search-input")).to_be_focused()
    page.keyboard.type("release notes")
    expect(page.locator("#app")).to_have_class(re.compile("search-open"))
    page.keyboard.press("Escape")
    expect(page.locator("#app")).not_to_have_class(re.compile("search-open"))
    expect(page.locator("#room-title")).to_have_text(ROOM)
    expect(button(page, "all")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#room-filter-note")).to_have_text("")
    expect(page.locator(f'#log .line[data-id="{ids["last"]}"]')).to_have_count(1)


def test_a_mention_that_came_under_files_still_waits_after_search(ui: UI) -> None:
    """With Files on, an agent's @mention of alice arrives and is not shown. Opening Search returns the room
    to All, but Search hides the log, so All is never on screen; Home is chosen from Search. The mention is
    still waiting on Home. Fails if leaving the filter for Search lets the seen mark go to the newest line
    held."""
    room = "e2e-filters-search-seen"
    fresh_room(ui.world, room, agent="asker")
    try:
        page = ui.open(room)
        button(page, "files").click()
        expect(page.locator("#room-filter-note")).to_have_text("No files in this room yet.")
        asked = ui.world.agent_say("#" + room, "asker", "@alice which tag should ship?")
        fid = live_file(ui.world, room, "notes.txt")  # after the mention on the same socket
        expect(page.locator(f'#log .line[data-id="{fid}"]')).to_have_count(1)
        assert asked not in shown(page)
        page.locator("#log").click(position={"x": 5, "y": 5})
        page.keyboard.press("/")
        expect(page.locator("#search-input")).to_be_focused()
        page.keyboard.type("release notes")
        expect(page.locator("#app")).to_have_class(re.compile("search-open"))
        page.locator("#home-nav").click()
        expect(page.locator("#room-title")).to_have_text("Home")
        card = page.locator("#home .home-card", has_text="#" + room)
        expect(card).to_have_count(1)
        expect(card).to_contain_text("which tag should ship?")
    finally:
        ui.world.close_extra_agents()


def test_a_mention_under_files_still_waits_after_the_whiteboard_and_search(ui: UI) -> None:
    """With Files on, an agent's @mention of alice arrives and is not shown. The whiteboard is opened, then
    Search over it; Escape closes Search and leaves the whiteboard, which still covers the log, so All is
    never on screen; Home is chosen. The mention is still waiting on Home. Fails if the seen mark follows any
    state rather than what the room's live log showed: here, if Escape from Search lets it reach the newest
    line."""
    room = "e2e-filters-board-seen"
    fresh_room(ui.world, room, agent="asker")
    try:
        page = ui.open(room)
        button(page, "files").click()
        expect(page.locator("#room-filter-note")).to_have_text("No files in this room yet.")
        asked = ui.world.agent_say("#" + room, "asker", "@alice which build should ship?")
        fid = live_file(ui.world, room, "build.txt")  # after the mention on the same socket
        expect(page.locator(f'#log .line[data-id="{fid}"]')).to_have_count(1)
        assert asked not in shown(page)
        page.get_by_role("button", name="Whiteboard", exact=True).click()
        expect(page.locator("#app")).to_have_class(re.compile("whiteboard-open"))
        page.locator("#search-input").click()
        page.keyboard.type("release notes")
        expect(page.locator("#app")).to_have_class(re.compile("search-open"))
        page.keyboard.press("Escape")
        expect(page.locator("#app")).not_to_have_class(re.compile("search-open"))
        expect(page.locator("#app")).to_have_class(re.compile("whiteboard-open"))
        page.locator("#home-nav").click()
        expect(page.locator("#room-title")).to_have_text("Home")
        card = page.locator("#home .home-card", has_text="#" + room)
        expect(card).to_have_count(1)
        expect(card).to_contain_text("which build should ship?")
    finally:
        ui.world.close_extra_agents()


def test_a_mention_under_files_still_waits_after_a_review_board_and_search(ui: UI) -> None:
    """With Files on, an agent's @mention of alice arrives and is not shown. The room's past review boards
    are opened (#main.board-open hides the log), then Search over them; Escape closes Search and leaves the
    boards, which still cover the log, so All is never on screen; Home is chosen. The mention is still
    waiting on Home. Fails if the seen mark is decided from a list of app states that misses a view covering
    the log: here, if Escape from Search lets it reach the newest line."""
    room = "e2e-filters-review-seen"
    fresh_room(ui.world, room, agent="asker")
    try:
        page = ui.open(room)
        button(page, "files").click()
        expect(page.locator("#room-filter-note")).to_have_text("No files in this room yet.")
        asked = ui.world.agent_say("#" + room, "asker", "@alice which image should ship?")
        fid = live_file(ui.world, room, "image.txt")  # after the mention on the same socket
        expect(page.locator(f'#log .line[data-id="{fid}"]')).to_have_count(1)
        assert asked not in shown(page)
        page.locator("#past-boards-toggle").click()
        expect(page.locator("#main")).to_have_class(re.compile("board-open"))
        expect(page.locator("#log")).to_be_hidden()
        page.locator("#search-input").click()
        page.keyboard.type("release notes")
        expect(page.locator("#app")).to_have_class(re.compile("search-open"))
        page.keyboard.press("Escape")
        expect(page.locator("#app")).not_to_have_class(re.compile("search-open"))
        expect(page.locator("#main")).to_have_class(re.compile("board-open"))
        expect(page.locator("#log")).to_be_hidden()
        page.locator("#home-nav").click()
        expect(page.locator("#room-title")).to_have_text("Home")
        card = page.locator("#home .home-card", has_text="#" + room)
        expect(card).to_have_count(1)
        expect(card).to_contain_text("which image should ship?")
    finally:
        ui.world.close_extra_agents()


def around_with_a_hidden_mention(ui: UI, room: str, word: str) -> tuple[Page, int]:
    """``room`` open and seen, then a search for ``word`` opens it around its one hit; an agent's @mention of
    alice arrives while the window is shown (so it is not drawn). Waits until the page has it: a later mention
    in a second room, on the same socket, shows that room's badge."""
    ping = "e2e-ping-" + word
    fresh_room(ui.world, room, agent="asker")
    fresh_room(ui.world, ping, agent="pinger")
    hit = ui.world.agent_say("#" + room, "asker", f"the {word} plan is drafted")
    page = ui.open(room)
    expect(page.locator(f'#log .line[data-id="{hit}"]')).to_have_count(1)
    page.locator("#search-input").click()
    page.keyboard.type(word)
    expect(page.locator("#search-results .search-hit")).to_have_count(1)
    page.locator("#search-results .search-hit").first.click()
    expect(page.locator("#banner-around")).to_be_visible()
    asked = ui.world.agent_say("#" + room, "asker", "@alice which tag should ship?")
    ui.world.agent_say("#" + ping, "pinger", "@alice ping")
    expect(page.locator(f'#tabs .room[data-room="#{ping}"] .badge')).to_have_text("1")
    assert asked not in shown(page)
    return page, asked


def test_back_to_results_does_not_mark_a_mention_the_window_kept_off_screen(ui: UI) -> None:
    """F9: in a window around a search result, an @mention of alice arrives and is not drawn. Back to results
    draws the live log and opens Search over it in the same click, so the mention is never on screen; Home is
    chosen from Search and the mention still waits there. Fails if the mark is taken when the log is drawn,
    before the click's own Search covers it."""
    room = "e2e-filters-back-seen"
    try:
        page, _asked = around_with_a_hidden_mention(ui, room, "quokka")
        page.click("#around-back")
        expect(page.locator("#app")).to_have_class(re.compile("search-open"))
        expect(page.locator("#search-results .search-hit")).to_have_count(1)
        page.locator("#home-nav").click()
        expect(page.locator("#room-title")).to_have_text("Home")
        card = page.locator("#home .home-card", has_text="#" + room)
        expect(card).to_have_count(1)
        expect(card).to_contain_text("which tag should ship?")
    finally:
        ui.world.close_extra_agents()


def test_jump_to_latest_marks_the_mention_it_shows(ui: UI) -> None:
    """F9's other half: the same hidden mention, but Jump to latest draws the live log and leaves it on
    screen, so the mention is seen: the page posts a mark that covers it and no card waits on Home. Fails if
    the painted-frame check never sends the mark (the room would stay waiting though alice read it)."""
    room = "e2e-filters-latest-seen"
    try:
        page, asked = around_with_a_hidden_mention(ui, room, "wombat")
        with page.expect_response(lambda r: r.url.endswith(f"/api/rooms/{room}/seen") and r.ok) as seen:
            page.click("#around-latest")
        assert seen.value.request.post_data_json["id"] >= asked
        expect(page.locator(f'#log .line[data-id="{asked}"]')).to_have_count(1)
        page.locator("#home-nav").click()
        expect(page.locator("#room-title")).to_have_text("Home")
        expect(page.locator("#home")).not_to_contain_text("Loading…")
        expect(page.locator("#home .home-card", has_text="#" + room)).to_have_count(0)
    finally:
        ui.world.close_extra_agents()


def test_a_mention_that_came_on_all_is_seen_when_leaving_for_home(ui: UI) -> None:
    """The ordinary case: the room is open on All when an agent's @mention of alice arrives, so it is drawn
    where alice can see it, and leaving for Home marks it seen: no card waits for it. Fails if the seen mark
    stops short of what the live log showed (a mark taken only when a room is opened, say)."""
    room = "e2e-filters-all-seen"
    fresh_room(ui.world, room, agent="asker")
    try:
        page = ui.open(room)
        expect(button(page, "all")).to_have_attribute("aria-pressed", "true")
        asked = ui.world.agent_say("#" + room, "asker", "@alice which branch is green?")
        expect(page.locator(f'#log .line[data-id="{asked}"]')).to_have_count(1)
        with page.expect_response(lambda r: r.url.endswith(f"/api/rooms/{room}/seen") and r.ok) as seen:
            page.locator("#home-nav").click()
        assert seen.value.request.post_data_json["id"] >= asked
        expect(page.locator("#room-title")).to_have_text("Home")
        expect(page.locator("#home")).not_to_contain_text("Loading…")
        expect(page.locator("#home .home-card", has_text="#" + room)).to_have_count(0)
    finally:
        ui.world.close_extra_agents()


def test_a_live_match_keeps_the_focus_on_a_download_link_in_a_row(ui: UI) -> None:
    """A Files row's download link has the keyboard (tabbed to) when another message with a file arrives: the
    log is drawn again, and the focus is on the same row's download link in the new log, not dropped to the
    page. Fails if focus is given back only to an element that carries data-focus or data-id itself."""
    room = "e2e-filters-focus-link"
    fresh_room(ui.world, room)
    first = live_file(ui.world, room, "first.txt")
    page = ui.open(room)
    button(page, "files").click()
    expect(page.locator("#room-filter-note")).to_have_text("1 message with a file")
    link = page.locator(f'#log .line[data-id="{first}"] a.md-file')
    link.focus()
    expect(link).to_be_focused()
    second = live_file(ui.world, room, "second.txt")
    expect(page.locator(f'#log .line[data-id="{second}"]')).to_have_count(1)
    expect(page.locator("#room-filter-note")).to_have_text("2 messages with files")
    expect(page.locator(f'#log .line[data-id="{first}"] a.md-file')).to_be_focused()


FILE_NAMES = ("first.txt", "second.txt")


def two_files(world: UIWorld, room: str, second: str = "stored") -> tuple[int, str, str]:
    """bob's message with two artifact lines, sent to the open pages: first.txt still pending, then
    second.txt stored (or in the state ``second``), so only the second has a download link. Returns the
    message id and both file ids."""

    def run() -> tuple[int, str, str]:
        state = world.broker.state
        r = state.store.get_room("#" + room)
        assert r is not None
        ids = (secrets.token_hex(16), secrets.token_hex(16))
        text = "\n".join(f"artifact: {n} | 3 | sha256:{'0' * 64} | /Users/someone/{n}" for n in FILE_NAMES)
        m = state.store.insert_message(r.id, sender_name="bob", sender_kind="human", via="web", text=text)
        for fid, name, st in zip(ids, FILE_NAMES, ("pending", second), strict=True):
            state.store.file_insert(
                file_id=fid, room_id=r.id, name=name, size=3, sha256="0" * 64, content_type="text/plain",
                uploader_name="bob", uploader_kind="human", state=st, created_at=1.0,
                stored_at=1.0 if st == "stored" else None, message_id=m.id,
            )  # fmt: skip
        state.service.hub.message(r.name, state.service.message_frame(m))
        return m.id, ids[0], ids[1]

    return world.broker.on_loop(run)  # type: ignore[no-any-return]


def store_file(world: UIWorld, room: str, mid: int, fid: str, *, tell: bool) -> None:
    """The pending file is stored, and with ``tell`` the pages get the broker's file frame (§53)."""

    def run() -> None:
        state = world.broker.state
        sha = "0" * 64
        got = state.store.file_mark_stored(fid, size=3, sha256=sha, content_type="text/plain", stored_at=2.0)
        assert got is not None and got.state == "stored"
        if tell:
            data = {"message_id": mid, "file": {"id": fid, "name": FILE_NAMES[0], "state": "stored"}}
            state.service.hub.file_stored("#" + room, data)

    world.broker.on_loop(run)


def file_link(page: Page, mid: int, fid: str) -> Any:
    return page.locator(f'#log .line[data-id="{mid}"] a.md-file[href="/api/files/{fid}"]')


@pytest.mark.parametrize("show", ["all", "files"])
def test_a_file_stored_above_a_focused_link_keeps_the_focus_on_its_file(ui: UI, show: str) -> None:
    """A row's second download link has the keyboard when the first file of the same message is stored: the
    file frame redraws the message's text, which now has a link before the focused one, and the focus is on
    the second file's new link, on All and under Files. Fails if the redraw drops the focus (the node it was
    on is replaced), or if it is given back by place, which lands on the first file's link."""
    room = f"e2e-filters-focus-stored-{show}"
    fresh_room(ui.world, room)
    page = ui.open(room)
    if show == "files":
        button(page, "files").click()
        expect(page.locator("#room-filter-note")).to_have_text("No files in this room yet.")
    mid, first, second = two_files(ui.world, room)
    expect(file_link(page, mid, second)).to_have_count(1)
    expect(file_link(page, mid, first)).to_have_count(0)
    file_link(page, mid, second).focus()
    expect(file_link(page, mid, second)).to_be_focused()
    store_file(ui.world, room, mid, first, tell=True)
    expect(file_link(page, mid, first)).to_have_count(1)
    expect(file_link(page, mid, second)).to_be_focused()


def test_a_files_page_that_adds_a_link_above_the_focus_keeps_it_on_its_file(ui: UI) -> None:
    """Files is chosen and its first page is held back; meanwhile a message arrives with first.txt pending and
    second.txt stored, is drawn, and second.txt's link takes the keyboard. first.txt is stored before the page
    is answered, so the answer's copy of the row has a link above the focused one. Once it is drawn, the focus
    is on second.txt's link. Fails if the redraw drops the focus, or gives it back by its place in the row,
    which is now first.txt's link."""
    room = "e2e-filters-focus-page"
    fresh_room(ui.world, room)
    page = ui.open(room)
    held: list[Any] = []
    page.route(re.compile(r"/api/rooms/[^/]+/filter\?show=files$"), lambda route: held.append(route))
    with page.expect_request(re.compile(r"/api/rooms/[^/]+/filter\?show=files$")):
        button(page, "files").click()
    mid, first, second = two_files(ui.world, room)
    expect(file_link(page, mid, second)).to_have_count(1)
    file_link(page, mid, second).focus()
    expect(file_link(page, mid, second)).to_be_focused()
    store_file(ui.world, room, mid, first, tell=False)
    assert len(held) == 1
    held[0].continue_()
    expect(file_link(page, mid, first)).to_have_count(1)
    expect(file_link(page, mid, second)).to_be_focused()


def test_a_file_stored_above_a_focused_link_in_the_filters_own_copy_keeps_the_focus(ui: UI) -> None:
    """Two copies of one row: the live log's, and the one Files fetched. A message arrives on All with both
    its files pending; second.txt is stored with no frame to this page (as during a disconnect), so the live
    copy has no link and Files, chosen next, draws the broker's copy with second.txt's link, which takes the
    keyboard. Then first.txt's file frame arrives. The row is drawn once, from the copy Files shows, and the
    focus stays on second.txt's link. Fails if the row is first drawn from the live copy (which has no second
    link: the focus drops to the row) before the merged copy is drawn."""
    room = "e2e-filters-focus-two-copies"
    fresh_room(ui.world, room)
    page = ui.open(room)
    mid, first, second = two_files(ui.world, room, second="pending")
    expect(page.locator(f'#log .line[data-id="{mid}"]')).to_have_count(1)
    expect(page.locator(f'#log .line[data-id="{mid}"] a.md-file')).to_have_count(0)
    store_file(ui.world, room, mid, second, tell=False)
    button(page, "files").click()
    expect(page.locator("#room-filter-note")).to_have_text("1 message with a file")
    expect(file_link(page, mid, second)).to_have_count(1)
    expect(file_link(page, mid, first)).to_have_count(0)
    file_link(page, mid, second).focus()
    expect(file_link(page, mid, second)).to_be_focused()
    store_file(ui.world, room, mid, first, tell=True)
    expect(file_link(page, mid, first)).to_have_count(1)
    expect(file_link(page, mid, second)).to_be_focused()
