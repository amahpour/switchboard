"""Admins in the People sheet (#569, DESIGN.md §51), on a hosted broker.

Alice is the owner. Bob and Carol are people she added. Marker ``e2e``, opt-in; the
``UI`` fixture watches every page for console errors, page errors and CSP violations.
Waits are Playwright's ``expect`` or ``ui_world.wait_js``, never ``page.wait_for_function``.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHASE_REPORTS, PHONE, UI, no_horizontal_scroll
from ui_world import HostedWorld

pytestmark = pytest.mark.e2e

ADMIN_PW = "correct horse battery"
BOB_PW = "bob's own secret 1"


def _client(origin: str, cookie: str | None = None) -> httpx.Client:
    cookies = {"switchboard_session": cookie} if cookie else None
    return httpx.Client(base_url=origin, headers={"Origin": origin, "X-Switchboard": "1"}, cookies=cookies)


@pytest.fixture(scope="module")
def world(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[HostedWorld]:
    """A hosted broker: alice the owner, bob and carol signed in as ordinary people."""
    del playwright
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
                json={
                    "name": "bob",
                    "email": "bob@example.com",
                    "first_name": "Bob",
                    "last_name": "Lee",
                },
            )
            assert bob.status_code == 200, bob.text
            carol = c.post(
                "/api/people",
                json={
                    "name": "carol",
                    "email": "carol@example.com",
                    "first_name": "Carol",
                    "last_name": "Ng",
                },
            )
            assert carol.status_code == 200, carol.text
            admin_cookie = c.cookies.get("switchboard_session")
        assert admin_cookie
        with _client(origin) as c:
            signed = c.post(
                "/api/signin/password", json={"name": "bob@example.com", "password": bob.json()["password"]}
            )
            assert signed.status_code == 200, signed.text
            chosen = c.post("/api/me/password", json={"password": BOB_PW})
            assert chosen.status_code == 200, chosen.text
            bob_cookie = c.cookies.get("switchboard_session")
        assert bob_cookie
        hosted.admin_cookie = admin_cookie  # type: ignore[attr-defined]
        hosted.bob_cookie = bob_cookie  # type: ignore[attr-defined]
        hosted.bob_id = bob.json()["person"]["id"]  # type: ignore[attr-defined]
        hosted.carol_id = carol.json()["person"]["id"]  # type: ignore[attr-defined]
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


def set_admin(world: HostedWorld, pid: int, on: bool) -> None:
    """The owner sets ``pid``'s admin, with a fresh check, so each test starts from a known list."""
    with _client(world.origin, world.admin_cookie) as c:  # type: ignore[attr-defined]
        checked = c.post("/api/signin/check", json={"password": ADMIN_PW})
        assert checked.status_code == 200, checked.text
        done = c.post(f"/api/people/{pid}/admin", json={"admin": on})
        assert done.status_code == 200, done.text


def open_page(ui: UI, world: HostedWorld, cookie: str, **options: Any) -> Page:
    ctx = ui.context(**options)
    ctx.add_cookies([{"name": "switchboard_session", "value": cookie, "url": world.origin}])
    page = ctx.new_page()
    page.goto(world.origin + "/")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    return page


def open_people(page: Page) -> None:
    if page.locator("#rooms-toggle").is_visible() and page.locator("#open-people").is_hidden():
        page.click("#rooms-toggle")
    page.click("#open-people")
    expect(page.locator("#people-panel")).to_be_visible()
    expect(page.locator("#person-first")).to_be_visible()


