"""Mentioning a person from the page (#418, DESIGN.md §29, §32), on a hosted broker with alice (the
admin), bob (who signs in), carol and one agent. The @ popover lists the room's people after the
agents that match, a private room's own people only, and another person's @mention of you raises
the same red badge an agent's does: the room's tab, the phone's Rooms badge and the title count.
A person's mention reaches no agent: the real broker, a real agent, a real click on the popover."""

from __future__ import annotations

import asyncio
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NamedTuple

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from fakes.fake_agent import FakeAgent
from playwright.sync_api import Browser, Page, expect
from test_private_rooms_ui import person_cookie
from test_web_ui import PHASE_REPORTS, PHONE, UI, chat_row, cls

from switchboard.broker.auth import WebOrigin

pytestmark = pytest.mark.e2e

ADMIN_PW = "correct horse battery"


class Hosted(NamedTuple):
    broker: InProcBroker
    admin: str  # alice's session cookie
    bob: str  # bob's


@pytest.fixture(scope="module")
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Hosted]:
    """A hosted broker set up through its API: alice, the admin, with a password; bob, who signs
    in; carol, who is only on the list; and the room #build."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://sb.localhost:{port}")).start()
        origin = b.state.web_origin.origin
        one_time = b.paths.test_claim_link.read_text().strip().split("#t=", 1)[1]
        h = {"Origin": origin, "X-Switchboard": "1"}
        with httpx.Client(base_url=origin, headers=h) as c:
            signed = c.post("/api/signin/password", json={"name": "admin", "password": one_time})
            assert signed.status_code == 200, signed.text
            assert c.post("/api/setup/password", json={"password": ADMIN_PW}).status_code == 200
            assert c.post("/api/rooms", json={"name": "#build"}).status_code == 200
            added = c.post("/api/people", json={"name": "carol", "email": "carol@example.com"})
            assert added.status_code == 200, added.text
            admin = c.cookies.get("switchboard_session")
        assert admin
        try:
            yield Hosted(b, admin, person_cookie((b, admin), "bob"))
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


class Agents:
    """Test agents on an event loop of their own, for a test that is not async: join one to a
    room, read what it was sent. Closed with the test."""

    def __init__(self, broker: InProcBroker) -> None:
        self.broker = broker
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="e2e-agents", daemon=True)
        self.thread.start()
        self.agents: dict[str, FakeAgent] = {}

    def run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(60)

    def join(self, room: str, name: str) -> None:
        agent = FakeAgent(self.broker.home, f"mention-people-{name}")
        self.run(agent.start())
        self.agents[name] = agent
        joined = self.run(agent.join(room, name))
        assert joined["ok"], joined

    def read(self, room: str, name: str) -> str:
        got = self.run(self.agents[name].read(room))
        assert got["ok"], got
        return str(got["text"])

    def close(self) -> None:
        for agent in self.agents.values():
            self.run(agent.close())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


@pytest.fixture
def agents(hosted: Hosted) -> Iterator[Agents]:
    a = Agents(hosted.broker)
    try:
        yield a
    finally:
        a.close()


def page_for(ui: UI, hosted: Hosted, cookie: str, **options: Any) -> Page:
    """A page signed in with ``cookie`` (alice's or bob's), connected."""
    origin = hosted.broker.state.web_origin.origin
    ctx = ui.context(**options)
    ctx.add_cookies([{"name": "switchboard_session", "value": cookie, "url": origin}])
    page = ctx.new_page()
    page.goto(origin + "/")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    return page


def post(hosted: Hosted, cookie: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST to the API as the person with ``cookie``, as the page does."""
    origin = hosted.broker.state.web_origin.origin
    headers = {"Origin": origin, "X-Switchboard": "1"}
    with httpx.Client(base_url=origin, headers=headers, cookies={"switchboard_session": cookie}) as c:
        r = c.post(path, json=body)
        assert r.status_code == 200, r.text
        out: dict[str, Any] = r.json()
        return out


