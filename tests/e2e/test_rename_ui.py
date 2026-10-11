"""Renaming in a real browser (#114, DESIGN.md §41): you rename yourself in Settings, the admin
renames someone from their People card, and each room says so. A hosted broker of its own, so
the renames touch no other test's world. Marker ``e2e``."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from playwright.sync_api import Browser, expect
from test_people_ui import ADMIN_PW, choose, sign_in
from test_web_ui import PHASE_REPORTS, UI

from switchboard.broker.auth import WebOrigin

pytestmark = pytest.mark.e2e


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
