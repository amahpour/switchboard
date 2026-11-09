"""The home page in a real browser (#626, DESIGN.md §55): it is the first screen, a question and a review
board call are answered from their cards with the room showing the same result, it updates live, and it works
on a phone and by keyboard alone. A hosted broker of its own with its admin signed in; the agents' messages
are
posted by stand-ins that have left (the page follows the broker's frames, not the agents). Marker ``e2e``."""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from fakes.fake_agent import FakeAgent
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHASE_REPORTS, PHONE, UI
from ui_world import wait_js

from switchboard.broker.auth import WebOrigin
from switchboard.models import session_key

pytestmark = pytest.mark.e2e

ADMIN_PW = "correct horse battery"
PR = "https://example.com/shop/pull/7"


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


class World:
    """Two rooms of their own in the shared hosted broker, and what is said and planted in them."""

    def __init__(self, hosted: tuple[InProcBroker, str], tag: str) -> None:
        self.b, self.cookie = hosted
        self.origin = self.b.state.web_origin.origin
        self.build = f"#build-{tag}"
        self.uploads = f"#uploads-{tag}"
        self.headers = {"Origin": self.origin, "X-Switchboard": "1"}
        with self.client() as c:
            for name in (self.build, self.uploads):
                assert c.post("/api/rooms", json={"name": name}).status_code == 200
            self.me = c.get("/api/me").json()["human"]

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.origin, headers=self.headers, cookies={"switchboard_session": self.cookie}
        )

    def agent_says(self, room: str, name: str, text: str, key: str) -> int:
        async def go() -> int:
            async with FakeAgent(self.b.home, key.replace("#", "")) as a:
                joined = await a.join(room, name)
                assert joined["ok"], joined
                return int((await a.say(room, text))["posted_id"])

        with ThreadPoolExecutor(1) as pool:  # the page's own thread already runs a loop
            return pool.submit(asyncio.run, go()).result(30)

    def plant_board(self, room: str) -> None:
        """An open review board with one question for a person, as agents leave it."""

        def run() -> None:
            store = self.b.state.store
            r = store.get_room(room)
            assert r is not None
            p = store.upsert_participant("test", session_key("test", "", f"claude-{room}"), host="")
            store.create_membership(r.id, p.id, "claude-1")
            rv = store.open_review(r.id, PR, "", "claude-1")
            store.add_review_item(
                rv["id"],
                "question",
                "open",
                title="Keep v1?",
                raised_by="claude-1",
                options=["Keep", "Remove"],
            )

        self.b.on_loop(run)

    def messages(self, room: str) -> list[dict[str, Any]]:
        with self.client() as c:
            return list(c.get(f"/api/rooms/{room.lstrip('#')}/messages").json()["messages"])


def open_home(ui: UI, w: World, **options: Any) -> Page:
    """A page opened on the bare address: Home, with nothing chosen."""
    ctx = ui.context(**options)
    ctx.add_cookies([{"name": "switchboard_session", "value": w.cookie, "url": w.origin}])
    page = ctx.new_page()
    page.goto(w.origin + "/")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    return page


def no_sideways_scroll(page: Page) -> None:
    """Nothing is cut off or wider than the screen: the page does not scroll sideways and no control of Home
    sticks out past the right edge."""
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert wide <= 0, f"the page scrolls sideways by {wide}px"
    out = page.evaluate(
        """() => Array.from(document.querySelectorAll('#home button, #home .hc-body')).filter(
            e => e.getBoundingClientRect().right > window.innerWidth + 0.5).map(e => e.className)"""
    )
    assert out == [], f"cut off on the right: {out}"


