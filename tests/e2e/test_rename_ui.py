"""Renaming in a real browser (#114, DESIGN.md §41): you rename yourself in Settings, the admin
renames someone from their People card, and each room says so. A hosted broker of its own, so
the renames touch no other test's world. Marker ``e2e``."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from playwright.sync_api import Browser, expect
from test_people_ui import ADMIN_PW, choose, sign_in
from test_private_rooms_ui import admin_page
from test_web_ui import PHASE_REPORTS, PHONE, UI

from switchboard.broker.auth import WebOrigin

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def room_hosted(
    playwright: Any, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[InProcBroker, str]]:
    """A signed-in browser for the room title tests, separate from the person-rename world."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://sb.localhost:{port}")).start()
        b.on_loop(lambda: b.state.service.create_room("#build"))
        origin = b.state.web_origin.origin
        one_time = b.paths.test_claim_link.read_text().strip().split("#t=", 1)[1]
        headers = {"Origin": origin, "X-Switchboard": "1"}
        with httpx.Client(base_url=origin, headers=headers) as client:
            assert (
                client.post("/api/signin/password", json={"name": "admin", "password": one_time}).status_code
                == 200
            )
            assert client.post("/api/setup/password", json={"password": ADMIN_PW}).status_code == 200
            cookie = client.cookies.get("switchboard_session")
        assert cookie
        try:
            yield b, cookie
        finally:
            b.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.mark.parametrize("theme,phone", [("light", False), ("dark", False), ("light", True)])
def test_room_title_renames_with_keyboard_and_keeps_focus(
    ui: UI, room_hosted: tuple[InProcBroker, str], theme: str, phone: bool
) -> None:
    """Clicking the title opens a small dialog; Escape cancels and Enter renames in each view."""
    old = "#room-" + theme + ("-phone" if phone else "")
    new = old + "-new"
    hosted, _cookie = room_hosted
    hosted.on_loop(lambda: hosted.state.service.create_room(old))
    page = admin_page(ui, room_hosted, **(PHONE if phone else {}))
    if phone:
        page.click("#rooms-toggle")
    page.locator('#tabs .room[data-room="' + old + '"]').click()
    page.evaluate("theme => { document.documentElement.dataset.theme = theme; }", theme)
    title = page.locator("#room-title")
    title_button = page.locator("#room-title-button")
    expect(title).to_have_text(old[1:])
    if phone:
        rooms_box = page.locator("#rooms-toggle").bounding_box()
        title_box = title.bounding_box()
        members_box = page.locator("#buddy-toggle").bounding_box()
        assert rooms_box and title_box and members_box
        assert rooms_box["x"] + rooms_box["width"] <= title_box["x"]
        assert title_box["x"] + title_box["width"] <= members_box["x"]
    title_button.click()
    expect(page.locator("#app-dialog-name")).to_be_focused()
    page.keyboard.press("Escape")
    expect(title_button).to_be_focused()
    title_button.click()
    page.fill("#app-dialog-name", "bad name")
    expect(page.locator("#app-dialog-action")).to_be_disabled()
    page.fill("#app-dialog-name", "build")
    expect(page.locator("#app-dialog-action")).to_be_disabled()
    page.fill("#app-dialog-name", new)
    page.keyboard.press("Enter")
    expect(title).to_have_text(new[1:])
    expect(title_button).to_be_focused()
    expect(page.locator('#tabs .room[data-room="' + new + '"]')).to_have_count(1)
    assert page.evaluate("location.hash") == new
    # the composer names the room it posts to: it kept the old name until the page was reloaded
    expect(page.locator("#input")).to_have_attribute(
        "placeholder", re.compile("^Message " + re.escape(new) + " ")
    )
    final = new + "-cmd"
    page.fill("#input", "/rename " + final)
    page.keyboard.press("Enter")
    expect(title).to_have_text(final[1:])
    assert page.evaluate("location.hash") == final
    expect(page.locator("#input")).to_have_attribute(
        "placeholder", re.compile("^Message " + re.escape(final) + " ")
    )


