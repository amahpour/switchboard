"""Browser notifications, level 1 (#723, DESIGN.md §55.9), on the real page.

The browser's Notification is stubbed (the page is given a fake that records what it was asked), and
``document.hidden`` is under the test's control. The permission is asked only from the click in Settings,
never on load; a notification shows only while the page is not in front (the tab hidden, or visible without
the focus: #837), once per item, for what needs this person (a message to them, an agent waiting for their
approval); it says the message's first line unless the person turned that off, wears the sender's avatar from
the broker's own origin, and clicking one opens the item. Marker ``e2e``.
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
window.__focused = true;
Object.defineProperty(document, 'hidden', { get: () => window.__hidden });
document.hasFocus = () => window.__focused;
window.__asked = 0;
window.__shown = [];
class FakeNotification {
  constructor(title, opts) {
    this.title = title; this.body = (opts || {}).body; this.icon = (opts || {}).icon;
    this.closed = false; window.__shown.push(this);
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


def set_harness(ui: UI, room: str, name: str, harness: str) -> None:
    """Make what a stand-in agent says from now on carry ``harness`` as its sender's, as the message of a real
    session of that harness does. The stand-in's own session is untouched (it still calls as a test agent), so
    a trigger stamps each message it posts, in the same transaction, before the page can read it."""
    from switchboard import db

    def update() -> None:
        s = ui.world.broker.state.store
        m = s.find_member(s.get_room(f"#{room}").id, name)
        with db.tx(s.con):
            s.con.execute(f"DROP TRIGGER IF EXISTS stamp_{m.membership_id}")
            s.con.execute(
                f"CREATE TRIGGER stamp_{m.membership_id} AFTER INSERT ON messages "
                f"WHEN NEW.sender_membership_id = {int(m.membership_id)} BEGIN "
                f"UPDATE messages SET sender_harness = '{harness}' WHERE id = NEW.id; END"
            )

    ui.world.broker.on_loop(update)


def put_text_setting(ui: UI, on: bool) -> None:
    web = ui.world.broker.web_client()
    try:
        assert (
            web.put("/api/me/notify", json={"text": on}, headers=ui.world.broker.write_headers()).status_code
            == 200
        )
    finally:
        web.close()


def test_a_message_to_the_person_notifies_with_its_first_line_by_default(ui: UI) -> None:
    """A message that @mentions the person, while the tab is hidden, shows "who in #room" with the message's
    first line as its body, cleaned of Markdown and cut to 100 characters. Fails if the body is the old fixed
    wording, still has the Markdown marks, or is not cut."""
    page, room = room_page(ui)
    turn_on(page)
    page.evaluate("window.__hidden = true")
    long = "word " * 40
    ui.world.agent_say(
        f"#{room}", "scout", f"@alice **Can you approve** the `deploy` now? {long}\nsecond line"
    )
    wait_shown(page, 1, 30_000)
    assert shown(page) == [f"scout in #{room}"]
    body = page.evaluate("window.__shown[0].body")
    assert body.startswith("Can you approve the deploy now? word word"), body
    assert len(body) <= 100 and body.endswith("\u2026"), body
    assert "**" not in body and "`" not in body and "second line" not in body


def test_the_message_text_can_be_hidden_by_the_setting(ui: UI) -> None:
    """With "Show message text in notifications" off the notification keeps the old wording and none of the
    message, secrets included; the box is on by default and the choice is kept by the broker. Fails if the
    text leaks while the setting is off, or the setting is off by default."""
    page, room = room_page(ui)
    turn_on(page)
    page.click("#me-settings")
    tab = page.locator("#settings-tab-profile")
    if tab.is_visible():
        tab.click()
    expect(page.locator("#settings-notify-text")).to_be_checked()  # on by default
    page.locator("#settings-notify-text").uncheck()
    try:
        expect(page.locator("#settings-notify-text")).not_to_be_checked()
        page.keyboard.press("Escape")
        page.evaluate("window.__hidden = true")
        ui.world.agent_say(f"#{room}", "scout", "@alice the deploy key is hunter2, can you approve?")
        wait_shown(page, 1, 30_000)
        assert shown(page) == [f"scout needs you in #{room}"]
        assert page.evaluate("window.__shown[0].body") == "A message for you"
        assert "hunter2" not in page.evaluate("window.__shown[0].body + window.__shown[0].title")
    finally:
        put_text_setting(ui, True)


def test_it_notifies_when_the_page_is_visible_but_not_focused_and_not_when_focused(ui: UI) -> None:
    """A page that is visible but has lost the focus (a call or an editor in front) notifies; the same message
    while the page has the focus shows nothing, and a later blur does not bring it back. Fails if only
    a hidden
    tab notifies (the live try of #837), or a focused page does."""
    page, room = room_page(ui)
    turn_on(page)
    ui.world.agent_say(f"#{room}", "scout", "@alice are you looking at this?")
    expect(page.locator("#home-badge")).to_be_visible(timeout=30_000)  # the page heard it, focused
    page.evaluate("window.__focused = false")
    assert shown(page) == []  # the blur alone shows nothing: it was already seen
    ui.world.agent_say(f"#{room}", "bench", "@alice and this, from the other agent?")
    wait_shown(page, 1, 30_000)
    assert shown(page) == [f"bench in #{room}"]
    assert page.evaluate("document.hidden") is False


def test_the_icon_is_the_brokers_own_never_a_remote_address(ui: UI) -> None:
    """The icon is a Codex agent's built-in image served by the broker, an admin's upload for Cursor under the
    upload's URL, and for an agent with no image a tile the page drew (a data: URL), never an http(s) address
    of another host. Fails if an icon points off the broker, or an upload is ignored."""
    from test_agent_icons_ui import picture, put_icon, reset_all

    page, room = room_page(ui)
    set_harness(ui, room, "scout", "codex")
    set_harness(ui, room, "bench", "cursor")
    put_icon(ui.world, "cursor", "light")
    put_icon(ui.world, "cursor", "dark")
    try:
        turn_on(page)
        page.evaluate("window.__hidden = true")
        ui.world.agent_say(f"#{room}", "scout", "@alice one")
        wait_shown(page, 1, 30_000)
        ui.world.agent_say(f"#{room}", "bench", "@alice two")
        wait_shown(page, 2, 30_000)
        icons = page.evaluate("window.__shown.map(n => n.icon)")
        assert icons[0] == "/static/agents/codex-light.png", icons
        assert icons[1].startswith("/api/agent-icons/cursor/light?v="), icons
        assert page.request.get(ui.world.broker.base + icons[0]).status == 200
        assert picture("cursor", "light")  # the upload is what the URL serves, by its own tests
        set_harness(ui, room, "scout", "claude")
        ui.world.agent_say(f"#{room}", "scout", "@alice three")
        wait_shown(page, 3, 30_000)
        assert page.evaluate("window.__shown[2].icon").startswith("data:image/png;base64,")
    finally:
        reset_all(ui.world)


def test_a_message_to_the_person_never_shows_while_the_page_is_in_front(ui: UI) -> None:
    """Visible and focused: a message to the person shows nothing, whatever the setting. Fails if a
    notification shows over the page the person is looking at."""
    page, room = room_page(ui)
    turn_on(page)
    ui.world.agent_say(f"#{room}", "scout", "@alice in front?")
    expect(page.locator("#home-badge")).to_be_visible(timeout=30_000)
    assert shown(page) == []


def test_nothing_shows_while_the_setting_is_off(ui: UI) -> None:
    """With the setting never turned on, a message to the person while the tab is hidden shows nothing, though
    the page did hear it (the Home count in the sidebar rises: that is what proves it ran, not a wait). Fails
    if the page shows a notification without the person having turned them on."""
    page, room = room_page(ui)
    page.evaluate("window.__hidden = true; Notification.permission = 'granted'")
    ui.world.agent_say(f"#{room}", "scout", "@alice are you there?")
    expect(page.locator("#home-badge")).to_be_visible(timeout=30_000)
    assert shown(page) == []


def test_a_prompt_that_was_open_when_the_tab_loaded_is_not_news(ui: UI) -> None:
    """An agent already waiting for approval when the page loads (a hidden tab reloaded) shows nothing when a
    later frame arrives for the same room; a fresh prompt after it still does. Fails if the page treats what
    was already open as news, as it did before the members the page held were compared."""
    room = "nt-" + secrets.token_hex(3)
    ui.world.create_room(f"#{room}")
    ui.world.add_agents(f"#{room}", ("scout", "bench"))
    ui.world.set_member_status(f"#{room}", "scout", "waiting-approval", "hook:Notification")
    broker = ui.world.broker
    web = broker.web_client()
    assert web.put("/api/me/notify", json={"on": True}, headers=broker.write_headers()).status_code == 200
    web.close()
    ctx = ui.context()
    # a hidden tab with notifications already allowed, loading while the prompt is open
    ctx.add_init_script(STUB + "window.__hidden = true; Notification.permission = 'granted';")
    page = ctx.new_page()
    page.goto(broker.login_url())
    expect(page.locator("#st-conn")).to_have_text("Connected")
    page.locator(f'#tabs .room[data-room="#{room}"]').click()
    expect(page.locator('#buddy-list .member[data-name="scout"]')).to_contain_text("Waiting for approval")
    ui.world.set_member_status(
        f"#{room}", "bench", "idle", "hook:Stop"
    )  # a frame for the room, scout unchanged
    expect(page.locator('#buddy-list .member[data-name="bench"]')).to_contain_text("Idle")
    assert shown(page) == []
    ui.world.set_member_status(f"#{room}", "bench", "waiting-approval", "hook:Notification")  # a new prompt
    wait_shown(page, 1, 15_000)
    assert shown(page) == [f"bench is waiting for your approval in #{room}"]
