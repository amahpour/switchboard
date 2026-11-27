"""A public room's members in the page (#843, DESIGN.md §63), on a hosted broker with alice (the admin), bob
and carol: the sidebar lists the rooms alice is in and folds the rest under Other rooms; a room she is not
in is a read-only preview with a Join bar; Leave is in the room's menu and on her own row in Members, after
a confirm; @humans in a room she is not in gives her no badge. A desktop broker, one person, shows none of
it. Desktop light, desktop dark and the phone. A console error, a page error or a CSP violation fails the
test."""

from __future__ import annotations

import itertools
import re
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
NAMES = itertools.count(1)
THEMES = [
    pytest.param({"color_scheme": "light"}, id="desktop-light"),
    pytest.param({"color_scheme": "dark"}, id="desktop-dark"),
    pytest.param({**PHONE, "color_scheme": "light"}, id="phone"),
]


class Team:
    """The hosted broker and each person's session cookie, with a short way to call its API as one of them."""

    def __init__(self, b: InProcBroker, cookies: dict[str, str]) -> None:
        self.b = b
        self.cookies = cookies
        self.origin = b.state.web_origin.origin

    def api(self, who: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        h = {"Origin": self.origin, "X-Switchboard": "1"}
        with httpx.Client(
            base_url=self.origin, headers=h, cookies={"switchboard_session": self.cookies[who]}
        ) as c:
            r = c.request(method, path, json=body)
        assert r.status_code == 200, r.text
        return r.json()

    def room(self, who: str, stem: str) -> str:
        """A new public room made by ``who`` (its first member), with a name no other test uses."""
        name = f"#{stem}-{next(NAMES)}"
        self.api(who, "POST", "/api/rooms", {"name": name})
        return name


@pytest.fixture(scope="module")
def team(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Team]:
    """A hosted broker set up through its API: alice (the admin) with a password, bob and carol added and
    signed in with passwords of their own."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://sb.localhost:{port}")).start()
        origin = b.state.web_origin.origin
        one_time = b.paths.test_claim_link.read_text().strip().split("#t=", 1)[1]
        h = {"Origin": origin, "X-Switchboard": "1"}
        cookies: dict[str, str] = {}
        with httpx.Client(base_url=origin, headers=h) as c:
            assert (
                c.post("/api/signin/password", json={"name": "admin", "password": one_time}).status_code
                == 200
            )
            assert c.post("/api/setup/password", json={"password": ADMIN_PW}).status_code == 200
            cookies["alice"] = c.cookies.get("switchboard_session") or ""
            added = {
                n: c.post("/api/people", json={"name": n, "email": f"{n}@example.com"})
                for n in ("bob", "carol")
            }
        for name, r in added.items():
            assert r.status_code == 200, r.text
            with httpx.Client(base_url=origin, headers=h) as c:
                signed = c.post(
                    "/api/signin/password",
                    json={"name": f"{name}@example.com", "password": r.json()["password"]},
                )
                assert signed.status_code == 200, signed.text
                assert (
                    c.post("/api/me/password", json={"password": "correct horse " + name}).status_code == 200
                )
                cookies[name] = c.cookies.get("switchboard_session") or ""
        assert all(cookies.values())
        try:
            yield Team(b, cookies)
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


def alices_page(ui: UI, team: Team, **options: Any) -> Page:
    ctx = ui.context(**options)
    ctx.add_cookies([{"name": "switchboard_session", "value": team.cookies["alice"], "url": team.origin}])
    page = ctx.new_page()
    page.goto(team.origin + "/")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    return page


def is_phone(options: dict[str, Any]) -> bool:
    return bool(options.get("is_mobile"))


def sidebar(page: Page) -> None:
    """On a phone the sidebar is a drawer: open it."""
    if page.locator("#rooms-toggle").is_visible() and not page.locator("#app.nav-open").count():
        page.click("#rooms-toggle")


def no_sideways_scroll(page: Page) -> None:
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert wide <= 0, f"the page scrolls sideways by {wide}px"


def mine(page: Page, room: str) -> Any:
    """A room's row among the rooms alice is a member of (not under Other rooms)."""
    return page.locator(f'#tabs > .room[data-room="{room}"]')


def other(page: Page, room: str) -> Any:
    return page.locator(f'#other-rooms-list .room[data-room="{room}"]')


def unfold_other_rooms(page: Page) -> None:
    """Open the sidebar's Other rooms, wherever it starts. It starts open when alice is in no room (§63), and
    whether she is depends on what earlier tests left on this module's shared broker: a plain click folded it
    again in a test that ran before any of hers, and the room under it never showed."""
    toggle = page.locator("#other-rooms")
    expect(toggle).to_be_visible()
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")


@pytest.mark.parametrize("options", THEMES)
def test_your_rooms_are_listed_and_the_others_fold_under_other_rooms(
    ui: UI, team: Team, options: dict[str, Any]
) -> None:
    """alice's own room and one her agent joined are her rooms, the agent's with "joined with claude-2"; a
    room only bob is in waits under Other rooms, folded, and opens by keyboard with how many people are in
    it. Fails if every public room is in the list again, or if Other rooms can't be opened or loses focus."""
    own = team.room("alice", "own")
    agents = team.room("bob", "infra")
    side = team.room("bob", "side")

    def agent_joins() -> None:
        room = team.b.state.store.get_room(agents)
        assert room is not None
        team.b.state.service.agent_joined(room, 0, "claude-2")  # what an agent's join() does (§63)

    team.b.on_loop(agent_joins)
    page = alices_page(ui, team, **options)
    sidebar(page)
    expect(mine(page, own)).to_be_visible()
    expect(mine(page, agents)).to_contain_text("joined with claude-2")
    expect(mine(page, side)).to_have_count(0)
    toggle = page.locator("#other-rooms")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(toggle).to_contain_text(re.compile(r"Other rooms \(\d+\)"))
    expect(other(page, side)).to_have_count(0)
    toggle.focus()
    page.keyboard.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(toggle).to_be_focused()  # the redraw keeps the keyboard where it was
    expect(other(page, side)).to_contain_text("1 person")
    if is_phone(options):
        no_sideways_scroll(page)


@pytest.mark.parametrize("options", THEMES)
def test_a_room_you_are_not_in_is_a_preview_and_join_makes_it_yours(
    ui: UI, team: Team, options: dict[str, Any]
) -> None:
    """bob's room with an @humans in it gives alice no badge; opened, it is a preview: its messages, a Join
    bar where the composer was (with the keyboard on Join), a Preview chip, no room menu. Join makes it hers:
    the composer, the menu, the room among hers, and a notice that she joined. Fails if she can type where
    she can't post, if @humans there pings her, or if Join leaves the page in preview."""
    look = team.room("bob", "look")
    team.api("bob", "POST", f"/api/rooms/{look[1:]}/say", {"text": "@humans deploy at noon?"})
    page = alices_page(ui, team, **options)
    sidebar(page)
    unfold_other_rooms(page)
    row = other(page, look)
    expect(row.locator(".badge")).to_have_count(0)
    row.click()
    expect(page.locator("#log")).to_contain_text("deploy at noon?")
    expect(page.locator("#composer")).to_be_hidden()
    bar = page.locator("#preview-bar")
    expect(bar).to_be_visible()
    expect(page.locator("#preview-title")).to_have_text(f"You’re viewing {look}")
    expect(bar).to_contain_text("@humans here won’t reach you")
    join = page.locator("#preview-join")
    expect(join).to_have_text(f"Join {look}")
    expect(join).to_be_focused()
    expect(page.locator("#room-menu-wrap")).to_be_hidden()
    leave_row = page.locator('#more-list [data-more="leave"]')
    if is_phone(options):
        assert join.bounding_box()["height"] >= 44
        no_sideways_scroll(page)
        page.click("#room-more")  # the phone's room menu is the More sheet's Leave row (#995): none yet
        expect(page.locator("#more-list .more-item").first).to_be_focused()
        expect(leave_row).to_have_count(0)
        page.keyboard.press("Escape")
        join.focus()
    else:
        expect(page.locator("#room-preview")).to_be_visible()
    page.keyboard.press("Enter")
    expect(page.locator("#composer")).to_be_visible()
    expect(bar).to_be_hidden()
    expect(page.locator("#room-preview")).to_be_hidden()
    expect(page.locator("#input")).to_be_focused()
    if is_phone(options):
        page.click("#room-more")
        expect(leave_row).to_have_text(f"Leave {look}")
        page.keyboard.press("Escape")
    else:
        expect(page.locator("#room-menu-wrap")).to_be_visible()
    expect(page.locator("#log")).to_contain_text(f"alice joined {look}")
    sidebar(page)
    expect(mine(page, look)).to_be_visible()


@pytest.mark.parametrize("options", THEMES)
def test_leave_says_what_changes_and_keeps_the_room_readable(
    ui: UI, team: Team, options: dict[str, Any]
) -> None:
    """The room's menu opens by click, closes on Escape with the keyboard back on its button, and its Leave
    asks first, saying what stops and that her agents stay; Cancel changes nothing. Leaving makes the room a
    preview she can still read, under Other rooms, with the keyboard on Join. On a phone the menu is the More
    sheet's Leave row (the header is one row, #995). Fails if Leave acts without asking, if the confirm says
    something else, if leaving hides the room, or if a phone has no way to leave."""
    bye = team.room("alice", "bye")
    page = alices_page(ui, team, **options)
    sidebar(page)
    mine(page, bye).click()
    phone = is_phone(options)
    menu_btn = page.locator("#room-more" if phone else "#room-menu-btn")
    expect(menu_btn).to_be_visible()
    menu_btn.click()
    item = page.locator('#more-list [data-more="leave"]' if phone else "#room-menu-leave")
    expect(item).to_have_text(f"Leave {bye}")
    if not phone:
        expect(item).to_be_focused()  # the menu's one item; the More sheet starts on its first row
    page.keyboard.press("Escape")
    expect(page.locator("#more-sheet" if phone else "#room-menu")).to_be_hidden()
    expect(menu_btn).to_be_focused()
    menu_btn.click()
    item.click()
    dialog = page.locator("#app-dialog")
    expect(dialog.locator("#app-dialog-title")).to_have_text(f"Leave {bye}?")
    expect(dialog).to_contain_text("You will stop getting @humans, unread badges and notifications")
    expect(dialog).to_contain_text("Your agents there stay in the room")
    expect(dialog).to_contain_text("anything waiting on you there leaves Home")
    page.click("#app-dialog-cancel")
    expect(page.locator("#composer")).to_be_visible()
    menu_btn.click()
    item.click()
    page.click("#app-dialog-action")
    expect(page.locator("#preview-bar")).to_be_visible()
    expect(page.locator("#preview-join")).to_be_focused()
    expect(page.locator("#log")).to_contain_text(f"alice left {bye}")
    sidebar(page)
    expect(other(page, bye)).to_be_visible()  # opened: it is the room on screen
    if is_phone(options):
        no_sideways_scroll(page)


@pytest.mark.parametrize("options", THEMES)
def test_members_lists_the_rooms_people_with_leave_and_add(
    ui: UI, team: Team, options: dict[str, Any]
) -> None:
    """A public room's People are its members (not everyone on the broker): alice with "you" and Leave, a
    line on what being a member means, and Add a person, which offers bob and carol and adds the one picked.
    Leave on her row asks first. Fails if the list is everyone again, or the actions are missing."""
    crew = team.room("alice", "crew")
    page = alices_page(ui, team, **options)
    sidebar(page)
    mine(page, crew).click()
    if is_phone(options):
        page.click("#buddy-toggle")
    people = page.locator("#room-people")
    expect(people.locator(".group-label")).to_have_text("People (1)")
    me = people.locator('.person-row[data-name="alice"]')
    expect(me).to_contain_text("you")
    expect(me.locator("#room-people-leave")).to_have_text("Leave")
    expect(people.locator(".people-note")).to_have_text(
        "People here get @humans, unread badges and notifications. Agents still answer to everyone."
    )
    page.click("#room-people-add")
    pick = page.locator("#app-dialog-person")
    expect(pick.locator("option")).to_have_text(["bob", "carol"])
    pick.select_option("carol")
    page.click("#app-dialog-action")
    expect(people.locator(".group-label")).to_have_text("People (2)")
    expect(people.locator('.person-row[data-name="carol"]')).to_be_visible()
    expect(people.locator('.person-row[data-name="carol"] button')).to_have_count(0)  # nobody removes carol
    page.click("#room-people-leave")
    expect(page.locator("#app-dialog-title")).to_have_text(f"Leave {crew}?")
    page.click("#app-dialog-cancel")
    if is_phone(options):
        no_sideways_scroll(page)


@pytest.mark.parametrize("options", [THEMES[0], THEMES[2]])
def test_leave_from_your_row_in_people_puts_the_keyboard_on_join(
    ui: UI, team: Team, options: dict[str, Any]
) -> None:
    """Leave on alice's own row under People, confirmed, makes the room a preview with the keyboard on its
    Join button, as Leave from the room's menu does. On a phone People is in the Members sheet, which keeps
    the page behind it inert, so the sheet closes first; on a desktop Members is a column and stays. Fails if
    the sheet stays up over the room with the keyboard nowhere (review F1 on #879), or if a desktop's
    Members column closes with it."""
    gone = team.room("alice", "gone")
    page = alices_page(ui, team, **options)
    sidebar(page)
    mine(page, gone).click()
    app = page.locator("#app")
    sheet_open = re.compile(r"\bsheet-open\b")
    if is_phone(options):
        page.click("#buddy-toggle")
        expect(app).to_have_class(sheet_open)
    page.click("#room-people-leave")
    expect(page.locator("#app-dialog-title")).to_have_text(f"Leave {gone}?")
    page.click("#app-dialog-action")
    expect(page.locator("#log")).to_contain_text(f"alice left {gone}")
    expect(page.locator("#preview-bar")).to_be_visible()
    expect(app).not_to_have_class(sheet_open)
    assert page.evaluate("document.getElementById('main').inert") is False
    expect(page.locator("#preview-join")).to_be_focused()
    if not is_phone(options):
        expect(page.locator("#pane")).to_be_visible()


def test_at_humans_badges_only_your_rooms_and_home_waits_only_on_them(ui: UI, team: Team) -> None:
    """bob writes @humans in a room alice is in and in one she is not: the first has her red count and a
    card in Home's Waiting on you; the second has neither. Fails if a room she never joined pings her."""
    loud = team.room("alice", "loud")
    quiet = team.room("bob", "quiet")
    team.api("bob", "POST", f"/api/rooms/{loud[1:]}/people", {"action": "add", "name": "bob"})
    page = alices_page(ui, team)
    for room in (loud, quiet):
        team.api("bob", "POST", f"/api/rooms/{room[1:]}/say", {"text": "@humans can someone look?"})
    expect(mine(page, loud).locator(".badge")).to_have_text("1")
    unfold_other_rooms(page)
    expect(other(page, quiet)).to_be_visible()
    expect(other(page, quiet).locator(".badge")).to_have_count(0)
    page.click("#home-nav")
    cards = page.locator("#home .home-card")
    expect(cards.filter(has_text=loud[1:])).to_have_count(1)
    expect(cards.filter(has_text=quiet[1:])).to_have_count(0)


@pytest.fixture(scope="module")
def desktop(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    """A desktop broker: one person, with a room made before members counted (no row for anyone)."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home).start()
        b.on_loop(lambda: b.state.service.create_room("#build"))
        b.on_loop(lambda: b.state.store.create_room("#untouched", "alice", 60, 6))
        b.on_loop(b.state.service.rooms_changed)
        try:
            yield b
        finally:
            b.stop()
            shutil.rmtree(home, ignore_errors=True)


def test_one_person_sees_no_change(ui: UI, desktop: InProcBroker) -> None:
    """A broker with one person: every room is in the list (even one nobody joined), with no Other rooms,
    no room menu, no Join bar and no People list. Fails if a desktop user's sidebar loses a room."""
    page = ui.context().new_page()
    page.goto(desktop.login_url())
    expect(page.locator("#st-conn")).to_have_text("Connected")
    for room in ("#build", "#untouched"):
        expect(mine(page, room)).to_be_visible()
    expect(page.locator("#other-rooms")).to_have_count(0)
    mine(page, "#untouched").click()
    expect(page.locator("#composer")).to_be_visible()
    expect(page.locator("#preview-bar")).to_be_hidden()
    expect(page.locator("#room-menu-wrap")).to_be_hidden()
    expect(page.locator("#room-people")).to_be_hidden()