@pytest.mark.parametrize("phone", [False, True])
def test_the_rename_dialog_opens_without_an_error(
    ui: UI, room_hosted: tuple[InProcBroker, str], phone: bool
) -> None:
    """The dialog opens with the room's own name in the box. It shared New room's check, which
    asks only whether some room has that name, so it opened saying "#x already exists. Pick
    another name." before anyone had typed (#334). The room's own name now shows nothing and
    leaves Rename disabled, however it is typed, and another room's name is refused as before.
    Fails if the room's own name counts as taken."""
    room = "#own-name" + ("-phone" if phone else "")
    hosted, _cookie = room_hosted
    hosted.on_loop(lambda: hosted.state.service.create_room(room))
    page = admin_page(ui, room_hosted, **(PHONE if phone else {}))
    if phone:
        page.click("#rooms-toggle")
    page.locator('#tabs .room[data-room="' + room + '"]').click()
    expect(page.locator("#room-title")).to_have_text(room[1:])
    page.locator("#room-title-button").click()
    name, error, rename = (page.locator("#app-dialog-" + part) for part in ("name", "error", "action"))
    expect(name).to_be_focused()
    expect(name).to_have_value(room)
    expect(error).to_be_empty()  # nobody has typed: nothing in red
    expect(rename).to_be_disabled()  # and nothing to save yet
    name.fill("build")
    expect(error).to_have_text("#build already exists. Pick another name.")
    expect(rename).to_be_disabled()
    name.fill(room[1:].upper())  # the same name without its # and in capitals is still this room
    expect(error).to_be_empty()
    expect(rename).to_be_disabled()
    page.keyboard.press("Enter")  # Enter on the unchanged name does nothing
    expect(page.locator("#app-dialog")).to_be_visible()
    name.fill(room + "-2")
    expect(error).to_be_empty()
    expect(rename).to_be_enabled()
    page.keyboard.press("Escape")
    expect(page.locator("#room-title")).to_have_text(room[1:])


@pytest.fixture(scope="module")
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://sb.localhost:{port}")).start()
        b.on_loop(lambda: b.state.service.create_room("#build"))
        try:
            yield b
        finally:
            b.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def ui(browser: Browser, hosted: InProcBroker, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, None)  # type: ignore[arg-type]  # no seeded world: the pages are opened by hand
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def test_rename_yourself_in_settings_and_someone_from_people(ui: UI, hosted: InProcBroker) -> None:
    """A late initial People fetch must not replace a rename input while it is being edited."""
    origin = hosted.state.web_origin.origin
    one_time = hosted.paths.test_claim_link.read_text().strip().split("#t=", 1)[1]
    page = ui.context().new_page()
    page.goto(origin + "/")
    sign_in(page, "admin", one_time)
    choose(page, ADMIN_PW, "alice@example.com")  # Alice Liddell
    page.click("#open-people")
    page.fill("#person-first", "Bob")
    page.fill("#person-last", "Builder")
    page.fill("#person-email", "bob@example.com")
    page.click("#person-add")
    page.click("#invite-done")
    page.keyboard.press("Escape")

    # Settings: your names, and your name in the rooms
    page.click("#me-settings")
    expect(page.locator("#settings-first")).to_have_value("Alice")
    expect(page.locator("#settings-last")).to_have_value("Liddell")
    expect(page.locator("#settings-name")).to_have_value("alice")
    page.fill("#settings-name", "Not A Name")
    page.click("#settings-name-save")
    expect(page.locator("#settings-name-status")).to_contain_text("A name looks like")
    page.fill("#settings-name", "ali")
    page.fill("#settings-last", "Liddell-Hart")
    page.click("#settings-name-save")  # fresh from the sign-in: no check asked
    expect(page.locator("#settings-name-status")).to_have_text("Saved")
    expect(page.locator("#me-name")).to_have_text("ali")
    expect(page.locator("#buddy-me .m-full")).to_have_text("Alice Liddell-Hart")
    page.keyboard.press("Escape")
    expect(page.locator("#log")).to_contain_text("alice is now ali")

    # People: the admin renames bob on his card
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = (...args) => {
        if (String(args[0]).endsWith('/api/people') &&
            (!args[1] || !args[1].method || args[1].method === 'GET')) {
          return new Promise(resolve => {
            window.releasePeople = () => { window.fetch = original; resolve(original(...args)); };
          });
        }
        return original(...args);
      };
    }""")
    page.click("#open-people")
    expect(page.locator("#person-first")).to_have_count(0)  # no form until the initial fetch settles
    page.evaluate("() => window.releasePeople()")
    expect(page.locator("#person-first")).to_be_visible()
    bob = page.locator(".person-card").nth(1)
    expect(bob.locator(".person-handle")).to_have_text("@bob")
    bob.locator('input[id^="name-"]').fill("rob")
    bob.locator('button[id^="name-save-"]').click()
    expect(bob.locator('[id^="name-result-"]')).to_have_text("Renamed")
    expect(bob.locator(".person-handle")).to_have_text("@rob")
    expect(page.locator(".person-card").nth(0).locator(".person-handle")).to_have_text("@ali")
    page.keyboard.press("Escape")
    expect(page.locator("#log")).to_contain_text("bob is now rob (renamed by ali)")
    assert [p.name for p in hosted.state.store.people()] == ["rob"] and hosted.state.cfg.human_name == "ali"
