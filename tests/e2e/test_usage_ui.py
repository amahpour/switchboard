"""The usage page in a real browser (#818, DESIGN.md §61): the wakes, the posts and the budget
the room has left, on a phone in the dark theme and on a desktop in the light one. Opening a
row opens that room. A console error, a page error or a CSP violation fails the test."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import DESKTOP, PHASE_REPORTS, PHONE, UI

from switchboard.models import session_key

pytestmark = pytest.mark.e2e


def no_sideways_scroll(page: Page) -> None:
    """The page does not scroll sideways, and no usage control sticks out past the right edge."""
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert wide <= 0, f"the page scrolls sideways by {wide}px"
    out = page.evaluate(
        """() => Array.from(document.querySelectorAll('#usage button, #usage .usage-name')).filter(
            e => e.getBoundingClientRect().right > window.innerWidth + 0.5).map(e => e.className)"""
    )
    assert out == [], f"cut off on the right: {out}"


@pytest.fixture(scope="module")
def meter(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    """A broker of its own, so planting batches here cannot change another test's rooms."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        broker = InProcBroker(home).start()
        try:

            def plant() -> None:
                store = broker.state.store
                room = store.create_room("#meter", "alice", 40, 0)
                person = store.upsert_participant("claude", session_key("claude", "", "meter-1"))
                joined = store.create_membership(room.id, person.id, "meter-1")
                store.set_held(joined.id, True)
                store.set_status(person.id, "offline", "test")
                for text in ("one", "two"):
                    msg = store.insert_message(
                        room.id, sender_name="alice", sender_kind="human", via="web", text=text
                    )
                    store.create_batch(joined.id, path="wait", kind="wake", counted=True, items=[(msg.id, 1)])
                store.insert_message(
                    room.id,
                    sender_name="meter-1",
                    sender_kind="agent",
                    via="mcp",
                    text="posted",
                    sender_membership_id=joined.id,
                    sender_harness="claude",
                )

            broker.on_loop(plant)
            yield broker
        finally:
            broker.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def ui(browser: Browser, meter: InProcBroker, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, None)  # type: ignore[arg-type]
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


@pytest.mark.parametrize("phone", [False, True])
def test_usage_shows_the_wakes_the_posts_and_the_budget(ui: UI, meter: InProcBroker, phone: bool) -> None:
    """Two wakes and one post are what was stored, and the budget is what those wakes left.
    The phone is dark and the desktop is light, and a row opens its room."""
    opts: dict[str, Any] = dict(PHONE if phone else DESKTOP)
    opts["color_scheme"] = "dark" if phone else "light"
    page = ui.context(**opts).new_page()
    page.goto(meter.login_url())
    expect(page.locator("#st-conn")).to_have_text("Connected")
    if phone:
        page.locator("#rooms-toggle").click()
    page.locator("#usage-nav").click()
    expect(page.locator("#room-title")).to_have_text("Usage")
    row = page.locator("#usage-rooms li")
    expect(row).to_contain_text("38 of 40 left this hour")
    expect(row).to_contain_text("Today: 2 wakes, 1 message")
    expect(row).to_contain_text("7 days: 2 wakes, 1 message")
    expect(page.locator("#usage-agents")).to_contain_text("meter-1")
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg == ("rgb(28, 28, 31)" if phone else "rgb(255, 255, 255)")
    if phone:
        no_sideways_scroll(page)
        box = row.locator("button").bounding_box()
        assert box is not None and box["height"] >= 44
    opener = page.locator('#usage-rooms button[data-room="#meter"]')
    opener.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#room-title")).to_have_text("meter")
