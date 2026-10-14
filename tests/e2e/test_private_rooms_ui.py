"""Private rooms in the page (#74, DESIGN.md §44), on a hosted broker: the New room dialog's
Private box, the lock in the sidebar, the room's people with Add and Leave, and the admin's
list of private rooms in People. A public room lists everyone on the broker, with who is here
now (#354)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHASE_REPORTS, PHONE, UI

from switchboard.broker.auth import WebOrigin

pytestmark = pytest.mark.e2e

ADMIN_PW = "correct horse battery"


@pytest.fixture(scope="module")
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[InProcBroker, str]]:
    """A hosted broker set up through its API: the admin (alice) with a password, bob added,
    and the admin's session cookie for the pages."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://sb.localhost:{port}")).start()
        b.on_loop(lambda: b.state.service.create_room("#build"))
        origin = b.state.web_origin.origin
        one_time = b.paths.test_claim_link.read_text().strip().split("#t=", 1)[1]
        h = {"Origin": origin, "X-Switchboard": "1"}
        with httpx.Client(base_url=origin, headers=h) as c:
            assert (
                c.post("/api/signin/password", json={"name": "admin", "password": one_time}).status_code
                == 200
            )
            assert c.post("/api/setup/password", json={"password": ADMIN_PW}).status_code == 200
            assert c.post("/api/people", json={"name": "bob"}).status_code == 200
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


def admin_page(ui: UI, hosted: tuple[InProcBroker, str], **options: Any) -> Page:
    b, cookie = hosted
    origin = b.state.web_origin.origin
    ctx = ui.context(**options)
    ctx.add_cookies([{"name": "switchboard_session", "value": cookie, "url": origin}])
    page = ctx.new_page()
    page.goto(origin + "/")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    return page


def test_a_private_room_shows_its_lock_and_its_people(ui: UI, hosted: tuple[InProcBroker, str]) -> None:
    page = admin_page(ui, hosted)
    page.click("#new-room")
    page.fill("#app-dialog-name", "secret")
    page.check("#app-dialog-private")
    page.click("#app-dialog-action")
    tab = page.locator('#tabs .room[data-room="#secret"]')
    expect(tab.locator("svg.ic-lock")).to_have_count(1)
    lock = page.evaluate("() => document.querySelector('.head-hash path').getAttribute('d')")
    assert lock.startswith("M5"), lock  # the header's glyph is the lock too
    people = page.locator("#room-people")
    expect(people.locator(".group-label")).to_have_text("People (1) · private")
    expect(people.locator('.person-row[data-name="alice"]')).to_contain_text("Leave")
    page.click("#room-people-add")
    pick = page.locator("#app-dialog-person")
    expect(pick).to_be_focused()
    expect(pick.locator("option")).to_have_text(["bob"])
    page.click("#app-dialog-action")
    expect(people.locator(".group-label")).to_have_text("People (2) · private")
    expect(people.locator('.person-row[data-name="bob"]')).to_contain_text("Remove")
    # a public room has no people list: everyone signed in sees it
    page.locator('#tabs .room[data-room="#build"]').click()
    # a public room lists people too (#354), without the private room's actions
    expect(people.locator(".group-label")).not_to_contain_text("private")
    expect(people.locator("button")).to_have_count(0)
    expect(page.locator('#tabs .room[data-room="#build"] svg.ic-hash')).to_have_count(1)
    # the admin's list: names and people
    page.click("#open-people")
    card = page.locator("#private-rooms")
    expect(card.locator('li[data-room="#secret"]')).to_have_text("#secret · alice, bob")


