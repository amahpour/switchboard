"""People shown by their real name (issue #563, DESIGN.md §39.6), on a hosted broker.

Alice is the admin (Alice Ng, username alice). Bob is Bob Lee, and his username comes from
his email. Carol has no first or last name, so the page shows her username. Marker ``e2e``,
opt-in; the ``UI`` fixture watches every page for console errors, page errors and CSP
violations.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHASE_REPORTS, PHONE, UI, chat_row, no_horizontal_scroll, open_settings
from ui_world import HostedWorld, wait_js

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
    bob_id = chat_row(page, "ships Friday").get_attribute("data-id")
    bob = page.locator(f'#log .line.k-chat[data-id="{bob_id}"]')  # a reply below quotes his text
    expect(bob).to_have_attribute("data-from", "bob-lee")
    expect(bob.locator(".nick")).to_have_text("Bob Lee")
    expect(bob.locator(".nick")).to_have_attribute("title", "@bob-lee")
    carol = chat_row(page, "from carol")
    expect(carol.locator(".nick")).to_have_text("carol")
    assert carol.locator(".nick").get_attribute("title") in (None, "")

    expect(page.locator("#buddy-me .m-name")).to_have_text("Alice Ng")
    expect(page.locator("#buddy-me .m-full")).to_have_text("@alice")
    expect(page.locator("#me-name")).to_have_text("Alice Ng")  # the footer, @alice on hover
    expect(page.locator("#me-name")).to_have_attribute("title", "@alice")
    expect(page.locator("#room-sub")).to_have_text("Alice Ng and 0 agents")
    bob_row = page.locator('#room-people .person-row[data-name="bob-lee"]')
    expect(bob_row.locator(".m-name")).to_have_text("Bob Lee")
    expect(bob_row.locator(".m-full")).to_have_text("@bob-lee")
    carol_row = page.locator('#room-people .person-row[data-name="carol"]')
    expect(carol_row.locator(".m-name")).to_have_text("carol")
    expect(carol_row.locator(".m-full")).to_have_count(0)

    # a reply to Bob, and a pin of his message, name him as his message does
    with _client(world.origin, world.admin_cookie) as c:  # type: ignore[attr-defined]
        said = c.post("/api/rooms/build/say", json={"text": "thanks", "reply_to": int(bob_id or 0)})
        assert said.status_code == 200, said.text
        pinned = c.post("/api/rooms/build/command", json={"text": f"/pin {bob_id}"})
        assert pinned.status_code == 200, pinned.text
    expect(chat_row(page, "thanks").locator(".reply-nick")).to_have_text("Bob Lee")
    expect(page.locator("#pins-peek")).to_contain_text("Bob Lee: ships Friday")

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
        """() => [...document.querySelectorAll('#log, #room-people, #log .nick, #room-people .p-names')]
            .map(n => [n.id || n.className, n.scrollWidth, n.clientWidth])"""
    )
    assert boxes and all(scroll_w <= client_w + 1 for _sel, scroll_w, client_w in boxes), boxes


def _person_key(world: HostedWorld, name: str) -> str:
    with _client(world.origin, world.admin_cookie) as c:  # type: ignore[attr-defined]
        people = c.get("/api/people")
        assert people.status_code == 200, people.text
        found = next(p for p in people.json()["people"] if p["name"] == name)
    return "owner" if found["id"] is None else str(found["id"])


def _save_names(page: Page, key: str) -> None:
    """Click the card's Save. A session whose fresh check has run out confirms with the password."""
    page.click(f"#names-save-{key}")
    wait_js(  # not wait_for_function: the page's CSP refuses its eval (test_no_eval_waits.py)
        page,
        """(key) => {
          const d = document.querySelector('#confirm-dialog');
          const r = document.querySelector('#names-result-' + key);
          return (d && d.open) || (r && r.textContent.length > 0);
        }""",
        key,
    )
    if page.locator("#confirm-dialog").evaluate("d => d.open"):
        page.fill("#confirm-password", ADMIN_PW)
        page.click("#confirm-ok")
    expect(page.locator(f"#names-result-{key}")).to_have_text("Saved")


def _restore_names(world: HostedWorld) -> None:
    """Put carol and the admin back, so this module's other tests still see the fixture's names."""
    with _client(world.origin, world.admin_cookie) as c:  # type: ignore[attr-defined]
        people = c.get("/api/people")
        if people.status_code != 200:
            return
        carol = next((p for p in people.json()["people"] if p["name"] == "carol"), None)

        def post(path: str, body: dict[str, Any]) -> None:
            got = c.post(path, json=body)
            if got.status_code == 403 and got.json().get("error") == "reauth":
                assert c.post("/api/signin/check", json={"password": ADMIN_PW}).status_code == 200
                got = c.post(path, json=body)
            assert got.status_code == 200, got.text

        if carol is not None:
            post(f"/api/people/{carol['id']}/name", {"first_name": None, "last_name": None})
        post("/api/people/owner/name", {"first_name": "Alice", "last_name": "Ng"})


