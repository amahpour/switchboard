"""An artifact line in a real browser (issue #544, DESIGN.md §29.4).

Posting the line shows a card. Its link is a real anchor through the page's one link path, and
it opens that URL. Light, dark and a phone width all show the card. Marker ``e2e``.
"""

from __future__ import annotations

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
SHA = "0123456789abcdef" * 4
URL = "https://files.example.com/out/report.zip"
LINE = f"artifact: report.zip | 12 KB | sha256:{SHA} | {URL}"


@pytest.fixture(scope="module")
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[InProcBroker, str]]:
    """A hosted broker with its admin signed in. No agents: the message is posted from the page."""
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
    u = UI(browser, None)  # type: ignore[arg-type]  # no seeded world: the page is opened by hand
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def open_room(ui: UI, hosted: tuple[InProcBroker, str], **options: Any) -> Page:
    b, cookie = hosted
    origin = b.state.web_origin.origin
    ctx = ui.context(**options)
    ctx.add_cookies([{"name": "switchboard_session", "value": cookie, "url": origin}])
    page = ctx.new_page()
    page.goto(origin + "/#files")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    if not page.locator('#tabs .room[data-room="#files"]').is_visible():
        page.click("#rooms-toggle")
    page.locator('#tabs .room[data-room="#files"]').click()
    expect(page.locator("#room-title")).to_have_text("files")
    return page


def card_background(page: Page) -> str:
    return page.locator("#log .md-artifact").evaluate("el => getComputedStyle(el).backgroundColor")


def test_a_posted_artifact_line_shows_as_a_card_with_a_working_link(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """Sending the artifact line shows a card whose link opens that URL in a new tab. The card
    uses the pre-bg token in light and in dark, and it fits a phone without sideways scroll.
    Fails if the line stays plain text, if the anchor's href is not the URL, or if the card
    paints a colour that is not the theme token."""
    page = open_room(ui, hosted, color_scheme="light")
    page.fill("#input", LINE)
    page.keyboard.press("Enter")
    card = page.locator("#log .md-artifact")
    expect(card).to_be_visible()
    expect(card.locator(".md-artifact-name")).to_have_text("report.zip")
    expect(card.locator(".md-artifact-size")).to_have_text("12 KB")
    expect(card.locator(".md-artifact-sha")).to_contain_text(SHA[:12])
    link = card.locator("a.md-link")
    expect(link).to_have_attribute("href", URL)
    expect(link).to_have_text("files.example.com")
    assert link.get_attribute("rel") == "noopener noreferrer nofollow"
    assert link.get_attribute("target") == "_blank"
    expect(card.locator(".md-url")).to_have_text(URL)
    light = card_background(page)
    token = page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--pre-bg').trim()"
    )
    assert light == rgb(token) == "rgb(247, 247, 249)"

    def fulfill(route: Any) -> None:
        route.fulfill(status=200, content_type="text/html", body="<p>artifact fetched</p>")

    page.context.route(URL, fulfill)
    with page.expect_popup() as popped:
        link.click()
    popup = popped.value
    expect(popup.locator("p")).to_have_text("artifact fetched")
    assert popup.url == URL
    popup.close()

    dark = open_room(ui, hosted, color_scheme="dark")
    expect(dark.locator("#log .md-artifact a.md-link")).to_have_attribute("href", URL)
    dark_bg = card_background(dark)
    dark_token = dark.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--pre-bg').trim()"
    )
    assert dark_bg == rgb(dark_token) == "rgb(38, 38, 42)"
    assert dark_bg != light

    phone = open_room(ui, hosted, color_scheme="light", **PHONE)
    expect(phone.locator("#log .md-artifact")).to_be_visible()
    expect(phone.locator("#log .md-artifact a.md-link")).to_have_attribute("href", URL)
    assert phone.locator("#log .md-artifact").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    no_horizontal_scroll(phone)
