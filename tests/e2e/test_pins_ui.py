"""Pinned decisions in a real browser (#292, DESIGN.md §48): Pin and Unpin on a message, the strip
under the room header with a link to each pinned message and Unpin, the cap of five, a second page
following live, and the keyboard. A hosted broker of its own (a signed-in admin, no agents running:
the one agent message is posted by a stand-in that has left). Marker ``e2e``."""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from fakes.fake_agent import FakeAgent
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHASE_REPORTS, PHONE, UI

from switchboard.broker.auth import WebOrigin

pytestmark = pytest.mark.e2e

ADMIN_PW = "correct horse battery"


@pytest.fixture(scope="module")
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[InProcBroker, str]]:
    """A hosted broker with its admin signed in through the API: the broker and the session cookie."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://sb.localhost:{port}")).start()
        origin = b.state.web_origin.origin
        one_time = b.paths.test_claim_link.read_text().strip().split("#t=", 1)[1]
        h = {"Origin": origin, "X-Switchboard": "1"}
        with httpx.Client(base_url=origin, headers=h) as c:
            assert (
                c.post("/api/signin/password", json={"name": "admin", "password": one_time}).status_code
                == 200
            )
            assert c.post("/api/setup/password", json={"password": ADMIN_PW}).status_code == 200
            cookie = c.cookies.get("switchboard_session")
        assert cookie
        try:
            yield b, cookie
        finally:
            b.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def ui(browser: Browser, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, None)  # type: ignore[arg-type]  # no seeded world: the pages are opened by hand
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


class Room:
    """A fresh room of its own in the shared hosted broker, with messages as ids."""

    def __init__(self, hosted: tuple[InProcBroker, str], name: str) -> None:
        self.b, self.cookie = hosted
        self.name = "#" + name
        self.slug = name
        self.origin = self.b.state.web_origin.origin
        self.headers = {"Origin": self.origin, "X-Switchboard": "1"}
        with self.client() as c:
            assert c.post("/api/rooms", json={"name": self.name}).status_code == 200

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.origin, headers=self.headers, cookies={"switchboard_session": self.cookie}
        )

    def say(self, text: str) -> int:
        with self.client() as c:
            r = c.post(f"/api/rooms/{self.slug}/say", json={"text": text})
        assert r.status_code == 200, r.text
        return int(r.json()["id"])

    def agent_says(self, name: str, text: str) -> int:
        async def go() -> int:
            async with FakeAgent(self.b.home, f"k-{self.slug}-{name}") as a:
                assert (await a.join(self.name, name))["ok"]
                return int((await a.say(self.name, text))["posted_id"])

        with ThreadPoolExecutor(1) as pool:  # the page's own thread already runs a loop
            return pool.submit(asyncio.run, go()).result(30)

    def pins(self) -> list[int]:
        with self.client() as c:
            rooms = c.get("/api/rooms").json()["rooms"]
        return [p["id"] for p in next(r for r in rooms if r["slug"] == self.slug)["pins"]]


def open_page(ui: UI, room: Room, **options: Any) -> Page:
    ctx = ui.context(**options)
    ctx.add_cookies([{"name": "switchboard_session", "value": room.cookie, "url": room.origin}])
    page = ctx.new_page()
    page.goto(room.origin + "/")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    if not page.locator(f'#tabs .room[data-room="{room.name}"]').is_visible():
        page.click("#rooms-toggle")
    page.locator(f'#tabs .room[data-room="{room.name}"]').click()
    expect(page.locator("#room-title")).to_have_text(room.slug)
    return page


def row(page: Page, mid: int) -> Any:
    return page.locator(f'#log .line.k-chat[data-id="{mid}"]')


def pin_button(page: Page, mid: int) -> Any:
    return row(page, mid).locator(".msg-pin")


def opacity(loc: Any) -> float:
    return float(loc.evaluate("e => getComputedStyle(e).opacity"))


@pytest.mark.parametrize("phone", [False, True])
def test_a_person_pins_from_a_message_and_manages_the_pins_from_the_strip(
    ui: UI, hosted: tuple[InProcBroker, str], phone: bool
) -> None:
    """Pin on a message puts it in the strip under the header (its sender and text, as the newest
    pin), and Unpin takes it out, from either place; the strip's link jumps to the message and
    flashes it; the sixth pin says to unpin one first and pins nothing; keyboard focus lands
    somewhere sensible when what had it goes (the Unpin that was clicked, the last pin). Fails if a
    pin isn't shown, the buttons and the strip disagree, or focus is dropped. The phone: nothing
    scrolls sideways and the pin button can be seen without a hover."""
    room = Room(hosted, "decide-phone" if phone else "decide")
    ids = [room.say(f"decision {i}: we ship on friday") for i in range(5)]
    said = room.agent_says("planner", "use sqlite, not postgres")
    page = open_page(ui, room, **(PHONE if phone else {}))
    expect(row(page, said)).to_be_visible()
    strip = page.locator("#pins")
    expect(strip).to_be_hidden()  # no pins: no strip
    # a pin's button is there, off until the row is hovered or focused (a phone has no hover)
    button = pin_button(page, said)
    expect(button).to_have_attribute("aria-pressed", "false")
    if phone:
        assert opacity(button) > 0.4
    else:
        assert opacity(button) == 0
        row(page, said).hover()
        expect(button).to_have_css("opacity", "1")

    # Pin: the agent's message is the strip's only pin; the row says so
    button.click()
    expect(page.locator("#pins-count")).to_have_text("Pinned (1)")
    expect(page.locator("#pins-peek")).to_have_text("planner: use sqlite, not postgres")
    expect(button).to_have_attribute("aria-pressed", "true")
    expect(row(page, said)).to_have_class(re.compile(r"\bis-pinned\b"))
    assert room.pins() == [said]
    expect(page.locator("#log")).to_contain_text(f"pinned message {said} (planner's)")
    toggle = page.locator("#pins-toggle")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#pins-list")).to_be_hidden()
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    pin_row = page.locator(f'#pins-list .pin-row[data-id="{said}"]')
    expect(pin_row.locator(".pin-from")).to_have_text("planner")
    expect(pin_row.locator(".pin-text")).to_have_text("use sqlite, not postgres")
    expect(pin_row.locator(".pin-unpin")).to_have_accessible_name(f"Unpin message {said}")
    # the link goes to the message and flashes it
    page.evaluate("document.getElementById('log').scrollTop = 0")
    pin_row.locator(".pin-link").click()
    expect(row(page, said)).to_have_class(re.compile(r"\bflash\b"))
    # a keyboard user pins the first message: the button takes Tab focus, shows itself, and Enter
    # pins without taking the focus away
    first = pin_button(page, ids[0])
    first.focus()
    assert opacity(first) == 1
    page.keyboard.press("Enter")
    expect(page.locator("#pins-count")).to_have_text("Pinned (2)")
    expect(first).to_have_attribute("aria-pressed", "true")
    expect(first).to_be_focused()

    # at most five: three more fill it, the sixth is refused in the log and pins nothing
    for mid in ids[1:4]:
        pin_button(page, mid).click()
    expect(page.locator("#pins-count")).to_have_text("Pinned (5)")
    pin_button(page, ids[4]).click()
    expect(page.locator("#log .line.local.error").last).to_contain_text(
        "has 5 pinned messages already: unpin one first"
    )
    expect(pin_button(page, ids[4])).to_have_attribute("aria-pressed", "false")
    assert sorted(room.pins()) == sorted([said, *ids[:4]])
    # the browser logs the refused pin's 409
    page.wait_for_timeout(100)
    assert any("409" in p and "/command" in p for p in ui.problems), ui.problems
    ui.problems.clear()

    # Unpin from the strip: the row goes, the buttons follow, and the focus goes to the strip's
    # own button, not the page
    page.locator(f'#pins-list .pin-row[data-id="{said}"] .pin-unpin').click()
    expect(page.locator("#pins-count")).to_have_text("Pinned (4)")
    expect(pin_button(page, said)).to_have_attribute("aria-pressed", "false")
    expect(page.locator(f'#pins-list .pin-row[data-id="{said}"]')).to_have_count(0)
    expect(toggle).to_be_focused()
    # and from the message: the last pins go, and the strip with them
    for mid in ids[:4]:
        pin_button(page, mid).click()
        expect(pin_button(page, mid)).to_have_attribute("aria-pressed", "false")
    expect(strip).to_be_hidden()
    assert room.pins() == []
    if phone:
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_typing_pin_works_and_every_page_follows(ui: UI, hosted: tuple[InProcBroker, str]) -> None:
    """``/pin <id>`` from the composer is the same pin (the #number is on the message), a second
    page sees it without reloading, and a page opened after loads it. Fails if a page has to be
    reloaded, or a pin made in one page is missing from another."""
    room = Room(hosted, "decide-live")
    mid = room.say("the cache is per process")
    other_id = room.say("another message")
    a = open_page(ui, room)
    b = open_page(ui, room)
    expect(row(a, mid).locator(".msg-number")).to_have_text(f"#{mid}")
    a.fill("#input", f"/pin {mid}")
    a.keyboard.press("Enter")
    expect(b.locator("#pins-count")).to_have_text("Pinned (1)")  # b was never touched
    expect(pin_button(b, mid)).to_have_attribute("aria-pressed", "true")
    expect(pin_button(b, other_id)).to_have_attribute("aria-pressed", "false")
    late = open_page(ui, room)  # opened after
    expect(late.locator("#pins-count")).to_have_text("Pinned (1)")
    late.fill("#input", f"/unpin {mid}")
    late.keyboard.press("Enter")
    expect(a.locator("#pins")).to_be_hidden()
    expect(b.locator("#pins")).to_be_hidden()
    expect(pin_button(a, mid)).to_have_attribute("aria-pressed", "false")
