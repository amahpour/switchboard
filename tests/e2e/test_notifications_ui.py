"""Browser notifications, level 1 (#723, DESIGN.md §55.9), on the real page.

The browser's Notification is stubbed (the page is given a fake that records what it was asked), and
``document.hidden`` is under the test's control. The permission is asked only from the click in Settings,
never on load; a notification shows only while the tab is hidden, once per item, for what needs this person
(a message to them, an agent waiting for their approval); and clicking one opens the item. Marker ``e2e``.
"""

from __future__ import annotations

import secrets
import shutil
import time
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import PHASE_REPORTS, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import UI
from ui_world import UIWorld

pytestmark = pytest.mark.e2e

STUB = """
window.__hidden = false;
Object.defineProperty(document, 'hidden', { get: () => window.__hidden });
window.__asked = 0;
window.__shown = [];
class FakeNotification {
  constructor(title, opts) {
    this.title = title; this.body = (opts || {}).body; this.closed = false; window.__shown.push(this);
  }
  close() { this.closed = true; }
  static requestPermission() {
    window.__asked += 1; FakeNotification.permission = 'granted'; return Promise.resolve('granted');
  }
}
FakeNotification.permission = 'default';
window.Notification = FakeNotification;
"""


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


def room_page(ui: UI) -> tuple[Page, str]:
    room = "nt-" + secrets.token_hex(3)
    ui.world.create_room(f"#{room}")
    ui.world.add_agents(f"#{room}", ("scout", "bench"))
    ctx = ui.context()
    ctx.add_init_script(STUB)
    page = ctx.new_page()
    page.goto(ui.world.broker.login_url())
    expect(page.locator("#st-conn")).to_have_text("Connected")
    page.locator(f'#tabs .room[data-room="#{room}"]').click()
    expect(page.locator("#room-title")).to_have_text(room)
    return page, room


def turn_on(page: Page) -> None:
    page.click("#me-settings")
    tab = page.locator("#settings-tab-profile")
    if tab.is_visible():
        tab.click()
    page.locator("#settings-notify").check()
    expect(page.locator("#settings-notify")).to_be_checked()
    page.keyboard.press("Escape")


def wait_shown(page: Page, n: int, timeout_ms: float) -> None:
    """Wait for the n-th notification (the page's CSP forbids wait_for_function's string)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while len(shown(page)) < n:
        assert time.monotonic() < deadline, "no notification in time"
        page.wait_for_timeout(100)


def shown(page: Page) -> list[str]:
    return page.evaluate("window.__shown.map(n => n.title)")


def test_the_permission_is_asked_only_from_the_settings_click(ui: UI) -> None:
    """Loading the page asks for nothing; ticking the box in Settings asks once and the setting stays on.
    Fails if the prompt comes on load (browsers refuse that) or the click doesn't ask."""
    page, _room = room_page(ui)
    assert page.evaluate("window.__asked") == 0
    turn_on(page)
    assert page.evaluate("window.__asked") == 1
    page.reload()
    expect(page.locator("#st-conn")).to_have_text("Connected")
    assert page.evaluate("window.__asked") == 0  # a reload asks nothing again


def test_an_agent_waiting_for_approval_notifies_once_and_only_while_hidden(ui: UI) -> None:
    """An approval prompt shows a notification for the person's own agent while the tab is hidden, once;
    clearing it and a new prompt is news again; with the tab visible nothing shows; and clicking the
    notification opens the room. Fails if it shows on a visible tab, repeats for one prompt, or the click
    goes nowhere."""
    page, room = room_page(ui)
    turn_on(page)
    ui.world.set_member_status(f"#{room}", "scout", "waiting-approval", "hook:Notification")
    expect(page.locator('#buddy-list .member[data-name="scout"]')).to_contain_text("Waiting for approval")
    assert shown(page) == []  # the tab is visible: the page is the notification
    ui.world.set_member_status(f"#{room}", "scout", "busy", "hook:PostToolUse")
    expect(page.locator('#buddy-list .member[data-name="scout"]')).to_contain_text("Busy")
    page.evaluate("window.__hidden = true")
    ui.world.set_member_status(f"#{room}", "scout", "waiting-approval", "hook:Notification")
    wait_shown(page, 1, 15_000)
    assert shown(page) == [f"scout is waiting for your approval in #{room}"]
    ui.world.set_member_status(f"#{room}", "bench", "idle", "hook:Stop")  # another frame: no repeat
    expect(page.locator('#buddy-list .member[data-name="bench"]')).to_contain_text("Idle")
    assert len(shown(page)) == 1
    page.locator('#tabs .room[data-room="#build"]').click()
    page.evaluate("window.__hidden = false; window.__shown[0].onclick()")
    expect(page.locator("#room-title")).to_have_text(room)
    assert page.evaluate("window.__shown[0].closed")


def test_a_message_to_the_person_notifies_while_hidden_without_its_text(ui: UI) -> None:
    """A message that @mentions the person shows one notification naming who and where, never the text of it.
    Fails if the notification carries the message text."""
    page, room = room_page(ui)
    turn_on(page)
    page.evaluate("window.__hidden = true")
    ui.world.agent_say(f"#{room}", "scout", "@alice the deploy key is hunter2, can you approve?")
    wait_shown(page, 1, 30_000)
    assert shown(page) == [f"scout needs you in #{room}"]
    assert "hunter2" not in page.evaluate("window.__shown[0].body + window.__shown[0].title")


def test_nothing_shows_while_the_setting_is_off(ui: UI) -> None:
    """With the setting never turned on, a message to the person while the tab is hidden shows nothing, though
    the page did hear it (the Home count in the sidebar rises: that is what proves it ran, not a wait). Fails
    if the page shows a notification without the person having turned them on."""
    page, room = room_page(ui)
    page.evaluate("window.__hidden = true; Notification.permission = 'granted'")
    ui.world.agent_say(f"#{room}", "scout", "@alice are you there?")
    expect(page.locator("#home-badge")).to_be_visible(timeout=30_000)
    assert shown(page) == []