@pytest.mark.parametrize("phone", [False, True])
def test_home_is_the_first_screen_and_a_question_is_answered_from_its_card(
    ui: UI, hosted: tuple[InProcBroker, str], phone: bool
) -> None:
    """Opening the bare address shows Home, not a room: its four sections, the question to this person as a
    card with its room and who asked. Reply is reached and used by keyboard alone, with a visible focus ring;
    Ctrl+Enter sends it, the card and the sidebar's count go, and the room shows the reply as a reply to that
    message. A new question that arrives while the page is open shows without a reload. The phone: nothing is
    cut off or scrolls sideways, and the buttons are big enough to press. Fails if a room opens first, the
    answer is not the room's, or a card needs the mouse."""
    w = World(hosted, "phone" if phone else "desk")
    ask = w.agent_says(
        w.build, "planner", f"@{w.me} keep the old upload endpoint, or remove it?", f"k-{w.build}-q"
    )
    page = open_home(ui, w, **(PHONE if phone else {}))
    expect(page.locator("#room-title")).to_have_text("Home")
    expect(page.locator("#home")).to_be_visible()
    expect(page.locator("#composer")).to_be_hidden()
    heads = [t.split("\n")[0].split("·")[0].strip() for t in page.locator("#home h2").all_inner_texts()]
    assert [h.rstrip("0123456789 ") for h in heads] == [
        "Waiting on you",
        "Done since you last looked",
        "Live now",
        "Rooms",
    ]
    card = page.locator("#home .home-card", has_text=w.build)
    expect(card).to_have_count(1)
    expect(card).to_contain_text("planner asked you a question in " + w.build)
    expect(card).to_contain_text("keep the old upload endpoint")
    if phone:
        no_sideways_scroll(page)
        box = card.locator(".home-act").bounding_box()
        assert box is not None and box["height"] >= 44
        page.locator("#rooms-toggle").click()  # the phone's room list is the drawer
        expect(page.locator("#home-nav")).to_be_visible()
        page.keyboard.press("Escape")
    before = int(page.locator("#home-badge").inner_text())  # the broker is shared: other tests left cards

    # by keyboard: Tab from the sidebar to the card's Reply, open it with Enter, type, Ctrl+Enter
    reply = card.locator(".home-act")
    reply.focus()
    ring = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    assert ring != "none"
    page.keyboard.press("Enter")
    box_ = page.locator("#home textarea")
    expect(box_).to_be_focused()
    page.keyboard.press("Escape")  # closes it and the keyboard is back on the button
    expect(page.locator("#home textarea")).to_have_count(0)
    expect(reply).to_be_focused()
    page.keyboard.press("Enter")
    page.keyboard.type("Remove it.")
    # a refresh while it is half typed (another room's message) keeps the draft
    w.agent_says(w.uploads, "builder", "just working", f"k-{w.uploads}-a")
    expect(page.locator("#home")).to_contain_text("builder")
    expect(page.locator("#home textarea")).to_have_value("Remove it.")
    page.keyboard.press("Control+Enter")
    expect(page.locator("#home .home-card", has_text=w.build)).to_have_count(0)
    wait_js(
        page,
        "n => { const b = document.getElementById('home-badge');"
        " return b.classList.contains('hidden') || Number(b.textContent) < n }",
        before,
    )
    sent = [m for m in w.messages(w.build) if m["text"] == "Remove it."]
    assert len(sent) == 1 and sent[0]["reply_to"] == ask
    # the room shows the same message
    page.locator("#tabs .room", has_text=w.build.lstrip("#")).first.click() if not phone else None
    if phone:
        page.locator("#rooms-toggle").click()
        page.locator("#tabs .room", has_text=w.build.lstrip("#")).first.click()
    expect(page.locator("#log")).to_contain_text("Remove it.")
    expect(page.locator("#home")).to_be_hidden()

    # back on Home, live: a new question appears without a reload
    page.locator("#home-nav").click() if not phone else None
    if phone:
        page.locator("#rooms-toggle").click()
        page.locator("#home-nav").click()
    expect(page.locator("#room-title")).to_have_text("Home")
    w.agent_says(w.uploads, "scout", f"@{w.me} which limit?", f"k-{w.uploads}-s")
    expect(page.locator("#home .home-card", has_text=w.uploads)).to_have_count(1)
    expect(page.locator("#home .home-card", has_text=w.uploads)).to_contain_text("which limit?")
    if phone:
        no_sideways_scroll(page)


