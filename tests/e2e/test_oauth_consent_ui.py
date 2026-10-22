"""The consent page of an agent that signs in through the browser, in a real browser (#386, DESIGN.md
§49.9): what the person is shown, Approve and Deny handing the answer to the agent's loopback address,
the Confirm it's you step a hosted broker asks for, signing in first, a request that can't continue, the
phone, and the sign-in showing in Settings → Agents. A hosted broker of its own, with its admin signed in
through the API; the agent is httpx, and its loopback address is a route the page's navigation is answered
by, so nothing listens on a port. Marker ``e2e``; any console error, page error or CSP violation fails."""

from __future__ import annotations

import re
import secrets
import shutil
import urllib.parse
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from playwright.sync_api import Browser, BrowserContext, Page, Route, expect
from test_agent_tokens_ui import ADMIN_PW, checked_now, revoke_all
from test_web_ui import PHASE_REPORTS, PHONE, UI, open_settings

from switchboard.broker import agent_tokens, oauth
from switchboard.broker.auth import WebOrigin

pytestmark = pytest.mark.e2e

REDIRECT = "http://localhost:41234/callback"
CLIENT = "Claude Code (switchboard)"
# the page has gone to the agent's own address (it says so itself just before: wait for the address)
AT_THE_AGENT = re.compile(re.escape(REDIRECT) + r"\?")


