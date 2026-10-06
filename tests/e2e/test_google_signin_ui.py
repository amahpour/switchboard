"""Sign in with Google in a real browser (#70, DESIGN.md §38). "Google" is a fake issuer: its
authorization page is a Playwright route that redirects back to the broker, cross-site, as the
real one does. That return trip is what the session cookie (SameSite=Strict) must survive."""

from __future__ import annotations

import re
import shutil
import urllib.parse
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from fakes.fake_oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIssuer
from playwright.sync_api import Browser, Page, Route, expect
from test_people_ui import ADMIN_PW, choose, sign_in
from test_web_ui import PHASE_REPORTS, UI

from switchboard.broker.auth import WebOrigin

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def google(
    playwright: Any, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[InProcBroker, FakeIssuer]]:
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        mp.setenv("SWITCHBOARD_OIDC_CLIENT_ID", CLIENT_ID)
        mp.setenv("SWITCHBOARD_OIDC_CLIENT_SECRET", CLIENT_SECRET)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://sb.localhost:{port}")).start()
        issuer = FakeIssuer()
        b.state.oidc.fetch = issuer.fetch
        b.on_loop(lambda: b.state.service.create_room("#build"))
        try:
            yield b, issuer
        finally:
            b.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def ui(browser: Browser, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, None)  # type: ignore[arg-type]
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    u.finish(any(r.failed for r in reports.values()) or bool(u.problems), request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def as_google(page: Page, issuer: FakeIssuer, origin: str, email: str) -> None:
    """Google's sign-in page, for this browser: it signs in as ``email`` and redirects back.

    Playwright doesn't intercept the target of a redirect, so the broker's own answer to
    /auth/oidc/start (a 303 to Google, and the binding cookie) is passed on as a page that
    navigates there; that navigation is intercepted, and "Google" redirects back cross-site."""

    def start(route: Route) -> None:
        resp = route.fetch(max_redirects=0)
        to = resp.headers["location"]
        assert to.startswith(ISSUER + "/"), to
        route.fulfill(
            status=200,
            headers={"content-type": "text/html", "set-cookie": resp.headers["set-cookie"]},
            body=f'<meta http-equiv="refresh" content="0;url={to}">',
        )

    def at_google(route: Route) -> None:
        state, code = issuer.authorize(route.request.url, email)
        back = origin + "/auth/oidc/callback?" + urllib.parse.urlencode({"state": state, "code": code})
        route.fulfill(status=302, headers={"Location": back})

    page.route(re.compile("^" + re.escape(origin) + "/auth/oidc/start$"), start)
    page.route(re.compile("^" + re.escape(ISSUER) + "/"), at_google)  # never the real Google


def test_the_admin_sets_a_google_email_and_bob_signs_in_with_it(ui: UI, google) -> None:
    b, issuer = google
    origin = b.state.web_origin.origin
    one_time = b.paths.test_claim_link.read_text().strip().split("#t=", 1)[1]
    admin = ui.context().new_page()
    admin.goto(origin + "/")
    expect(admin.locator("#sso-btn")).to_be_enabled()
    expect(admin.locator("#sso-btn")).to_contain_text("Sign in with Google")
    expect(admin.locator("#sso-soon")).to_be_hidden()
    sign_in(admin, "admin", one_time)
    choose(admin, ADMIN_PW, "alice@example.com")
    admin.click("#open-people")
    admin.fill("#person-first", "Bob")
    admin.fill("#person-last", "Builder")
    admin.fill("#person-email", "bob@example.com")
    admin.click("#person-add")
    admin.click("#invite-done")
    bob_card = admin.locator(".person-card").nth(1)
    bob_card.locator('input[type="email"]').fill("Bob@Example.com")
    bob_card.locator(".email-row button[type=submit]").click()
    expect(admin.locator(".person-card").nth(1).locator(".email-result")).to_have_text("Saved")
    expect(admin.locator(".person-card").nth(1).locator('input[type="email"]')).to_have_value(
        "bob@example.com"
    )

    bob = ui.context().new_page()
    as_google(bob, issuer, origin, "bob@example.com")
    bob.goto(origin + "/")
    bob.click("#sso-btn")
    expect(bob.locator("#me-name")).to_have_text("bob")  # the Strict cookie came along: the app, signed in
    expect(bob.locator("#st-conn")).to_have_text("Connected")  # no "choose a password" step: Google is enough
    admin.reload()
    admin.click("#open-people")
    expect(admin.locator(".person-card").nth(1)).to_contain_text("signs in with Google")


def test_an_account_nobody_signs_in_with_gets_a_plain_answer(ui: UI, google) -> None:
    b, issuer = google
    origin = b.state.web_origin.origin
    page = ui.context().new_page()
    as_google(page, issuer, origin, "stranger@example.com")
    page.goto(origin + "/")
    page.click("#sso-btn")
    expect(page.locator("#login-error")).to_have_text(
        "That Google account isn’t set up here. Ask your admin to add it to your name."
    )
