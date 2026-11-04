"""Attaching a file, and how long files are kept (issue #544, DESIGN.md §53).

The attach button and a drop post a card whose link downloads. An admin can shorten
retention and is warned first. Someone who is not an admin does not see that group.
Light, dark and a phone width. Marker ``e2e``.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHASE_REPORTS, PHONE, UI, no_horizontal_scroll, rgb

from switchboard.broker.auth import WebOrigin

pytestmark = pytest.mark.e2e

ADMIN_PW = "correct horse battery"
BOB_PW = "bob has his own 1"


@pytest.fixture(scope="module")
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[InProcBroker, str]]:
    """A hosted broker with its admin signed in. No agents: the file is posted from the page."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://sb.localhost:{port}")).start()
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
            assert client.post("/api/rooms", json={"name": "#files"}).status_code == 200
        try:
            yield b, cookie
        finally:
            b.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def ui(browser: Browser, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, None)  # type: ignore[arg-type]
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def open_room(ui: UI, hosted: tuple[InProcBroker, str], cookie: str | None = None, **options: Any) -> Page:
    b, admin_cookie = hosted
    origin = b.state.web_origin.origin
    ctx = ui.context(**options)
    ctx.add_cookies([{"name": "switchboard_session", "value": cookie or admin_cookie, "url": origin}])
    page = ctx.new_page()
    page.goto(origin + "/#files")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    if not page.locator('#tabs .room[data-room="#files"]').is_visible():
        page.click("#rooms-toggle")
    page.locator('#tabs .room[data-room="#files"]').click()
    expect(page.locator("#room-title")).to_have_text("files")
    return page


def open_files_settings(page: Page) -> None:
    page.click("#me-settings")
    tab = page.locator("#settings-tab-files")
    if tab.is_visible():
        tab.click()
    else:
        page.locator("#settings-row-files").click()
    expect(page.locator("#settings-files-days")).to_be_visible()


def test_attach_and_drop_post_a_card_that_downloads(ui: UI, hosted: tuple[InProcBroker, str]) -> None:
    """The button and a drop each post a card. Its link downloads the bytes, as an attachment.
    The card uses the theme's pre-bg in light and in dark, and it fits a phone. Fails if the
    line stays plain text, if the href is not this origin's file route, or if the button is
    hidden on a phone."""
    b, _cookie = hosted
    origin = b.state.web_origin.origin
    page = open_room(ui, hosted, color_scheme="light")
    expect(page.locator("#attach-btn")).to_be_visible()
    page.locator("#attach-file").set_input_files(
        {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"hello from the page"}
    )
    card = page.locator("#log .md-artifact").first
    expect(card).to_be_visible()
    expect(card.locator(".md-artifact-name")).to_have_text("notes.txt")
    link = card.locator("a.md-file")
    expect(link).to_have_attribute("href", re.compile(r"^/api/files/[0-9a-f]{32}$"))
    href = link.get_attribute("href")
    assert href
    got = page.request.get(origin + href)
    assert got.ok and got.body() == b"hello from the page"
    assert got.headers["content-disposition"].startswith("attachment;")
    assert got.headers["content-type"].split(";")[0] == "application/octet-stream"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in got.headers["cache-control"]
    light = card.evaluate("el => getComputedStyle(el).backgroundColor")
    token = page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--pre-bg').trim()"
    )
    assert light == rgb(token)

    page.evaluate(
        """() => {
          const dt = new DataTransfer();
          dt.items.add(new File(['dropped'], 'dropped.txt', {type: 'text/plain'}));
          document.querySelector('#log').dispatchEvent(
            new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: dt}));
        }"""
    )
    expect(page.locator("#log .md-artifact")).to_have_count(2)
    expect(page.locator("#log .md-artifact").nth(1).locator(".md-artifact-name")).to_have_text("dropped.txt")

    page.emulate_media(color_scheme="dark")
    dark = card.evaluate("el => getComputedStyle(el).backgroundColor")
    dark_token = page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--pre-bg').trim()"
    )
    assert dark == rgb(dark_token)
    assert dark != light

    phone = open_room(ui, hosted, **PHONE)
    expect(phone.locator("#log .md-artifact").first).to_be_visible()
    expect(phone.locator("#attach-btn")).to_be_visible()
    expect(phone.locator("#mention-btn")).to_be_hidden()
    no_horizontal_scroll(phone)

    open_files_settings(page)
    expect(page.locator("#settings-files-days")).to_have_value("30")
    page.fill("#settings-files-days", "7")
    page.click("#settings-files-save")
    expect(page.locator("#app-dialog-body")).to_have_text(
        "Files older than 7 days will be deleted at the next cleanup."
    )
    page.click("#app-dialog-action")
    expect(page.locator("#settings-files-days")).to_have_value("7")


def test_someone_who_is_not_an_admin_does_not_see_files_settings(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """Retention is an admin's setting. A person who is not an admin gets no Files group.
    Fails if the group is in the sheet, or if the admin's own sheet lost it."""
    b, cookie = hosted
    origin = b.state.web_origin.origin
    admin = open_room(ui, hosted)
    open_files_settings(admin)
    headers = {"Origin": origin, "X-Switchboard": "1", "Cookie": f"switchboard_session={cookie}"}
    with httpx.Client(base_url=origin, headers=headers) as client:
        added = client.post("/api/people", json={"name": "bob", "email": "bob@example.com"})
        assert added.status_code == 200, added.text
        one_time = added.json()["password"]
    with httpx.Client(base_url=origin, headers={"Origin": origin, "X-Switchboard": "1"}) as bob:
        assert (
            bob.post(
                "/api/signin/password", json={"name": "bob@example.com", "password": one_time}
            ).status_code
            == 200
        )
        assert bob.post("/api/me/password", json={"password": BOB_PW}).status_code == 200
        bob_cookie = bob.cookies.get("switchboard_session")
        assert bob_cookie
    page = open_room(ui, hosted, cookie=bob_cookie)
    page.click("#me-settings")
    expect(page.locator("#settings-tab-profile")).to_be_visible()
    expect(page.locator("#settings-tab-files")).to_have_count(0)
    expect(page.locator("#settings-row-files")).to_have_count(0)