@pytest.fixture(scope="module")
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[InProcBroker, str]]:
    """A hosted broker with its admin signed in through the API: the broker and the session cookie."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://sb.localhost:{port}")).start()
        origin = b.state.web_origin.origin
        one_time = b.paths.test_claim_link.read_text().strip().split("#t=", 1)[1]
        h = {"Origin": origin, "X-Switchboard": "1"}
        with httpx.Client(base_url=origin, headers=h) as c:
            assert (
                c.post("/api/signin/password", json={"name": "admin", "password": one_time}).status_code
                == 200
            )
            assert c.post("/api/setup/password", json={"password": ADMIN_PW}).status_code == 200
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


class Agent:
    """The program: registers, builds the address the browser is sent to, and swaps the code."""

    def __init__(self, origin: str, redirect: str = REDIRECT, name: str = CLIENT) -> None:
        self.origin, self.redirect = origin, redirect
        self.c = httpx.Client(base_url=origin, timeout=10.0)
        r = self.c.post(
            "/oauth/register",
            json={"client_name": name, "redirect_uris": [redirect], "token_endpoint_auth_method": "none"},
        )
        assert r.status_code == 201, r.text
        self.client_id = r.json()["client_id"]
        self.verifier = secrets.token_urlsafe(32)
        self.state = secrets.token_urlsafe(12)

    def query(self, **over: str | None) -> str:
        q: dict[str, str | None] = {
            "response_type": "code",
            "client_id": self.client_id,
            "code_challenge": oauth.s256(self.verifier),
            "code_challenge_method": "S256",
            "redirect_uri": self.redirect,
            "state": self.state,
            "resource": self.origin + "/mcp",
        }
        q.update(over)
        return urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})

    def url(self, **over: str | None) -> str:
        return f"{self.origin}/oauth/authorize?{self.query(**over)}"

    def exchange(self, code: str) -> httpx.Response:
        return self.c.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": self.verifier,
                "redirect_uri": self.redirect,
                "client_id": self.client_id,
                "resource": self.origin + "/mcp",
            },
        )


def answer_loopback(ctx: BrowserContext, got: list[str]) -> None:
    """Stand in for the agent's listener: the page's navigation to its address is answered here, and the
    address (with the code or the error) is kept."""

    def handle(route: Route) -> None:
        got.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body="<p>the agent's own page</p>")

    ctx.route("http://localhost:41234/**", handle)


def open_consent(
    ui: UI, hosted: tuple[InProcBroker, str], agent: Agent, *, signed_in: bool = True, **options: Any
) -> tuple[Page, list[str]]:
    b, cookie = hosted
    origin = b.state.web_origin.origin
    ctx = ui.context(**options)
    if signed_in:
        ctx.add_cookies([{"name": "switchboard_session", "value": cookie, "url": origin}])
    got: list[str] = []
    answer_loopback(ctx, got)
    page = ctx.new_page()
    page.goto(agent.url())
    return page, got


def test_approve_hands_the_code_to_the_agent_and_the_agent_gets_a_token(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """The card says who is signed in, what the agent calls itself and where its answer goes; naming the
    device and pressing Approve sends the browser to the agent's address with a code and its state, which the
    agent swaps for a token; the sign-in then shows in Settings → Agents beside tokens, with Revoke. Fails
    if the card shows another person or address, if the device name is not asked, if the browser is not
    sent to the registered address with the right state, or if the token is not the device's."""
    b, _ = hosted
    revoke_all(hosted)
    checked_now(hosted)
    agent = Agent(b.state.web_origin.origin)
    page, got = open_consent(ui, hosted, agent)
    expect(page.locator("#oauth-title")).to_have_text("Sign in an agent")
    expect(page.locator("#step-consent")).to_be_visible()
    expect(page.locator("#consent-person")).to_have_text("alice")
    expect(page.locator("#consent-client")).to_have_text(CLIENT)
    expect(page.locator("#consent-redirect")).to_have_text("a program on this computer, at localhost:41234")
    expect(page.locator("#consent-confirm")).to_be_hidden()  # the check is fresh
    expect(page.locator("#consent-device")).to_be_focused()

    page.click("#consent-approve")  # no name yet
    expect(page.locator("#consent-error")).to_have_text("Name this device, for example work-laptop.")
    page.fill("#consent-device", "1 bad")
    page.click("#consent-approve")
    expect(page.locator("#consent-error")).to_contain_text("looks like work-laptop")
    assert got == [] and b.on_loop(lambda: b.state.store.agent_tokens_of(None)) == []  # nothing was sent
    page.fill("#consent-device", "Work-Laptop")
    page.click("#consent-approve")
    expect(page).to_have_url(AT_THE_AGENT)
    assert len(got) == 1 and got[0].startswith(REDIRECT + "?"), (got, page.url)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(got[0]).query)
    assert q["state"] == [agent.state] and set(q) == {"code", "state"}
    t = agent.exchange(q["code"][0])
    assert t.status_code == 200, t.text
    row = b.on_loop(lambda: b.state.store.agent_token_for_device("work-laptop"))
    assert row is not None and row.token_hash == agent_tokens.token_hash(t.json()["access_token"])
    assert row.via == "oauth" and row.client_name == CLIENT

    app = page.context.new_page()
    app.goto(b.state.web_origin.origin + "/")
    expect(app.locator("#st-conn")).to_have_text("Connected")
    open_settings(app, "agents")
    item = app.locator("#agent-tokens .agent-token")
    expect(item).to_have_count(1)
    expect(item.locator(".agent-token-device")).to_have_text("work-laptop")
    expect(item.locator(".agent-token-meta")).to_contain_text(f"signed in from {CLIENT} · made")
    app.click(f"#agent-token-ask-{row.id}")
    expect(app.locator(f"#agent-token-revoke-{row.id}")).to_have_attribute(
        "aria-label", "Revoke the sign-in for work-laptop: its agents stop working at once"
    )
    app.click(f"#agent-token-revoke-{row.id}")
    expect(item).to_have_count(0)
    expect(app.locator("#agent-token-result")).to_have_text("Revoked the sign-in for work-laptop.")
    assert b.on_loop(lambda: b.state.store.agent_token_by_hash(row.token_hash)) is None


