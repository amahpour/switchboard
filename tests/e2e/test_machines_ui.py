"""Machines that dial in, in a real browser (issue #41 part 3, DESIGN.md §31.8): Add a machine,
the pairing commands and their Copy buttons, a test machine dialing in with the code, the
approval card, Approve, the link up, Remove; Cancel, Reject and a code that expires. Marker
``e2e``, opt-in, with tests/e2e/test_web_ui.py's ``UI`` (every page watched for console errors,
page errors and CSP violations; a trace and screenshots kept on failure).

The broker is an in-process test-mode broker behind ``http://localhost:<port>``: a secure
context for the passkeys (Chromium's virtual authenticators, as in test_passkeys_ui.py) that the
test machine's dialer, another process, resolves too. The machine (``fakes.fake_machine``) pairs
with the code the page shows, saying it is a Mac called work-laptop, and runs the real dialer.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from fakes.fake_machine import TestMachine
from playwright.sync_api import Page, expect
from test_passkeys_ui import Devices, open_link
from test_web_ui import PHASE_REPORTS, UI

from switchboard.broker.auth import WebOrigin
from switchboard.remote.dialer import EXIT_FINAL

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    """An unclaimed hosted broker (test mode, so a test machine's satellite may link), one room."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://localhost:{port}")).start()
        b.on_loop(lambda: b.state.service.create_room("#build"))
        try:
            yield b
        finally:
            b.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def ui(browser: Any, hosted: InProcBroker, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, None)  # type: ignore[arg-type]  # no seeded world: the pages are opened by hand
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


@pytest.fixture
def machines() -> Iterator[list[TestMachine]]:
    made: list[TestMachine] = []
    yield made
    for m in made:
        m.close()


OWNER_KEYS: list[dict[str, Any]] = []  # the owner's passkey, carried from one test's browser to the next


def owner_page(ui: UI, hosted: InProcBroker) -> tuple[Page, Devices]:
    """A browser signed in as the owner: the claim in the first test, a passkey sign-in after."""
    origin = hosted.state.web_origin.origin
    ctx = ui.context()
    ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
    page = ctx.new_page()
    devices = Devices(ctx, page)
    aid = devices.add()
    if hosted.state.claim is not None:
        open_link(page, hosted.paths.test_claim_link.read_text().strip())
        expect(page.locator("#step-choose")).to_be_visible()
        page.fill("#setup-first", "Alice")  # setup asks who the admin is (#192)
        page.fill("#setup-last", "Liddell")
        page.fill("#setup-email", "alice@example.com")
        page.click("#passkey-btn")  # a passkey instead of a password (§32.4)
        expect(page.locator("#step-backup")).to_be_visible()
        page.click("#skip-btn")
    else:
        for c in OWNER_KEYS:  # its counter moved on since it was copied: ahead of anything the broker saw
            c = {**c, "signCount": c.get("signCount", 0) + 1000}
            devices.cdp.send("WebAuthn.addCredential", {"authenticatorId": aid, "credential": c})
        page.goto(origin + "/")
        page.click("#passkey-btn")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    OWNER_KEYS[:] = devices.credentials(aid)
    return page, devices


def not_fresh(hosted: InProcBroker) -> None:
    """Forget every session's passkey check: the next code or approval asks for a passkey."""

    def forget() -> None:
        hosted.state.passkey_checks.clear()
        hosted.state.claim_grace.clear()

    hosted.on_loop(forget)


def pair_code(page: Page) -> str:
    return page.locator("#pair-join").inner_text().split()[-1]


def test_add_approve_and_remove_a_machine(ui: UI, hosted: InProcBroker, machines: list[TestMachine]) -> None:
    origin = hosted.state.web_origin.origin
    page, _ = owner_page(ui, hosted)
    st = hosted.state

    # a hosted broker: Add a machine under Remote machines, and nothing else there yet
    expect(page.locator("#remotes-section")).to_be_visible()
    expect(page.locator("#machines .remote")).to_have_count(0)
    page.click("#add-machine")
    expect(page.locator("#machines-panel")).to_be_visible()
    expect(page.locator("#machine-name")).to_be_focused()
    # the hint follows the name as it's typed; a name that isn't one is refused in the page
    page.fill("#machine-name", "Work Laptop!")
    page.click("#machine-pair-btn")
    expect(page.locator("#machine-result-add")).to_contain_text("A name looks like work-laptop")
    page.fill("#machine-name", "work-laptop")
    expect(page.locator("#machine-add")).to_contain_text("Its agents show up as bench@work-laptop.")

    # making a code needs a passkey check in the last five minutes: the page asks for the
    # passkey first (no refused request, so no console error), then makes the code
    not_fresh(hosted)
    checks = len(st.passkey_checks)
    page.click("#machine-pair-btn")
    expect(page.locator("#pair-join")).to_have_text(
        re.compile(
            r"^switchboard remote join " + re.escape(origin) + r" [0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$"
        )
    )
    assert len(st.passkey_checks) == checks + 1
    expect(page.locator("#pair-install")).to_have_text(
        re.compile(r"^uv tool install switchboard-chat==\d+\.\d+\.\d+$")
    )
    expect(page.locator("#pair-left")).to_have_text(re.compile(r"^(10:00|9:[0-5]\d) left$"))
    expect(page.locator("#machines-body .machine-wait")).to_contain_text(
        "Waiting for work-laptop to dial in…"
    )
    expect(page.locator('[data-focus="copy:install"]')).to_be_focused()
    code = pair_code(page)

    # Copy puts the whole command on the clipboard, and says so
    page.click('[data-focus="copy:join"]')
    expect(page.locator('[data-focus="copy:join"]')).to_have_attribute("aria-label", "Copied")
    assert page.evaluate("navigator.clipboard.readText()") == f"switchboard remote join {origin} {code}"

    # the machine pairs with that code and dials in: its approval card takes the pairing's place
    m = TestMachine(origin)
    machines.append(m)
    m.pair(code)
    m.start()
    card = page.locator('.machine-card.st-pending[data-machine="work-laptop"]')
    expect(card).to_be_visible()
    expect(page.locator("#pair-join")).to_have_count(0)
    expect(card.locator(".machine-fp")).to_have_text(m.fingerprint)
    expect(card).to_contain_text("macOS 15.6 · arm64")  # what it says about itself
    expect(card).to_contain_text("It dialed in and is waiting for you.")
    expect(card).to_contain_text("Check this matches what remote join printed on your machine.")
    expect(page.locator('[data-focus="approve:work-laptop"]')).to_be_focused()
    row = page.locator('#machines .remote[data-focus="machine:work-laptop"]')
    expect(row).to_have_class(re.compile(r"\bst-pending\b"))
    expect(row).to_contain_text("needs approval")

    # Approve (the check from the code still counts): the link comes up
    page.click('[data-focus="approve:work-laptop"]')
    expect(row).to_have_class(re.compile(r"\bst-up\b"))
    up = page.locator('.machine-card.st-up[data-machine="work-laptop"]')
    expect(up).to_contain_text("wss: it dials in")
    expect(up).to_contain_text(m.fingerprint)
    expect(up).to_contain_text("Approved")
    assert st.store.machine("work-laptop").approved_via == "web"

    # closed and opened again from its row, the sheet shows that machine's card
    page.keyboard.press("Escape")
    expect(page.locator("#machines-panel")).to_be_hidden()
    row.click()
    expect(page.locator("#machines-panel")).to_be_visible()
    expect(up).to_be_focused()

    # Remove asks first; then the machine is gone, and its dialer stops for good
    page.click('[data-focus="remove:work-laptop"]')
    expect(page.locator("#app-dialog-title")).to_have_text("Remove work-laptop?")
    expect(page.locator("#app-dialog-cancel")).to_be_focused()
    page.click("#app-dialog-action")
    expect(page.locator("#machines .remote")).to_have_count(0)
    expect(page.locator("#machine-result-note")).to_have_text("Removed work-laptop.")
    assert m.proc is not None and m.proc.wait(15) == EXIT_FINAL
    assert st.store.machine("work-laptop").removed


def test_cancel_reject_and_an_expired_code(ui: UI, hosted: InProcBroker, machines: list[TestMachine]) -> None:
    origin = hosted.state.web_origin.origin
    page, _ = owner_page(ui, hosted)
    page.click("#add-machine")

    # Cancel: the code stops working at once, and the name comes back to the form
    page.fill("#machine-name", "lab-pc")
    page.click("#machine-pair-btn")
    code = pair_code(page)
    page.click('[data-focus="pair-cancel"]')
    expect(page.locator("#machine-name")).to_have_value("lab-pc")
    expect(page.locator("#machine-name")).to_be_focused()
    m = TestMachine(origin, facts={"hostname": "lab-pc", "os": "Ubuntu 24.04", "arch": "x86_64"})
    machines.append(m)
    with pytest.raises(httpx.HTTPStatusError) as e:
        m.pair(code)
    assert e.value.response.status_code == 403

    # a new code; the machine pairs without dialing in, and is rejected
    page.click("#machine-pair-btn")
    m.pair(pair_code(page))
    card = page.locator('.machine-card.st-pending[data-machine="lab-pc"]')
    expect(card).to_contain_text("its dialer isn't connected")
    page.click('[data-focus="reject:lab-pc"]')
    expect(page.locator("#app-dialog-title")).to_have_text("Reject lab-pc?")
    page.click("#app-dialog-action")
    expect(card).to_have_count(0)
    expect(page.locator("#machine-result-note")).to_have_text("Rejected lab-pc.")
    assert hosted.state.store.machine("lab-pc").removed

    # a code nobody used runs out: the page says so and makes a new one on request
    page.clock.install()
    page.fill("#machine-name", "lab-pc")
    page.click("#machine-pair-btn")
    old = pair_code(page)
    page.clock.fast_forward("10:01")
    expect(page.locator("#machines-body .machine-wait")).to_contain_text(
        "The code expired before a machine used it."
    )
    page.click('[data-focus="pair-again"]')
    expect(page.locator("#pair-left")).to_have_text(re.compile(r" left$"))
    assert pair_code(page) != old
