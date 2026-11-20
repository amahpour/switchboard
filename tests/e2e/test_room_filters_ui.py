"""A room's filters in a real browser (#914, DESIGN.md §64): the All, Mentions and Files row over the log,
each showing exactly its messages, ones older than the history the page loaded included; Show older; going
back to All, by the button and by posting; the keyboard; a phone and both themes. The seeded desktop broker
(the person is alice); the lines and files are planted straight into the store, since the page reads both
from the API. Any console error, page error or CSP violation fails the test. Marker ``e2e``."""

from __future__ import annotations

import re
import secrets
import shutil
from collections.abc import Iterator
from typing import Any

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