def test_deny_sends_the_agent_access_denied(ui: UI, hosted: tuple[InProcBroker, str]) -> None:
    """Deny answers the agent at its own address with ``access_denied`` and its state, so it stops
    waiting, and makes nothing. Fails if Deny leaves the agent hanging or a token behind."""
    b, _ = hosted
    revoke_all(hosted)
    checked_now(hosted)
    agent = Agent(b.state.web_origin.origin)
    page, got = open_consent(ui, hosted, agent)
    page.click("#consent-deny")
    expect(page).to_have_url(AT_THE_AGENT)
    assert got == [f"{REDIRECT}?error=access_denied&state={agent.state}"]
    assert b.on_loop(lambda: b.state.store.agent_tokens_of(None)) == []


def test_a_hosted_broker_asks_to_confirm_it_is_you_when_the_check_ran_out(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """With the five minutes up, the card shows Confirm it's you; a wrong password says so and approves
    nothing, the right one carries straight on to the approval. Fails if Approve works without the check, or
    if the page forgets the name the person typed."""
    b, _ = hosted
    revoke_all(hosted)
    b.on_loop(lambda: b.state.passkey_checks.clear())
    agent = Agent(b.state.web_origin.origin)
    page, got = open_consent(ui, hosted, agent)
    expect(page.locator("#consent-confirm")).to_be_visible()
    page.fill("#consent-device", "studio")
    page.click("#consent-approve")  # the password is empty
    expect(page.locator("#consent-error")).to_contain_text("Type your password")
    page.fill("#consent-password", "not the password")
    page.click("#consent-approve")
    expect(page.locator("#consent-error")).to_have_text("wrong password")
    page.wait_for_timeout(300)  # Chromium logs the refused request a moment after the DOM changed
    assert ui.problems and all("403" in p and "/api/signin/check" in p for p in ui.problems), ui.problems
    ui.problems.clear()
    assert got == [] and b.on_loop(lambda: b.state.store.agent_tokens_of(None)) == []
    page.fill("#consent-password", ADMIN_PW)
    page.click("#consent-approve")
    expect(page).to_have_url(AT_THE_AGENT)
    assert len(got) == 1 and "code=" in got[0]
    assert (
        agent.exchange(urllib.parse.parse_qs(urllib.parse.urlsplit(got[0]).query)["code"][0]).status_code
        == 200
    )
    assert b.on_loop(lambda: b.state.store.agent_token_for_device("studio")) is not None
    revoke_all(hosted)


def test_a_signed_out_browser_is_asked_to_sign_in_and_goes_on_when_it_has(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """With no session the page says to sign in, with a link to the sign-in page; once a session exists in
    the browser (here, the cookie arriving as it does after signing in in another tab) the page notices and
    shows the card on its own. Fails if the request is dropped by signing in or the page stays stuck."""
    b, cookie = hosted
    origin = b.state.web_origin.origin
    revoke_all(hosted)
    checked_now(hosted)
    agent = Agent(origin)
    page, _ = open_consent(ui, hosted, agent, signed_in=False)
    expect(page.locator("#step-signin")).to_be_visible()
    expect(page.locator("#oauth-title")).to_have_text("Sign in to approve")
    expect(page.locator("#signin-link")).to_have_attribute("href", "/")
    expect(page.locator("#signin-hosted")).to_be_visible()
    expect(page.locator("#step-consent")).to_be_hidden()
    page.context.add_cookies([{"name": "switchboard_session", "value": cookie, "url": origin}])
    expect(page.locator("#step-consent")).to_be_visible(timeout=10_000)
    expect(page.locator("#consent-person")).to_have_text("alice")


def test_a_request_that_cannot_continue_says_so_and_asks_for_nothing(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """An unknown client or an address the agent never registered is shown as an error: no card, no
    sign-in prompt, no Approve. Fails if the person is asked to approve something that was refused."""
    b, _ = hosted
    agent = Agent(b.state.web_origin.origin)
    ctx = ui.context()
    page = ctx.new_page()
    resp = page.goto(agent.url(redirect_uri="http://localhost:41234/elsewhere"))
    assert resp is not None and resp.status == 400
    expect(page.locator("#oauth-title")).to_have_text("This sign-in can’t continue")
    expect(page.locator("#error-text")).to_contain_text("isn't the one it registered")
    expect(page.locator("#step-consent")).to_be_hidden()
    expect(page.locator("#step-signin")).to_be_hidden()
    page.goto(agent.url(client_id="sbc_nobody123456"))
    expect(page.locator("#error-text")).to_contain_text("may have expired")
    page.wait_for_timeout(300)
    # Chromium logs each refused request as a console error (the page itself, and the check the page
    # asks the broker): those 400s are expected, nothing else is
    assert ui.problems and all("status of 400" in p for p in ui.problems), ui.problems
    ui.problems.clear()


def test_the_phone_fits_the_card_without_a_sideways_scroll(ui: UI, hosted: tuple[InProcBroker, str]) -> None:
    """On a phone the card, with the facts stacked and the confirm step showing, fits the width and both
    buttons are reachable. Fails if anything pushes the page sideways or off the screen."""
    b, _ = hosted
    revoke_all(hosted)
    b.on_loop(lambda: b.state.passkey_checks.clear())
    agent = Agent(b.state.web_origin.origin, name="A very long name for an agent " * 3)
    page, _ = open_consent(ui, hosted, agent, **PHONE)
    expect(page.locator("#step-consent")).to_be_visible()
    expect(page.locator("#consent-confirm")).to_be_visible()
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    for sel in ("#consent-approve", "#consent-deny", "#consent-device", "#consent-password"):
        box = page.locator(sel).bounding_box()
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= PHONE["viewport"]["width"], (
            sel
        )
    expect(page.locator("#consent-client")).to_have_text(("A very long name for an agent " * 3).strip()[:80])


def test_the_page_builds_nothing_from_the_request(ui: UI, hosted: tuple[InProcBroker, str]) -> None:
    """A name with markup in it is text on the card and in Settings, never an element. Fails if the client's
    own name is parsed as HTML."""
    b, _ = hosted
    revoke_all(hosted)
    checked_now(hosted)
    agent = Agent(b.state.web_origin.origin, name="<img src=x onerror=alert(1)>")
    page, got = open_consent(ui, hosted, agent)
    expect(page.locator("#consent-client")).to_have_text("<img src=x onerror=alert(1)>")
    assert page.locator("#step-consent img").count() == 0
    page.fill("#consent-device", "markup")
    page.click("#consent-approve")
    expect(page).to_have_url(AT_THE_AGENT)
    assert (
        agent.exchange(urllib.parse.parse_qs(urllib.parse.urlsplit(got[0]).query)["code"][0]).status_code
        == 200
    )
    app = page.context.new_page()
    app.goto(b.state.web_origin.origin + "/")
    open_settings(app, "agents")
    expect(app.locator("#agent-tokens .agent-token-meta")).to_contain_text(
        "signed in from <img src=x onerror=alert(1)>"
    )
    assert app.locator("#agent-tokens img").count() == 0
    revoke_all(hosted)


def test_the_page_only_opens_the_agents_own_loopback_address(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """The broker only ever answers with the redirect the agent registered, but the page checks the
    address it is about to open anyway: given any other, it stays put and says so. Fails if the page
    follows an address that is not a loopback one."""
    b, _ = hosted
    revoke_all(hosted)
    checked_now(hosted)
    agent = Agent(b.state.web_origin.origin)
    page, got = open_consent(ui, hosted, agent)
    page.route(
        "**/api/oauth/approve",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"redirect": "https://evil.example/callback?code=stolen"}',
        ),
    )
    page.fill("#consent-device", "decoy")
    page.click("#consent-approve")
    expect(page.locator("#consent-error")).to_contain_text("won’t open")
    assert page.url.startswith(b.state.web_origin.origin + "/oauth/authorize") and got == []