def test_a_review_board_call_is_made_from_its_card_and_the_room_shows_it(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """A question on a review board that needs a person is a card: Decide lists it with its options, an option
    sends the board's own answer move, the card goes, and the room's board shows the question answered.
    Fails if
    the move is not the board's, or the room does not show it."""
    w = World(hosted, "board")
    w.plant_board(w.build)
    w.agent_says(w.uploads, "builder", "poke", f"k-{w.uploads}-b")  # a frame, after the board is planted
    page = open_home(ui, w)
    card = page.locator("#home .home-card", has_text="review board in " + w.build)
    expect(card).to_contain_text("1 item to call on the review board in " + w.build)
    card.get_by_role("button", name=re.compile("Decide")).click()
    expect(card).to_contain_text("Q1 Keep v1?")
    card.get_by_role("button", name="Remove").click()
    expect(page.locator("#home .home-card", has_text="review board in " + w.build)).to_have_count(0)
    texts = [m["text"] for m in w.messages(w.build)]
    assert any("Q1 answered with option 1" in t for t in texts), texts
    page.locator("#tabs .room", has_text=w.build.lstrip("#")).first.click()
    expect(page.locator("#log")).to_contain_text("Q1 answered with option 1")


def test_leaving_home_moves_since_you_last_looked_forward(ui: UI, hosted: tuple[InProcBroker, str]) -> None:
    """What an agent said while nobody looked is a done line; after the person opens that room, the next time
    they open Home it is no longer there. Fails if the line stays, or is missing the first time."""
    w = World(hosted, "seen")
    w.agent_says(w.uploads, "builder", "raised the upload limit", f"k-{w.uploads}-c")
    page = open_home(ui, w)
    row = page.locator("#home .home-rowbtn", has_text="raised the upload limit")
    expect(row).to_have_count(1)
    row.click()
    expect(page.locator("#log")).to_contain_text("raised the upload limit")
    again = open_home(ui, w)
    expect(again.locator("#home")).to_contain_text("Rooms")
    expect(again.locator("#home .home-rowbtn", has_text="raised the upload limit")).to_have_count(0)


def test_a_question_card_goes_once_the_room_is_opened_and_a_new_question_brings_it_back(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """#663: press Go on a question card, read the room, come back to Home: the card is gone (and stays gone
    after a reload, because the broker keeps the mark); a second question to the person is a card again, with
    no "and more" under it for the one already seen. Fails if the card is still there after the room was
    opened, comes back with the old question, or the new question does not show."""
    w = World(hosted, "goseen")
    w.agent_says(w.build, "planner", f"@{w.me} keep the old upload endpoint?", f"k-{w.build}-g")
    page = open_home(ui, w)
    card = page.locator("#home .home-card", has_text=w.build)
    expect(card).to_have_count(1)
    card.get_by_role("button", name=re.compile("^Open ")).click()
    expect(page.locator("#log")).to_contain_text("keep the old upload endpoint")
    page.locator("#home-nav").click()
    expect(page.locator("#room-title")).to_have_text("Home")
    expect(page.locator("#home .home-card", has_text=w.build)).to_have_count(0)
    page.reload()
    expect(page.locator("#room-title")).to_have_text("Home")
    expect(page.locator("#home .home-card", has_text=w.build)).to_have_count(0)
    w.agent_says(w.build, "scout", f"@{w.me} and the new endpoint?", f"k-{w.build}-h")
    again = page.locator("#home .home-card", has_text=w.build)
    expect(again).to_have_count(1)
    expect(again).to_contain_text("and the new endpoint?")
    expect(again).not_to_contain_text("more in this room")


def test_not_now_hides_a_board_card_and_it_comes_back_when_the_board_changes(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """#663: Not now takes a review board card off Home, by mouse and by keyboard (focus moves on, not into
    nothing), and it stays off after a reload; a new item on that board brings the card back. Fails if the
    card stays, returns on a reload with nothing changed, or never returns."""
    w = World(hosted, "later")
    w.plant_board(w.build)
    w.agent_says(w.uploads, "builder", "poke", f"k-{w.uploads}-l")  # a frame, after the board is planted
    page = open_home(ui, w)
    title = "review board in " + w.build
    card = page.locator("#home .home-card", has_text=title)
    expect(card).to_have_count(1)
    card.get_by_role("button", name=re.compile("^Not now")).focus()
    ring = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    assert ring != "none"
    page.keyboard.press("Enter")
    expect(page.locator("#home .home-card", has_text=title)).to_have_count(0)
    page.reload()
    expect(page.locator("#room-title")).to_have_text("Home")
    expect(page.locator("#home .home-card", has_text=title)).to_have_count(0)

    def add_item() -> None:
        store = w.b.state.store
        r = store.get_room(w.build)
        assert r is not None
        rv = store.open_reviews(r.id)[0]
        store.add_review_item(
            rv["id"], "question", "open", title="And v2?", raised_by="claude-1", options=["a", "b"]
        )

    w.b.on_loop(add_item)
    back = open_home(ui, w)
    expect(back.locator("#home .home-card", has_text=title)).to_have_count(1)
    expect(back.locator("#home .home-card", has_text=title)).to_contain_text("2 items to call")


def test_a_board_card_with_not_now_fits_a_phone(ui: UI, hosted: tuple[InProcBroker, str]) -> None:
    """#663: the board card has three buttons (Decide, Not now, Go); on a 390 px phone they wrap under the card's
    words, each at least 44 px tall, and nothing is cut off or scrolls sideways. Fails if the third button
    sticks out past the card's edge."""
    w = World(hosted, "narrowboard")
    w.plant_board(w.build)
    w.agent_says(w.uploads, "builder", "poke", f"k-{w.uploads}-p")
    page = open_home(ui, w, **PHONE)
    card = page.locator("#home .home-card", has_text="review board in " + w.build)
    expect(card).to_have_count(1)
    no_sideways_scroll(page)
    right = card.evaluate("c => c.getBoundingClientRect().right")
    for b in card.locator("button").all():
        box = b.bounding_box()
        assert box is not None and box["x"] + box["width"] <= right + 0.5 and box["height"] >= 44