def test_an_admin_appears_and_goes_without_a_reload(ui: UI, world: HostedWorld) -> None:
    """#569: the owner sees an Owner badge and makes bob an admin, after Confirm it's you.
    Bob's already-open page gains Admin without a reload, and he can make carol an admin.
    There is no Make admin or Remove admin on the owner's card. When the owner takes bob's
    admin away, his Admin entry goes and his People sheet closes. Fails if the sidebar or
    the sheet waits for a reload, or     if the owner's card offers the button."""
    set_admin(world, world.bob_id, False)  # type: ignore[attr-defined]
    set_admin(world, world.carol_id, False)  # type: ignore[attr-defined]
    alice = open_page(ui, world, world.admin_cookie)  # type: ignore[attr-defined]
    bob = open_page(ui, world, world.bob_cookie)  # type: ignore[attr-defined]
    expect(alice.locator("#admin-section")).to_be_visible()
    expect(bob.locator("#admin-section")).to_be_hidden()

    world.broker.on_loop(lambda: world.broker.state.passkey_checks.clear())
    open_people(alice)
    owner = alice.locator(".person-card", has=alice.locator(".person-chip", has_text="Owner"))
    expect(owner).to_be_visible()
    expect(owner.locator("button", has_text="Make admin")).to_have_count(0)
    expect(owner.locator("button", has_text="Remove admin")).to_have_count(0)
    alice.click(f"#admin-toggle-{world.bob_id}")  # type: ignore[attr-defined]
    expect(alice.locator("#confirm-dialog")).to_be_visible()
    expect(alice.locator("#confirm-title")).to_have_text("Confirm it’s you")
    alice.fill("#confirm-password", ADMIN_PW)
    alice.click("#confirm-ok")
    expect(alice.locator("#confirm-dialog")).to_be_hidden()
    bob_card = alice.locator(".person-card", has=alice.locator(f"#admin-toggle-{world.bob_id}"))  # type: ignore[attr-defined]
    expect(bob_card.locator(".person-chip")).to_have_text("Admin")

    expect(bob.locator("#admin-section")).to_be_visible()
    open_people(bob)
    bob_owner = bob.locator(".person-card", has=bob.locator(".person-chip", has_text="Owner"))
    expect(bob_owner.locator("button", has_text="Make admin")).to_have_count(0)
    expect(bob_owner.locator("button", has_text="Remove admin")).to_have_count(0)
    bob.click(f"#admin-toggle-{world.carol_id}")  # type: ignore[attr-defined]
    expect(bob.locator("#confirm-dialog")).to_be_visible()
    bob.fill("#confirm-password", BOB_PW)
    bob.click("#confirm-ok")
    expect(bob.locator("#confirm-dialog")).to_be_hidden()
    carol_card = bob.locator(".person-card", has=bob.locator(f"#admin-toggle-{world.carol_id}"))  # type: ignore[attr-defined]
    expect(carol_card.locator(".person-chip")).to_have_text("Admin")

    alice.click(f"#admin-toggle-{world.bob_id}")  # type: ignore[attr-defined]
    expect(bob.locator("#admin-section")).to_be_hidden()
    expect(bob.locator("#people-panel")).to_be_hidden()


def test_the_people_sheet_does_not_scroll_sideways_on_a_phone(ui: UI, world: HostedWorld) -> None:
    """#569: on a phone the Owner and Admin badges and the Make admin button wrap, so the
    People sheet has no horizontal scroll. Fails if a badge or a button widens the sheet."""
    set_admin(world, world.bob_id, True)  # type: ignore[attr-defined]
    set_admin(world, world.carol_id, False)  # type: ignore[attr-defined]
    phone = open_page(ui, world, world.admin_cookie, **PHONE)  # type: ignore[attr-defined]
    open_people(phone)
    expect(phone.locator(".person-chip", has_text="Owner")).to_be_visible()
    expect(phone.locator(".person-chip", has_text="Admin")).to_be_visible()
    expect(phone.locator("button", has_text="Make admin").first).to_be_visible()
    no_horizontal_scroll(phone)
    sheet = phone.evaluate(
        """() => {
          const b = document.querySelector('#people-panel .sheet-body');
          return [b.scrollWidth, b.clientWidth];
        }"""
    )
    assert sheet[0] <= sheet[1] + 1, sheet
