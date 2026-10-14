"""Admin > People says who still signs in by name (#240, DESIGN.md §39.3): a hosted broker's
admin is told how many people have no email, so a name still signs them in, who they are, and
that name sign-in goes away in the next release, with a way to each person's Email field.
Marker ``e2e``, opt-in; the ``UI`` fixture class of tests/e2e/test_web_ui.py watches every page
for console errors, page errors and CSP violations.
"""

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


@pytest.fixture
def hosted(
    playwright: Any, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[InProcBroker, str, dict[str, int]]]:
    """A hosted broker set up through its API: the admin (alice, no email yet), bob and carol
    added with no email (a name signs them in), and dave added with one. Yields the broker, the
    admin's session cookie and each person's id."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://sb.localhost:{port}")).start()
        b.on_loop(lambda: b.state.service.create_room("#build"))
        origin = b.state.web_origin.origin
        one_time = b.paths.test_claim_link.read_text().strip().split("#t=", 1)[1]
        h = {"Origin": origin, "X-Switchboard": "1"}
        ids: dict[str, int] = {}
        with httpx.Client(base_url=origin, headers=h) as c:
            assert (
                c.post("/api/signin/password", json={"name": "admin", "password": one_time}).status_code
                == 200
            )
            assert c.post("/api/setup/password", json={"password": ADMIN_PW}).status_code == 200
            for body in (
                {"name": "bob"},
                {"first_name": "Carol", "last_name": "Ng", "name": "carol"},
                {"first_name": "Dave", "last_name": "Dee", "name": "dave", "email": "dave@example.com"},
            ):
                added = c.post("/api/people", json=body)
                assert added.status_code == 200, added.text
                ids[body["name"]] = added.json()["person"]["id"]
            cookie = c.cookies.get("switchboard_session")
        assert cookie
        try:
            yield b, cookie, ids
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


def admin_people(ui: UI, hosted: tuple[InProcBroker, str, dict[str, int]], **options: Any) -> Page:
    """The admin's page with the People sheet open."""
    b, cookie, _ids = hosted
    origin = b.state.web_origin.origin
    ctx = ui.context(**options)
    ctx.add_cookies([{"name": "switchboard_session", "value": cookie, "url": origin}])
    page = ctx.new_page()
    page.goto(origin + "/")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    if page.locator("#open-people").is_hidden():  # a phone keeps the sidebar in a drawer
        page.click("#rooms-toggle")
    page.click("#open-people")
    expect(page.locator("#people-panel")).to_be_visible()
    expect(page.locator(".person-card")).to_have_count(4)
    return page


@pytest.mark.parametrize("phone", [False, True])
def test_people_says_who_still_signs_in_by_name_and_follows_their_emails(
    ui: UI, hosted: tuple[InProcBroker, str, dict[str, int]], phone: bool
) -> None:
    """#240: the release before name sign-in is dropped has to tell the admin who still depends
    on it. People shows a note with how many people have no email (bob and carol here; dave has
    one, and the admin is not counted, since ``admin`` always signs them in), names them, and says
    it goes away in the next release. Each name goes to that person's Email field, by mouse or by
    keyboard, and the note follows each Save: one fewer, then gone. Fails if the note is missing,
    counts the admin or someone with an email, doesn't name people, doesn't reach the Email field,
    or doesn't update (a phone must not scroll sideways either)."""
    _b, _cookie, ids = hosted
    page = admin_people(ui, hosted, **(PHONE if phone else {}))
    note = page.locator("#by-name-note")
    expect(note).to_be_visible()
    expect(note.locator("strong").first).to_have_text("2 people still sign in by name")
    expect(note).to_contain_text("Name sign-in goes away in the next release")
    expect(note.locator("li button")).to_have_text(["bob", "Carol Ng"])
    # the cards say the same, one by one (bob and carol; neither dave nor the admin)
    expect(page.locator(".person-card", has_text="no email yet")).to_have_count(3)  # + the admin's, by name
    if phone:  # the note and its buttons fit the screen
        assert page.evaluate(
            "() => { const b = document.querySelector('#people-panel .sheet-body');"
            " return b.scrollWidth <= b.clientWidth + 1; }"
        )

    # by mouse, a name goes to that person's Email field ...
    note.locator("li button", has_text="Carol Ng").click()
    expect(page.locator(f"#email-{ids['carol']}")).to_be_focused()
    # ... and by keyboard: Enter on the focused name does the same
    note.locator("li button", has_text="bob").focus()
    page.keyboard.press("Enter")
    expect(page.locator(f"#email-{ids['bob']}")).to_be_focused()

    # setting bob's email: the note says one person now, and focus stays in the field
    page.fill(f"#email-{ids['bob']}", "bob@example.com")
    page.keyboard.press("Enter")
    expect(page.locator(f"#email-result-{ids['bob']}")).to_have_text("Saved")
    expect(note.locator("strong").first).to_have_text("1 person still signs in by name")
    expect(note.locator("li button")).to_have_text(["Carol Ng"])
    expect(page.locator(f"#email-{ids['bob']}")).to_be_focused()

    # the last one: the note is gone, and so is every "no email yet" but the admin's
    page.fill(f"#email-{ids['carol']}", "carol@example.com")
    page.keyboard.press("Enter")
    expect(page.locator(f"#email-result-{ids['carol']}")).to_have_text("Saved")
    expect(page.locator("#by-name-note")).to_have_count(0)
    expect(page.locator(".person-card", has_text="no email yet")).to_have_count(1)
    expect(page.locator(f"#email-{ids['carol']}")).to_be_focused()
