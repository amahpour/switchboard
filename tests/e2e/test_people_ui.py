"""People on a hosted broker in a real browser (issue #61, DESIGN.md §32): the admin's first
sign-in with the one-time password from the log, the Choose how you'll sign in page, the
People sheet (Add someone, the invite to send, Done), a teammate's first sign-in and their own
password, and Confirm it's you before the admin adds someone once their check ran out. Marker
``e2e``, opt-in; the ``UI`` fixture class of tests/e2e/test_web_ui.py watches every page for
console errors, page errors and CSP violations.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHASE_REPORTS, UI

from switchboard.broker.auth import WebOrigin

pytestmark = pytest.mark.e2e

ADMIN_PW = "correct horse battery"
BOB_PW = "bob's own secret 1"


@pytest.fixture(scope="module")
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    """A hosted broker nobody has set up yet, with one room."""
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


def sign_in(page: Page, name: str, password: str) -> None:
    page.fill("#signin-name", name)
    page.fill("#signin-password", password)
    page.click("#password-btn")


def choose(page: Page, password: str, email: str | None = None) -> None:
    """Choose how you'll sign in: who the admin is first when an email is given (setup asks for
    their first and last name and their email, #192)."""
    expect(page.locator("#step-choose")).to_be_visible()
    if email is not None:
        page.fill("#setup-first", "Alice")
        page.fill("#setup-last", "Liddell")
        page.fill("#setup-email", email)
    page.fill("#new-password", password)
    page.fill("#new-password-2", password)
    page.click("#password-btn")
    expect(page.locator("#st-conn")).to_have_text("Connected")