def test_the_admin_sets_a_persons_names_from_their_card(ui: UI, world: HostedWorld) -> None:
    """#566: the admin sets Carol's first and last name on her card. Her message, the People
    list and the @ list show it on the open page, and the log gets no "is now" line. Clearing
    both shows carol again, with no hover. The admin's own card does the same, and Settings >
    Profile then shows those names. What was typed survives a re-render of the sheet. On a
    phone the People sheet does not scroll sideways. Fails if the fields are missing, if a
    save waits for a reload, if a names-only save posts a rename notice, or if a redraw wipes
    the fields."""
    try:
        page = page_for(ui, world)
        carol_key = _person_key(world, "carol")
        page.click("#open-people")
        expect(page.locator("#people-panel")).to_be_visible()
        first = page.locator(f"#first-{carol_key}")
        last = page.locator(f"#last-{carol_key}")
        expect(first).to_have_value("")
        expect(last).to_have_value("")
        # a bad username re-renders the sheet at once (renderPeoplePanel(true)), as Done does
        # in test_people_ui.py, and posts nothing
        first.fill("Carol")
        last.fill("Ng")
        page.fill(f"#name-{carol_key}", "!!!")
        page.click(f"#name-save-{carol_key}")
        expect(page.locator(f"#name-result-{carol_key}")).to_have_class(re.compile(r"\bbad\b"))
        expect(first).to_have_value("Carol")
        expect(last).to_have_value("Ng")
        page.fill(f"#name-{carol_key}", "carol")

        carol_card = page.locator(".person-card", has=page.locator(f"#first-{carol_key}"))
        _save_names(page, carol_key)
        expect(carol_card.locator(".card-name")).to_have_text("Carol Ng")
        expect(carol_card.locator(".person-handle")).to_have_text("@carol")
        expect(page.locator("#log")).not_to_contain_text("is now")
        carol = chat_row(page, "from carol")
        expect(carol.locator(".nick")).to_have_text("Carol Ng")
        expect(carol.locator(".nick")).to_have_attribute("title", "@carol")
        carol_row = page.locator('#room-people .person-row[data-name="carol"]')
        expect(carol_row.locator(".m-name")).to_have_text("Carol Ng")
        expect(carol_row.locator(".m-full")).to_have_text("@carol")
        page.click("#people-close")
        box = page.locator("#input")
        box.click()
        page.keyboard.type("@ng")  # "carol" does not contain this; the new last name does
        opt = page.locator("#men-carol")
        expect(opt).to_be_visible()
        expect(opt.locator(".m-name")).to_have_text("Carol Ng")
        expect(opt.locator(".m-full")).to_have_text("@carol")
        page.keyboard.press("Escape")
        box.fill("")

        page.click("#open-people")
        first.fill("")
        last.fill("")
        _save_names(page, carol_key)
        expect(carol_card.locator(".card-name")).to_have_text("carol")
        expect(carol_card.locator(".person-handle")).to_have_count(0)
        expect(carol.locator(".nick")).to_have_text("carol")
        assert carol.locator(".nick").get_attribute("title") in (None, "")
        expect(carol_row.locator(".m-name")).to_have_text("carol")
        expect(carol_row.locator(".m-full")).to_have_count(0)
        expect(page.locator("#log")).not_to_contain_text("is now")

        page.fill("#first-owner", "Alicia")
        page.fill("#last-owner", "Nguyen")
        _save_names(page, "owner")
        expect(page.locator(".person-card").first.locator(".card-name")).to_have_text("Alicia Nguyen")
        expect(page.locator("#buddy-me .m-name")).to_have_text("Alicia Nguyen")
        expect(page.locator("#me-name")).to_have_text("Alicia Nguyen")
        page.click("#people-close")
        open_settings(page, "profile")
        expect(page.locator("#settings-first")).to_have_value("Alicia")
        expect(page.locator("#settings-last")).to_have_value("Nguyen")
        page.keyboard.press("Escape")

        phone = page_for(ui, world, **PHONE)
        # page_for closes the phone's drawer, which is still sliding as it goes: wait, then open
        # it again, or People is not stable long enough to click
        expect(phone.locator("#app.nav-open")).to_have_count(0)
        phone.click("#rooms-toggle")
        expect(phone.locator("#open-people")).to_be_visible()
        phone.click("#open-people")
        expect(phone.locator("#people-panel")).to_be_visible()
        phone.locator("#first-owner").scroll_into_view_if_needed()
        phone.fill("#first-owner", "A" * 64)
        phone.fill("#last-owner", "B" * 64)
        # a stacked field with a 0 flex basis and a hidden overflow draws at no height
        boxes = phone.evaluate(
            """() => ['#first-owner', '#last-owner'].map((id) => {
              const r = document.querySelector(id).getBoundingClientRect();
              return [id, r.width, r.height];
            })"""
        )
        assert all(w >= 40 and h >= 24 for _, w, h in boxes), boxes
        no_horizontal_scroll(phone)
        sheet = phone.evaluate(
            """() => {
              const b = document.querySelector('#people-panel .sheet-body');
              return [b.scrollWidth, b.clientWidth];
            }"""
        )
        assert sheet[0] <= sheet[1] + 1, sheet
    finally:
        _restore_names(world)
