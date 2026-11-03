"""The room's claims as a Board strip under the pins (#545, DESIGN.md §47).

A real page: the strip is hidden with no claims, then lists the key, the holder, the note
and the expiry. A link in the note opens through the page's existing link path. The entry
leaves at its deadline, the way Members drops an expired claim. Light, dark and the phone.

Marker ``e2e``.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import PHASE_REPORTS, sanitize_env
from fakes.waiting import wait_for
from playwright.sync_api import Browser, expect
from test_web_ui import PHONE, UI, no_horizontal_scroll
from ui_world import UIWorld

pytestmark = pytest.mark.e2e

NOTE = "review https://example.com/pr/56"


@pytest.fixture(scope="module")
def world(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[UIWorld]:
    """One seeded broker for this module. ``playwright`` starts first, under the real home."""
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


def _subscribed(ui: UI, room: str) -> None:
    wait_for(
        lambda: ui.world.broker.on_loop(
            lambda: any(
                "#" + room in sub.rooms and "members" in sub.kinds and not sub.closed
                for sub in ui.world.broker.state.hub.subs
            )
        ),
        what="the browser's members subscription",
    )


def test_the_board_shows_a_note_follows_its_link_and_drops_the_claim_when_it_expires(ui: UI) -> None:
    """The strip lists the claim, a link in the note opens that URL, and the row is gone
    once the claim's own deadline passes. Keyboard Enter opens the list and leaves focus
    on the toggle. Light, dark and the phone all fit. Fails if the strip stays empty, the
    note is not a link, or an expired claim remains."""
    room = "e2e-claim-board"
    ui.world.create_room("#" + room)
    ui.world.add_agents("#" + room, ("claude-1",))
    light = ui.open(room=room, color_scheme="light")
    expect(light.locator("#claim-board")).to_be_hidden()
    _subscribed(ui, room)
    held = ui.world.agent_call("#" + room, "claude-1", "claim", key="build", ttl=3600, note=NOTE)
    assert held["note"]

    pages = [light]
    for options in ({"color_scheme": "dark"}, {**PHONE, "color_scheme": "light"}):
        pages.append(ui.open(room=room, **options))

    for page in pages:
        page.bring_to_front()
        phone = bool(page.viewport_size and page.viewport_size["width"] < 760)
        toggle = page.locator("#claim-board-toggle")
        expect(toggle).to_be_visible()
        expect(toggle).to_contain_text("Board (1)")
        fg = toggle.evaluate("el => getComputedStyle(el).color")
        if page == light or phone:
            assert fg == "rgb(29, 29, 31)", fg
        else:
            assert fg == "rgb(236, 236, 240)", fg
        # A click focuses the document (a background page's focus() stays "inactive"),
        # then Enter is the keyboard: it toggles the list and leaves the focus on the button.
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        row = page.locator("#claim-board-list .claim-board-row")
        expect(row).to_be_visible()
        expect(row).to_contain_text("build")
        expect(row).to_contain_text("claude-1")
        expect(row).to_contain_text("Expires")
        link = row.locator("a.md-link")
        expect(link).to_have_attribute("href", "https://example.com/pr/56")
        expect(link).to_have_text("https://example.com/pr/56")
        # The phone's Members sheet covers the board with the scrim, so open it after the board.
        if phone:
            page.click("#buddy-toggle")
        holder = page.locator('#buddy-list .member[data-name="claude-1"] .m-claim-entry')
        expect(holder).to_contain_text("build")
        expect(holder).to_contain_text("Expires")
        expect(holder.locator("a.md-link")).to_have_attribute("href", "https://example.com/pr/56")
        no_horizontal_scroll(page)

    light.bring_to_front()
    # Playwright's mouse click does not open this target=_blank link (rel=noopener). The DOM
    # click does, and the new page is the one the browser would open.
    link = light.locator("#claim-board-list a.md-link")

    # The note's link is answered here, never by the real example.com: on CI that page
    # answered 404, the console check failed the test at teardown, and a test must not
    # need the internet anyway.
    def fulfill(route: Any) -> None:
        route.fulfill(status=200, content_type="text/html", body="<p>the pull request</p>")

    light.context.route("https://example.com/pr/56", fulfill)
    with light.context.expect_page() as opened:
        link.evaluate("el => el.click()")
    popup = opened.value
    expect(popup).to_have_url("https://example.com/pr/56")
    expect(popup.locator("p")).to_have_text("the pull request")
    popup.close()

    ui.world.agent_call("#" + room, "claude-1", "claim", key="build", ttl=0)
    ui.world.agent_call("#" + room, "claude-1", "claim", key="build", ttl=8, note="queued")
    for page in pages:
        expect(page.locator("#claim-board-toggle")).to_contain_text("Board (1)")
        expect(page.locator("#claim-board-list")).to_contain_text("queued")
    light.bring_to_front()
    light.locator("#claim-board-toggle").click()
    for page in pages:
        expect(page.locator("#claim-board")).to_be_hidden()
        expect(page.locator("#buddy-list .m-claim-entry")).to_have_count(0)
    expect(light.locator("#input")).to_be_focused()