@contextmanager
def room_of_its_own(hosted: Hosted, name: str) -> Iterator[None]:
    """A new public room, closed again after the test: a page replays a room's history, so a
    mention left in it would count in the next test's badges and title."""
    post(hosted, hosted.admin, "/api/rooms", {"name": f"#{name}"})
    try:
        yield
    finally:
        post(hosted, hosted.admin, f"/api/rooms/{name}/command", {"text": "/close"})


def open_room(page: Page, name: str) -> None:
    """Open a room from the sidebar, the drawer first at phone width (where it is off screen
    while Playwright still calls its rooms visible, so the toggle is what says it is shut)."""
    tab = page.locator(f'#tabs .room[data-room="#{name}"]')
    if page.locator("#rooms-toggle").is_visible() and page.locator("#app.nav-open").count() == 0:
        page.click("#rooms-toggle")
    tab.click()
    expect(page.locator("#room-title")).to_have_text(name)


def test_the_popover_lists_people_and_picking_one_mentions_them_without_waking_an_agent(
    ui: UI, hosted: Hosted, agents: Agents
) -> None:
    """Issue #418: alice types @bo in #build and is offered the agent helper-bob (its name has
    "bob" in it) and then bob, a person, with the person avatar and no tier chip; bob shows as
    away until his page opens, and then as online, in the open popover. She is never offered
    herself. Picking bob inserts @bob, the composer highlights it, and once sent it is a mention in
    the log; the agent hears it as any person's message, and not as addressed to it. Fails on the
    first step if the popover lists only agents, as it did."""
    agents.join("#build", "helper-bob")
    page = page_for(ui, hosted, hosted.admin)
    open_room(page, "build")
    box = page.locator("#input")
    mentions = page.locator("#mentions")
    box.focus()

    page.keyboard.type("@bo")
    options = mentions.locator("[role=option]")
    expect(options).to_have_count(2)
    expect(options.nth(0)).to_have_attribute("id", "men-helper-bob")
    expect(options.nth(1)).to_have_attribute("id", "men-bob")
    expect(mentions.locator(".pal-head strong")).to_have_text(["Agents in #build", "People in #build"])
    bob = mentions.locator("#men-bob")
    expect(bob.locator(".avatar.human")).to_have_count(1)
    expect(bob.locator(".tier")).to_have_count(0)  # an agent has its tier chip; nothing wakes a person
    expect(bob.locator(".m-status")).to_have_text("Offline · Person")
    expect(mentions.locator("#men-helper-bob .tier")).to_have_count(1)

    # bob opens his page: the open popover follows, without a keystroke
    bob_page = page_for(ui, hosted, hosted.bob)
    open_room(bob_page, "build")
    expect(bob.locator(".m-status")).to_have_text("Online · Person")
    expect(bob.locator(".dot")).to_have_class(cls("s-human"))

    box.fill("@ali")  # alice is the viewer, and nothing else matches
    expect(mentions).to_be_hidden()
    box.fill("")

    page.keyboard.type("@bo")
    page.keyboard.press("ArrowDown")  # helper-bob, then bob
    page.keyboard.press("Enter")
    expect(box).to_have_value("@bob ")
    expect(page.locator("#input-mirror .mention-hl")).to_have_text("@bob")
    page.keyboard.type("can you take a look?")
    page.keyboard.press("Enter")
    row = chat_row(page, "can you take a look?")
    expect(row.locator(".md-mention")).to_have_text("@bob")
    expect(chat_row(bob_page, "can you take a look?")).to_be_visible()

    heard = agents.read("#build", "helper-bob")
    assert "can you take a look?" in heard, heard  # a person's message reaches every agent, as ever
    assert "to_you=yes" not in heard, heard  # but @bob addressed no agent


