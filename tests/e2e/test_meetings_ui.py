"""A live call in the room (#573): the bar, Leave call, and a transcript line that is not a chat row.

The broker is seeded with a live meeting and one transcript line. No joinly runs: the page reads
the room's settings and its history. A console error, a page error or a CSP violation fails the
test, through the watcher in ``test_web_ui.UI``.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHASE_REPORTS, PHONE, UI

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def calls(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    """A desktop broker in a temp home. ``playwright`` starts first, under the real HOME."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        broker = InProcBroker(home).start()
        try:
            yield broker
        finally:
            broker.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def ui(browser: Browser, calls: InProcBroker, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, None)  # type: ignore[arg-type]
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def _seed(broker: InProcBroker) -> None:
    headers = broker.write_headers()
    client = broker.web_client()
    try:
        made = client.post("/api/rooms", json={"name": "#calls"}, headers=headers)
        assert made.status_code == 200, made.text
        said = client.post("/api/rooms/calls/say", json={"text": "a note from the room"}, headers=headers)
        assert said.status_code == 200, said.text
    finally:
        client.close()

    def plant() -> None:
        room = broker.state.store.get_room("#calls")
        assert room is not None
        row = broker.state.store.insert_meeting(room.id, started_by="alice", platform="zoom")
        broker.state.store.set_meeting_live(row.id)
        broker.state.store.insert_message(
            room.id,
            sender_name="bob",
            sender_kind="meeting",
            via="meeting",
            text="Codex, how much is gas in Los Angeles?",
        )

    broker.on_loop(plant)


def _open(ui: UI, broker: InProcBroker, **options: Any) -> Page:
    page = ui.context(**options).new_page()
    page.goto(broker.login_url())
    expect(page.locator("#st-conn")).to_have_text("Connected")
    tab = page.locator('#tabs .room[data-room="#calls"]')
    if not tab.is_visible():
        page.click("#rooms-toggle")
    tab.click()
    expect(page.locator("#room-title")).to_have_text("calls")
    return page


def test_a_live_call_shows_a_bar_a_leave_button_and_a_smaller_transcript_line(
    ui: UI, calls: InProcBroker
) -> None:
    """The bar names the platform, Leave call ends it, and a transcript line is tagged and smaller.

    Light, dark and a phone width all show the bar. The line carries a call tag and the speaker,
    and it is set smaller than a person's message.
    """
    _seed(calls)
    page = _open(ui, calls)
    bar = page.locator("#banner-meeting")
    expect(bar).to_be_visible()
    expect(bar).to_contain_text("Transcribing a call (Zoom)")
    leave = page.locator("#meeting-leave")
    expect(leave).to_have_text("Leave call")
    expect(leave).to_be_visible()

    line = page.locator("#log .line.k-meeting")
    expect(line).to_be_visible()
    expect(line.locator(".call-tag")).to_have_text("call")
    expect(line.locator(".nick-meeting")).to_have_text("bob")
    expect(line).to_contain_text("how much is gas")
    expect(line.locator(".avatar")).to_have_count(0)
    chat = page.locator("#log .line.k-chat:not(.k-meeting)")
    expect(chat).to_be_visible()
    meet_size = line.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
    chat_size = chat.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
    assert meet_size < chat_size

    light = bar.evaluate("el => getComputedStyle(el).backgroundColor")
    page.emulate_media(color_scheme="dark")
    expect(bar).to_contain_text("Transcribing a call (Zoom)")
    dark = bar.evaluate("el => getComputedStyle(el).backgroundColor")
    assert light != dark

    phone = _open(ui, calls, **PHONE)
    phone_bar = phone.locator("#banner-meeting")
    expect(phone_bar).to_be_visible()
    expect(phone_bar).to_contain_text("Transcribing a call (Zoom)")
    expect(phone.locator("#meeting-leave")).to_be_visible()
    expect(phone.locator("#log .line.k-meeting .call-tag")).to_have_text("call")
    fits = phone.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )
    assert fits

    leave.click()
    expect(page.locator("#input")).to_be_focused()
    expect(bar).to_be_hidden()
