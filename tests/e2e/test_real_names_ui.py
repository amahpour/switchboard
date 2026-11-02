"""People shown by their real name (issue #563, DESIGN.md §39.6), on a hosted broker.

Alice is the admin (Alice Ng, username alice). Bob is Bob Lee, and his username comes from
his email. Carol has no first or last name, so the page shows her username. Marker ``e2e``,
opt-in; the ``UI`` fixture watches every page for console errors, page errors and CSP
violations.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHASE_REPORTS, PHONE, UI, chat_row, no_horizontal_scroll
from ui_world import HostedWorld

pytestmark = pytest.mark.e2e

ADMIN_PW = "correct horse battery"
BOB_PW = "bob's own secret 1"
CAROL_PW = "carol's own secret 1"
LONG_PW = "correct horse longname"
LONG_LAST = "Bartholomew" * 5  # one word, long enough to widen a phone if a name cannot wrap


def _client(origin: str, cookie: str | None = None) -> httpx.Client:
    cookies = {"switchboard_session": cookie} if cookie else None
    return httpx.Client(base_url=origin, headers={"Origin": origin, "X-Switchboard": "1"}, cookies=cookies)


def _sign_in_and_say(origin: str, email: str, one_time: str, password: str, text: str) -> None:
    with _client(origin) as c:
        signed = c.post("/api/signin/password", json={"name": email, "password": one_time})
        assert signed.status_code == 200, signed.text
        chosen = c.post("/api/me/password", json={"password": password})
        assert chosen.status_code == 200, chosen.text
        said = c.post("/api/rooms/build/say", json={"text": text})
        assert said.status_code == 200, said.text


@pytest.fixture(scope="module")
def world(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[HostedWorld]:
    """A hosted broker: alice (Alice Ng), bob (Bob Lee, username from his email) and carol."""
    del playwright  # the driver starts first, under the real HOME, as the other e2e fixtures
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        hosted = HostedWorld().start()
        origin = hosted.origin
        one_time = hosted.claim_link().split("#t=", 1)[1]
        with _client(origin) as c:
            signed = c.post("/api/signin/password", json={"name": "admin", "password": one_time})
            assert signed.status_code == 200, signed.text
            setup = c.post(
                "/api/setup/password",
                json={
                    "password": ADMIN_PW,
                    "email": "alice@example.com",
                    "first_name": "Alice",
                    "last_name": "Ng",
                },
            )
            assert setup.status_code == 200, setup.text
            bob = c.post(
                "/api/people",
                json={"first_name": "Bob", "last_name": "Lee", "email": "bob.lee@example.com"},
            )
            assert bob.status_code == 200 and bob.json()["person"]["name"] == "bob-lee", bob.text
            carol = c.post("/api/people", json={"name": "carol", "email": "carol@example.com"})
            assert carol.status_code == 200, carol.text
            admin = c.cookies.get("switchboard_session")
        assert admin
        _sign_in_and_say(origin, "bob.lee@example.com", bob.json()["password"], BOB_PW, "ships Friday")
        _sign_in_and_say(origin, "carol@example.com", carol.json()["password"], CAROL_PW, "from carol")
        hosted.admin_cookie = admin  # type: ignore[attr-defined]
        try:
            yield hosted
        finally:
            hosted.stop()


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


def page_for(ui: UI, world: HostedWorld, **options: Any) -> Page:
    ctx = ui.context(**options)
    ctx.add_cookies(
        [{"name": "switchboard_session", "value": world.admin_cookie, "url": world.origin}]  # type: ignore[attr-defined]
    )
    page = ctx.new_page()
    page.goto(world.origin + "/")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    tab = page.locator('#tabs .room[data-room="#build"]')
    if page.locator("#rooms-toggle").is_visible() and page.locator("#app.nav-open").count() == 0:
        page.click("#rooms-toggle")
    tab.click()
    expect(page.locator("#room-title")).to_have_text("build")
    if page.locator("#app.nav-open").count():
        page.keyboard.press("Escape")
    return page


def test_a_person_is_shown_by_their_real_name(ui: UI, world: HostedWorld) -> None:
    """#563: Bob's message leads with Bob Lee and the hover is @bob-lee; Carol, who has no
    names, stays carol. The People list and the You row do the same. The @ popover shows the
    real name and inserts the username. Add someone fills the username from the email and
    stops when one is typed. Renaming Bob's first name shows on the open page. Fails if the
    sender is still the username, or if the username still follows the first name."""
    page = page_for(ui, world)
    bob = chat_row(page, "ships Friday")
    expect(bob).to_have_attribute("data-from", "bob-lee")
    expect(bob.locator(".nick")).to_have_text("Bob Lee")
    expect(bob.locator(".nick")).to_have_attribute("title", "@bob-lee")
    carol = chat_row(page, "from carol")
    expect(carol.locator(".nick")).to_have_text("carol")
    assert carol.locator(".nick").get_attribute("title") in (None, "")

    expect(page.locator("#buddy-me .m-name")).to_have_text("Alice Ng")
    expect(page.locator("#buddy-me .m-full")).to_have_text("@alice")
    expect(page.locator("#me-name")).to_have_text("alice")  # the footer stays the username
    bob_row = page.locator('#room-people .person-row[data-name="bob-lee"]')
    expect(bob_row.locator(".m-name")).to_have_text("Bob Lee")
    expect(bob_row.locator(".m-full")).to_have_text("@bob-lee")
    carol_row = page.locator('#room-people .person-row[data-name="carol"]')
    expect(carol_row.locator(".m-name")).to_have_text("carol")
    expect(carol_row.locator(".m-full")).to_have_count(0)

    box = page.locator("#input")
    box.click()
    page.keyboard.type("@lee")
    opt = page.locator("#men-bob-lee")
    expect(opt).to_be_visible()
    expect(opt.locator(".m-name")).to_have_text("Bob Lee")
    expect(opt.locator(".m-full")).to_have_text("@bob-lee")
    page.keyboard.press("Enter")
    expect(box).to_have_value("@bob-lee ")
    box.fill("")

    page.click("#open-people")
    expect(page.locator("#person-name")).to_have_attribute("placeholder", "sam-lee")
    page.fill("#person-first", "Sam")
    page.fill("#person-last", "Lee")
    expect(page.locator("#person-name")).to_have_value("")
    page.fill("#person-email", "sam.lee@example.com")
    expect(page.locator("#person-name")).to_have_value("sam-lee")
    page.fill("#person-name", "chosen")
    page.fill("#person-email", "other.person@example.com")
    expect(page.locator("#person-name")).to_have_value("chosen")
    page.click("#people-close")

    origin = world.origin
    with _client(origin, world.admin_cookie) as c:  # type: ignore[attr-defined]
        people = c.get("/api/people")
        assert people.status_code == 200, people.text
        pid = next(p["id"] for p in people.json()["people"] if p["name"] == "bob-lee")
        renamed = c.post(f"/api/people/{pid}/name", json={"first_name": "Robert"})
        assert renamed.status_code == 200, renamed.text
    expect(bob.locator(".nick")).to_have_text("Robert Lee")
    expect(bob.locator(".nick")).to_have_attribute("title", "@bob-lee")
    expect(bob_row.locator(".m-name")).to_have_text("Robert Lee")
    expect(bob_row.locator(".m-full")).to_have_text("@bob-lee")
    box.click()
    page.keyboard.type("@robert")  # only the new real name matches; the username is still bob-lee
    expect(opt.locator(".m-name")).to_have_text("Robert Lee")
    page.keyboard.press("Enter")
    expect(box).to_have_value("@bob-lee ")


def test_a_long_real_name_does_not_widen_a_phone(ui: UI, world: HostedWorld) -> None:
    """#563: a person's real name can be one long word. On a phone it wraps, in the log and
    in the Members sheet, instead of widening the page. Fails if the sender's name cannot break."""
    origin = world.origin
    with _client(origin, world.admin_cookie) as c:  # type: ignore[attr-defined]
        added = c.post(
            "/api/people",
            json={
                "first_name": "Bo",
                "last_name": LONG_LAST,
                "email": "long.person@example.com",
            },
        )
        assert added.status_code == 200, added.text
        assert added.json()["person"]["name"] == "long-person"
        one_time = added.json()["password"]
    _sign_in_and_say(origin, "long.person@example.com", one_time, LONG_PW, "a short note")
    page = page_for(ui, world, **PHONE)
    nick = chat_row(page, "a short note").locator(".nick")
    expect(nick).to_have_text("Bo " + LONG_LAST)
    page.click("#buddy-toggle")
    expect(page.locator('#room-people .person-row[data-name="long-person"] .m-name')).to_have_text(
        "Bo " + LONG_LAST
    )
    no_horizontal_scroll(page)
    # the name wraps inside the log and the People row. The pane's track is two views wide on
    # purpose (Members and the Inspector), so it is not part of this check.
    boxes = page.evaluate(
        """() => [...document.querySelectorAll('#log, #room-people, #log .nick, #room-people .m-name')]
            .map(n => [n.id || n.className, n.scrollWidth, n.clientWidth])"""
    )
    assert boxes and all(scroll_w <= client_w + 1 for _sel, scroll_w, client_w in boxes), boxes