def person_cookie(hosted: tuple[InProcBroker, str], name: str) -> str:
    """Add ``name`` as the admin, sign in as them with the one-time password, choose a password,
    and hand back their session cookie."""
    b, admin = hosted
    origin = b.state.web_origin.origin
    h = {"Origin": origin, "X-Switchboard": "1"}
    with httpx.Client(base_url=origin, headers=h, cookies={"switchboard_session": admin}) as c:
        added = c.post("/api/people", json={"name": name, "email": f"{name}@example.com"})
        assert added.status_code == 200, added.text
        one_time = added.json()["password"]
    with httpx.Client(base_url=origin, headers=h) as c:
        signed = c.post("/api/signin/password", json={"name": f"{name}@example.com", "password": one_time})
        assert signed.status_code == 200, signed.text
        chosen = c.post("/api/me/password", json={"password": "correct horse " + name})
        assert chosen.status_code == 200, chosen.text
        cookie = c.cookies.get("switchboard_session")
    assert cookie
    return cookie


@pytest.mark.parametrize("phone", [False, True])
def test_a_public_room_lists_people_and_who_is_here(
    ui: UI, hosted: tuple[InProcBroker, str], phone: bool
) -> None:
    """#354: a public room showed You and the agents, and nobody else: the other people on the
    broker were only names on messages. It now lists everyone signed up (everyone sees a public
    room), each with a dot for a page open now, those people first, and none of a private room's
    Add, Remove or Leave. The list follows people as they open and close the page. Fails if a
    public room shows no people, or if the dot doesn't follow a second person's page."""
    b, _admin = hosted
    name = "carol" + ("p" if phone else "")
    cookie = person_cookie(hosted, name)
    page = admin_page(ui, hosted, **(PHONE if phone else {}))
    if phone:
        page.click("#rooms-toggle")
    page.locator('#tabs .room[data-room="#build"]').click()
    if phone:
        page.click("#buddy-toggle")
    people = page.locator("#room-people")
    expect(people).to_be_visible()
    expect(people.locator(".group-label")).to_contain_text("People (")
    expect(people.locator("button")).to_have_count(0)  # nothing to add or remove: everyone sees it
    me = people.locator('.person-row[data-name="alice"]')
    expect(me.locator(".p-dot.on")).to_have_count(1)
    expect(me).to_contain_text("you")
    other = people.locator(f'.person-row[data-name="{name}"]')
    expect(other.locator(".p-dot.off")).to_have_count(1)
    expect(other.locator(".p-dot")).to_have_attribute("aria-label", "offline")
    assert people.locator(".person-row").first.get_attribute("data-name") == "alice"  # here: first
    # the other person opens the page: their dot turns on here, and they move up with the present
    theirs = ui.context()
    theirs.add_cookies([{"name": "switchboard_session", "value": cookie, "url": b.state.web_origin.origin}])
    their_page = theirs.new_page()
    their_page.goto(b.state.web_origin.origin + "/")
    expect(their_page.locator("#st-conn")).to_have_text("Connected")
    expect(other.locator(".p-dot.on")).to_have_count(1)
    expect(other.locator(".p-dot")).to_have_attribute("aria-label", "online")
    present = people.locator(".person-row:has(.p-dot.on)")
    expect(present).to_have_count(2)
    # and their own page lists them as you, with alice here too
    their_page.locator('#tabs .room[data-room="#build"]').click()
    expect(their_page.locator(f'#room-people .person-row[data-name="{name}"]')).to_contain_text("you")
    expect(their_page.locator('#room-people .person-row[data-name="alice"] .p-dot.on')).to_have_count(1)
    # they close it: the dot goes off here
    their_page.close()
    expect(other.locator(".p-dot.off")).to_have_count(1)
    if phone:
        width = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert width <= 0


def test_the_room_people_fit_a_phone(ui: UI, hosted: tuple[InProcBroker, str]) -> None:
    b, _cookie = hosted
    b.on_loop(lambda: b.state.service.create_room("#tiny", person_id=None, private=True))
    page = admin_page(ui, hosted, **PHONE)
    page.click("#rooms-toggle")
    page.locator('#tabs .room[data-room="#tiny"]').click()
    page.click("#buddy-toggle")
    expect(page.locator("#room-people .person-row")).to_have_count(1)
    width = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert width <= 0
