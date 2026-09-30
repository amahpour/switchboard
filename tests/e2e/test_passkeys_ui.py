"""A hosted broker's owner in a real browser (issue #41, DESIGN.md §31): the claim page from
the log link, the backup passkey, the passkeys sheet, sign out, and signing in with a
passkey. Marker ``e2e``, opt-in, like tests/e2e/test_web_ui.py, whose ``UI`` fixture class is
reused (every page watched for console errors, page errors and CSP violations; a trace and
screenshots kept on failure).

The broker is an in-process test-mode broker behind ``http://sb.localhost:<port>``: a plain-http
public URL that browsers treat as a secure context, which Chromium resolves to loopback itself.
The passkeys are Chromium's own virtual authenticators (CDP ``WebAuthn.addVirtualAuthenticator``:
CTAP2, internal, resident keys, user verification), one per "device".
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

from conftest import InProcBroker, make_tmp_home, sanitize_env
from switchboard.broker.auth import WebOrigin
from test_web_ui import PHASE_REPORTS, UI

pytestmark = pytest.mark.e2e

AUTHENTICATOR = {"protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
                 "isUserVerified": True, "automaticPresenceSimulation": True}


@pytest.fixture(scope="module")
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    """An unclaimed hosted broker (its public URL names its port), with one room."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://sb.localhost:{port}")).start()
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


class Devices:
    """The virtual authenticators of one page: add one per device, remove one that is "not here"."""

    def __init__(self, ctx: BrowserContext, page: Page) -> None:
        self.cdp = ctx.new_cdp_session(page)
        self.cdp.send("WebAuthn.enable", {"enableUI": False})

    def add(self, transport: str = "internal") -> str:
        # Chromium allows one internal (platform) authenticator at a time; a security key is "usb"
        opts = {**AUTHENTICATOR, "transport": transport}
        return self.cdp.send("WebAuthn.addVirtualAuthenticator", {"options": opts})["authenticatorId"]

    def remove(self, authenticator_id: str) -> None:
        self.cdp.send("WebAuthn.removeVirtualAuthenticator", {"authenticatorId": authenticator_id})

    def credentials(self, authenticator_id: str) -> list[dict[str, Any]]:
        return self.cdp.send("WebAuthn.getCredentials", {"authenticatorId": authenticator_id})["credentials"]


def open_link(page: Page, url: str) -> None:
    """Open a claim link as a fresh navigation (setup.js leaves the page at /setup, so a second
    link would otherwise be a same-document hash change that runs no script)."""
    page.goto("about:blank")
    page.goto(url)