def test_the_admin_sets_up_adds_bob_and_bob_joins(ui: UI, hosted: InProcBroker) -> None:
    origin = hosted.state.web_origin.origin
    one_time = hosted.paths.test_claim_link.read_text().strip().split("#t=", 1)[1]
    ctx = ui.context()
    ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
    page = ctx.new_page()

    # not set up yet: the note about the log, the name already "admin", no passkey button yet
    page.goto(origin + "/")
    expect(page.locator("#login-first")).to_be_visible()
    expect(page.locator("#signin-name")).to_have_value("admin")
    expect(page.locator("#passkey-btn")).to_be_hidden()
    expect(page.locator("#sso-btn")).to_be_disabled()
    expect(page.locator("#sso-soon")).to_have_text("Coming soon")
    sign_in(page, "admin", one_time.lower())  # as typed: any case
    expect(page.locator("#choose-lead")).to_contain_text(
        "You’re the admin of this switchboard, signed in as alice"
    )
    # #192: setup asks who the admin is first, their name and the email they sign in with
    expect(page.locator("#setup-first")).to_be_focused()
    page.fill("#setup-first", "Alice")
    page.fill("#setup-last", "Liddell")
    page.fill("#setup-email", "Alice@Example.com")
    expect(page.locator("#setup-user")).to_have_value("Alice@Example.com")  # what a password manager saves
    # two different passwords: said here, nothing sent
    page.fill("#new-password", ADMIN_PW)
    page.fill("#new-password-2", ADMIN_PW + "!")
    page.click("#password-btn")
    expect(page.locator("#setup-error")).to_have_text("The two passwords are not the same.")
    choose(page, ADMIN_PW)
    assert hosted.state.claim is None and hosted.state.store.owner_password_hash() is not None
    assert hosted.state.store.owner_email() == "alice@example.com"
    assert hosted.state.store.owner_names() == ("Alice", "Liddell")
    expect(page.locator("#buddy-me .m-full")).to_have_text("Alice Liddell")

    # the admin section: Add someone (their names and email; the name in the rooms follows the
    # first name), then the invite to send, once
    expect(page.locator("#admin-section")).to_be_visible()
    page.click("#open-people")
    expect(page.locator("#people-panel")).to_be_visible()
    expect(page.locator("#person-first")).to_be_focused()
    page.fill("#person-first", "Bob")
    page.fill("#person-last", "Builder")
    page.fill("#person-email", "bob@example.com")
    expect(page.locator("#person-name")).to_have_value("bob")
    page.click("#person-add")  # fresh from the sign-in: no check asked
    expect(page.locator("#copy-invite")).to_be_focused()
    invite = page.locator("#invite-text").inner_text()
    assert invite.startswith(
        f"Hi Bob, you're invited to switchboard: {origin}\nSign in with bob@example.com and the"
        " one-time password "
    )
    bob_otp = invite.split("one-time password ", 1)[1].split(" ", 1)[0]
    page.click("#copy-invite")
    expect(page.locator("#copy-invite")).to_have_attribute("aria-label", "Copied")
    assert page.evaluate("navigator.clipboard.readText()") == invite
    page.click("#invite-done")
    expect(page.locator("#invite-text")).to_have_count(0)
    expect(page.locator(".person-card")).to_have_count(2)
    bob_card = page.locator(".person-card").nth(1)
    expect(bob_card).to_contain_text("hasn't signed in yet: one-time password")
    expect(bob_card.locator(".remote-name")).to_have_text("Bob Builder")
    expect(bob_card.locator(".person-handle")).to_have_text("@bob")
    expect(page.locator(".person-card").nth(0).locator(".remote-name")).to_have_text("Alice Liddell")
    expect(page.locator(".person-card").nth(0)).not_to_contain_text("no email yet")

    # bob, in another browser: the three ways in, his one-time password, then his own
    bob_ctx = ui.context()
    bob = bob_ctx.new_page()
    bob.goto(origin + "/")
    expect(bob.locator("#login-first")).to_be_hidden()
    expect(bob.locator("#passkey-btn")).to_be_visible()
    sign_in(bob, "bob@example.com", "WRONG-PASS-WORD-0000")
    expect(bob.locator("#login-error")).to_have_text("wrong email or password")
    bob.wait_for_timeout(300)
    assert ui.problems and all("403" in p and "/api/signin/password" in p for p in ui.problems), ui.problems
    ui.problems.clear()
    sign_in(bob, "bob@example.com", bob_otp)
    expect(bob.locator("#choose-lead")).to_contain_text("Hi Bob. Your one-time password worked.")
    choose(bob, BOB_PW)
    expect(bob.locator("#me-name")).to_have_text("bob")
    expect(bob.locator("#buddy-me .m-full")).to_have_text("Bob Builder")
    expect(bob.locator("#admin-section")).to_be_hidden()  # the admin section is the admin's
    expect(bob.locator("#add-machine")).to_be_visible()  # everyone pairs their own machines

    # bob posts as himself; the admin's page shows it under his name
    bob.fill("#input", "hello from bob")
    bob.keyboard.press("Enter")
    expect(page.locator("#log .line.k-chat").last).to_contain_text("hello from bob")
    expect(page.locator("#log .line.k-chat").last).to_contain_text("bob")
    # his own page knows his full name: it's the hover on his name
    expect(bob.locator("#log .line.k-chat").last.locator(".nick-human")).to_have_attribute(
        "title", "Bob Builder"
    )

    # the admin's check ran out: adding someone asks for the password first
    hosted.on_loop(lambda: hosted.state.passkey_checks.clear())
    page.fill("#person-first", "Carol")
    page.fill("#person-last", "Ng")
    page.fill("#person-email", "carol@example.com")
    page.click("#person-add")
    expect(page.locator("#confirm-dialog")).to_be_visible()
    expect(page.locator("#confirm-password")).to_be_focused()
    page.fill("#confirm-password", "not it at all")
    page.click("#confirm-ok")
    expect(page.locator("#confirm-error")).to_have_text("wrong password")
    page.wait_for_timeout(300)
    assert all("403" in p and "/api/signin/check" in p for p in ui.problems), ui.problems
    ui.problems.clear()
    page.fill("#confirm-password", ADMIN_PW)
    page.click("#confirm-ok")
    expect(page.locator("#confirm-dialog")).to_be_hidden()
    expect(page.locator("#invite-text")).to_contain_text("Sign in with carol@example.com and")

    # #192: the admin changes bob's email on his card; from then on the new one signs him in,
    # and neither his old email nor his name does. What's typed outlives a render: "Copied"
    # fading re-renders the sheet
    page.click("#copy-invite")
    expect(bob_card.locator(".email-row label")).to_have_text("Email")
    bob_card.locator('input[type="email"]').fill("Robert@Example.com")
    expect(page.locator("#copy-invite")).to_have_attribute("aria-label", "Copy the invite")
    expect(bob_card.locator('input[type="email"]')).to_have_value("Robert@Example.com")
    page.click("#invite-done")
    bob_card.locator(".email-row button").click()
    expect(bob_card.locator(".email-result")).to_have_text("Saved")
    expect(bob_card).not_to_contain_text("no email yet")
    again = ui.context().new_page()
    again.goto(origin + "/")
    expect(again.locator('label[for="signin-name"]')).to_have_text("Email")
    hint = "No email set for you yet? Your name works for now."
    expect(again.locator("#signin-name-hint")).to_have_text(hint)
    for old in ("bob", "bob@example.com"):
        sign_in(again, old, BOB_PW)
        expect(again.locator("#login-error")).to_have_text("wrong email or password")
        again.wait_for_timeout(300)
        assert all("403" in p and "/api/signin/password" in p for p in ui.problems), ui.problems
        ui.problems.clear()
    sign_in(again, "robert@example.com", BOB_PW)
    expect(again.locator("#me-name")).to_have_text("bob")
    again.click("#me-settings")
    expect(again.locator("#app-dialog")).to_contain_text(
        "You sign in with your email, robert@example.com, and this password."
    )
    expect(again.locator('#password-form input[name="username"]')).to_have_value("robert@example.com")