def test_a_private_room_offers_only_its_own_people(ui: UI, hosted: Hosted) -> None:
    """Issue #418: in a private room alice offers bob once he is in it and never carol, who is
    not; the public room offers both. Fails if a private room offers someone who can't see it."""
    post(hosted, hosted.admin, "/api/rooms", {"name": "#tiny", "private": True})
    page = page_for(ui, hosted, hosted.admin)
    box = page.locator("#input")
    mentions = page.locator("#mentions")

    open_room(page, "tiny")
    expect(page.locator("#room-people .group-label")).to_have_text("People (1) · private")
    box.focus()
    page.keyboard.type("@")
    expect(mentions.locator("[role=option]")).to_have_count(3)  # @humans, @here, @everyone: no one else yet
    box.fill("")

    page.click("#room-people-add")
    page.select_option("#app-dialog-person", "bob")
    page.click("#app-dialog-action")
    expect(page.locator("#room-people .group-label")).to_have_text("People (2) · private")
    box.focus()  # the dialog took the focus
    page.keyboard.type("@")
    expect(mentions.locator("#men-bob")).to_have_count(1)
    expect(mentions.locator("#men-carol")).to_have_count(0)
    expect(mentions.locator(".pal-head strong").last).to_have_text("People in #tiny")
    box.fill("")

    open_room(page, "build")
    box.focus()
    page.keyboard.type("@")
    expect(mentions.locator("#men-bob")).to_have_count(1)
    expect(mentions.locator("#men-carol")).to_have_count(1)
    expect(mentions.locator("#men-alice")).to_have_count(0)  # never the viewer
    box.fill("")


@pytest.mark.parametrize("phone", [False, True])
def test_another_persons_mention_raises_the_badge_but_your_own_does_not(
    ui: UI, hosted: Hosted, phone: bool
) -> None:
    """Issue #418: bob's @alice in a room alice is not viewing, is a red badge on the room's
    tab, a count in the title and, on a phone, on the Rooms toggle -- as an agent's @alice would
    be. Bob talking without naming her, and naming carol, are not. Alice's own @bob in the same
    room is no badge for her (and is one for bob, in his own page); opening the room clears it,
    and bob's message is a mention line. Fails if a person's mention is only ever an agent's."""
    options: dict[str, Any] = PHONE if phone else {}
    room = "watched-phone" if phone else "watched"
    with room_of_its_own(hosted, room):
        alice = page_for(ui, hosted, hosted.admin, **options)
        bob = page_for(ui, hosted, hosted.bob)
        open_room(alice, "build")
        open_room(bob, "build")
        tab = alice.locator(f'#tabs .room[data-room="#{room}"]')
        mention = "alice, is the build green?" + (" (phone)" if phone else "")

        # three lines in the room that are not a mention of alice (bob's two, and her own @bob), then
        # bob's @alice. A line in #build, the room alice is looking at, comes last: the socket
        # delivers in order, so once it shows, every line in the other room has been counted or not, and the
        # badge below reads what they added in all, not what it read on the way
        post(hosted, hosted.bob, f"/api/rooms/{room}/say", {"text": "anyone around?"})
        post(hosted, hosted.bob, f"/api/rooms/{room}/say", {"text": "@carol can you look?"})
        post(hosted, hosted.admin, f"/api/rooms/{room}/say", {"text": "@bob yes, it is"})
        post(hosted, hosted.bob, f"/api/rooms/{room}/say", {"text": f"@alice {mention}"})
        done = "that is all for now" + (" (phone)" if phone else "")
        post(hosted, hosted.bob, "/api/rooms/build/say", {"text": done})
        expect(chat_row(alice, done)).to_be_visible()
        expect(chat_row(bob, done)).to_be_visible()

        badge = tab.locator(".badge")
        expect(badge).to_have_text("1")  # bob's @alice only
        expect(badge).to_have_attribute("aria-label", "1 message for you")
        expect(tab.locator(".dot")).to_have_count(0)  # a person's line leaves no quiet dot either
        expect(alice).to_have_title("(1) switchboard — #build")
        if phone:
            expect(alice.locator("#rooms-badge")).to_have_text("1")
            expect(alice.locator("#rooms-badge")).to_be_visible()

        # alice's own @bob is a badge for bob, on his page, and (above) none for her
        expect(bob.locator(f'#tabs .room[data-room="#{room}"] .badge')).to_have_text("1")

        open_room(alice, room)
        expect(badge).to_have_count(0)
        expect(alice).to_have_title(f"switchboard — #{room}")
        expect(chat_row(alice, mention)).to_have_class(cls("mention"))
        for other in ("anyone around?", "@carol can you look?", "@bob yes, it is"):
            expect(chat_row(alice, other)).not_to_have_class(cls("mention"))