def test_claim_backup_sheet_sign_out_and_sign_in(ui: UI, hosted: InProcBroker) -> None:
    origin = hosted.state.web_origin.origin
    link = hosted.paths.test_claim_link.read_text().strip()
    assert link.startswith(origin + "/setup#t=")
    ctx = ui.context()
    page = ctx.new_page()
    devices = Devices(ctx, page)

    # a link with no token in it: the bad-link step at once, and no request carried anything
    open_link(page, origin + "/setup#t=bad")
    expect(page.locator("#step-bad")).to_be_visible()
    expect(page.locator("#step-claim")).to_be_hidden()
    assert page.url == origin + "/setup"
    # a well-formed token that isn't the one: the broker refuses it, and the page says so
    open_link(page, origin + "/setup#t=not-the-real-token-at-all-000000000000")
    expect(page.locator("#step-claim")).to_be_visible()
    page.click("#claim-btn")
    expect(page.locator("#step-bad")).to_be_visible()
    assert hosted.state.claim is not None and hosted.state.claim.active  # the real one is untouched
    # Chromium logs the refused request as a console error (delivered a moment after the DOM
    # changed): that one is expected, nothing else is
    page.wait_for_timeout(300)
    assert ui.problems and all("403" in p and "/api/setup/begin" in p for p in ui.problems), ui.problems
    ui.problems.clear()

    # the real link: the token leaves the address bar at once, and the claim step shows
    laptop = devices.add()
    open_link(page, link)
    expect(page.locator("#step-claim")).to_be_visible()
    assert page.url == origin + "/setup"
    assert page.evaluate("location.hash") == ""
    expect(page.locator("#claim-name")).to_have_value(re.compile(r".+"))  # a default from the platform
    page.fill("#claim-name", "MacBook Pro")
    page.click("#claim-btn")
    expect(page.locator("#step-backup")).to_be_visible()
    expect(page.locator("#backup-lead")).to_contain_text('yours (passkey "MacBook Pro")')
    assert len(devices.credentials(laptop)) == 1
    st = hosted.state
    assert st.claim is None and [p.name for p in st.store.passkeys()] == ["MacBook Pro"]
    assert not hosted.paths.test_claim_link.exists()

    # the backup passkey, on another device (the laptop's authenticator would refuse: excluded)
    devices.remove(laptop)
    phone = devices.add()
    page.fill("#backup-name", "iPhone")
    page.click("#backup-btn")
    expect(page.locator("#st-conn")).to_have_text("Connected")  # the app, signed in by the claim
    assert page.url == origin + "/"
    assert [p.name for p in st.store.passkeys()] == ["MacBook Pro", "iPhone"]
    assert len(devices.credentials(phone)) == 1

    # the passkeys sheet: the count, Add a passkey (fresh from the claim: no check asked)
    expect(page.locator("#passkeys")).to_be_visible()
    page.click("#passkeys")
    expect(page.locator("#passkeys-panel")).to_be_visible()
    expect(page.locator("#passkeys-body")).to_contain_text("2 passkeys can sign in here.")
    key = devices.add("usb")
    page.fill("#passkey-name", "YubiKey")
    page.click("#passkey-add-btn")
    expect(page.locator("#passkey-result")).to_have_text('added "YubiKey"')
    expect(page.locator("#passkeys-body")).to_contain_text("3 passkeys can sign in here.")
    devices.remove(key)  # the key is unplugged again
    page.keyboard.press("Escape")
    expect(page.locator("#passkeys-panel")).to_be_hidden()

    # the claim link is gone for good: /setup is the app now
    page.goto(origin + "/setup")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    assert page.url == origin + "/"

    # sign off this browser: the sign-in page offers the passkey button, not the terminal text
    page.click("#logout")
    expect(page.locator("#passkey-btn")).to_be_visible()
    expect(page.locator("#login-cli")).to_be_hidden()
    expect(page.locator("#login-claim")).to_be_hidden()
    assert "login?t=" not in page.content()

    # sign in with the phone's passkey (the only authenticator still attached)
    page.click("#passkey-btn")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    assert page.url == origin + "/"
    rows = {p.name: p for p in st.store.passkeys()}
    assert rows["iPhone"].last_used_at is not None and rows["MacBook Pro"].last_used_at is None
    assert rows["iPhone"].sign_count >= 1  # Chromium's virtual authenticator counts

    # Sign out everywhere, then back in
    page.click("#passkeys")
    page.once("dialog", lambda d: d.accept())
    page.click("#logout-all")
    expect(page.locator("#passkey-btn")).to_be_visible()
    assert st.store.web_session_count() == 0
    page.click("#passkey-btn")
    expect(page.locator("#st-conn")).to_have_text("Connected")


def test_the_sign_in_page_on_a_phone(ui: UI, hosted: InProcBroker) -> None:
    """After the claim (the test above): the passkey button at phone width, and a cancelled
    ceremony (no authenticator at all) shows its reason and lets you try again."""
    origin = hosted.state.web_origin.origin
    page = ui.context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True,
                      has_touch=True).new_page()
    page.goto(origin + "/")
    expect(page.locator("#passkey-btn")).to_be_visible()
    widths = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert widths[0] <= widths[1], f"horizontal scroll: {widths}"
    page.click("#passkey-btn")
    expect(page.locator("#login-error")).to_be_visible()
    expect(page.locator("#passkey-btn")).to_be_enabled()
    expect(page.locator("#passkey-btn")).to_have_text("Sign in with a passkey")
    assert page.evaluate("document.cookie") == ""  # nothing signed in, and the cookies are HttpOnly anyway
