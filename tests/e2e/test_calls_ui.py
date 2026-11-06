"""The call as a member of the room, in a real browser (#623, DESIGN.md §54, §29.9, §29.11).

A room's open call, whatever runs it (the server's own bot, or a person's machine), is a row in Members
under a Calls group, with its platform's icon, a status dot and where it runs. Opening it shows its details
in the pane the way an agent's row shows its Inspector, with **End this call** for whoever may end it. These
tests drive the page against the real broker: the rows a call leaves are planted as the other call tests
plant them, the key's pushes and the End command are real, and a person's identity is a real session on a
hosted broker (alice the owner, an admin; bob and carol ordinary).

Marker ``e2e``; the ``UI`` fixture class of tests/e2e/test_web_ui.py watches every page for console errors,
page errors and CSP violations. Nothing waits for a time to pass: every wait is Playwright's ``expect``, so
a call that goes live, a line that arrives or a frame that removes the row is waited for by what it shows.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Iterator
from typing import Any, NamedTuple

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from fakes import pictures
from playwright.sync_api import Browser, Locator, Page, expect
from test_agent_icons_ui import PIXEL, choose, open_icons
from test_private_rooms_ui import person_cookie
from test_web_ui import PHASE_REPORTS, PHONE, UI, no_horizontal_scroll

from switchboard.broker.auth import WebOrigin
from switchboard.meeting import keys
from switchboard.models import session_key

pytestmark = pytest.mark.e2e

ADMIN_PW = "correct horse battery"
PLATFORMS = ("zoom", "meet", "teams")


def key_for(name: str) -> str:
    return "sbc_" + hashlib.sha256(f"an example key for {name}".encode()).hexdigest()


# ------------------------------------------------------------------ the brokers
class Team(NamedTuple):
    """A hosted broker (a team server): alice the owner and an admin, bob and carol ordinary people."""

    broker: InProcBroker
    cookies: dict[str, str]  # by name

    @property
    def origin(self) -> str:
        return self.broker.state.web_origin.origin


@pytest.fixture(scope="module")
def team(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Team]:
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
            admin = c.cookies.get("switchboard_session")
        assert admin
        cookies = {"alice": admin}
        for person in ("bob", "carol"):
            cookies[person] = person_cookie((b, admin), person)
        try:
            yield Team(b, cookies)
        finally:
            b.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture(scope="module")
def desk(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    """A desktop broker: one person, alice, the owner and an admin."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home).start()
        try:
            yield b
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


# ------------------------------------------------------------------ what a call leaves in the broker
def make_room(b: InProcBroker, name: str) -> None:
    b.on_loop(lambda: b.state.service.create_room(name))


def host_call(
    b: InProcBroker,
    room: str,
    person: str | None,
    device: str,
    *,
    platform: str = "zoom",
    live: bool = True,
    model: str = "large-v3-turbo",
    key: str | None = None,
) -> str:
    """A call hosted from ``device`` by ``person`` (None: the owner) in ``room``: the token's row and the
    call's, as registering leaves them. Returns the key (the relay's side of it), of which the broker
    holds only the hash."""
    key = key or key_for(room)

    def run() -> None:
        store = b.state.store
        who = store.person_named(person) if person else None
        # a device name is unique among live tokens: a machine that hosts a second call is the same token
        token = store.agent_token_for_device(device) or store.agent_token_add(
            who.id if who else None, device, hashlib.sha256(f"{room} {device}".encode()).hexdigest()
        )
        assert token is not None and token.person_id == (who.id if who else None)
        r = store.get_room(room)
        assert r is not None
        row = store.insert_hosted_meeting(
            r.id,
            started_by=person or b.state.cfg.human_name,
            platform=platform,
            key_hash=keys.key_hash(key),
            token_id=token.id,
            owner_person_id=who.id if who else None,
            host=device,
            model=model,
        )
        if live:
            store.set_meeting_live(row.id)

    b.on_loop(run)
    return key


def bot_call(b: InProcBroker, room: str, platform: str = "meet", *, live: bool = True) -> None:
    """A call the broker's own bot follows in ``room``, as joining it leaves it."""

    def run() -> None:
        store = b.state.store
        r = store.get_room(room)
        assert r is not None
        row = store.insert_meeting(r.id, started_by=b.state.cfg.human_name, platform=platform)
        if live:
            store.set_meeting_live(row.id)

    b.on_loop(run)


def open_page(ui: UI, b: InProcBroker, room: str, *, cookie: str | None = None, **options: Any) -> Page:
    """A signed-in page with ``room`` open: a session cookie (a hosted broker) or a login link (a desktop
    one)."""
    ctx = ui.context(**options)
    page = ctx.new_page()
    if cookie:
        ctx.add_cookies([{"name": "switchboard_session", "value": cookie, "url": b.state.web_origin.origin}])
        page.goto(b.state.web_origin.origin + "/")
    else:
        page.goto(b.login_url())
    expect(page.locator("#st-conn")).to_have_text("Connected")
    tab = page.locator(f'#tabs .room[data-room="#{room}"]')
    if not tab.is_visible():
        page.click("#rooms-toggle")
    tab.click()
    expect(page.locator("#room-title")).to_have_text(room)
    return page


def is_phone(page: Page) -> bool:
    return (page.viewport_size or {}).get("width", 1440) <= 760


def members(page: Page) -> None:
    """Members, on a phone the sheet the header's pill opens; on a desktop the pane is there."""
    if is_phone(page) and not page.locator("#app.sheet-open").count():
        page.click("#buddy-toggle")
    expect(page.locator("#members-view")).to_be_visible()


def below(page: Page, *ids: str) -> bool:
    """The named blocks of Members are in this order, top to bottom, and every one is on screen."""
    return page.evaluate(
        """(ids) => {
          const tops = ids.map(id => document.getElementById(id).getBoundingClientRect());
          return tops.every((r, i) => r.height > 0 && (i === 0 || r.top >= tops[i - 1].top));
        }""",
        list(ids),
    )


def call_row(page: Page) -> Locator:
    return page.locator("#call-list .member")


def open_details(page: Page, *, tap: bool = False) -> None:
    """Open the call's details from its row and wait until they are whole: the broker's answer (who started
    it, the lines, whether this viewer may end it) redraws the pane, so anything read before it is read from
    a pane that is about to be replaced."""
    members(page)
    (page.tap if tap else page.click)("#call-list .m-open")
    expect(page.locator("#insp-name")).to_be_focused()
    expect(page.locator(".insp-facts .muted")).to_have_count(0)  # no fact still says "loading…"
    expect(page.locator("#members-view")).to_be_hidden()  # the pane has finished sliding to the details


def end_button(page: Page) -> Locator:
    return page.locator("#insp-end-call")


def inside_the_screen(page: Page, *selectors: str) -> None:
    """Every control is within the viewport's width and the page does not scroll sideways."""
    ok = page.evaluate(
        """(sel) => sel.every(s => {
          const r = document.querySelector(s).getBoundingClientRect();
          return r.left >= 0 && r.right <= document.documentElement.clientWidth;
        })""",
        list(selectors),
    )
    assert ok, selectors
    no_horizontal_scroll(page)


def relay_for(b: InProcBroker, key: str) -> httpx.Client:
    """What a host's relay is: a client with the call's key, nothing else."""
    return httpx.Client(base_url=b.state.web_origin.origin, headers={"authorization": f"Bearer {key}"})


# ------------------------------------------------------------------ the row and its details
def test_the_server_s_own_bot_is_a_call_in_members_and_its_details_end_it(ui: UI, desk: InProcBroker) -> None:
    """A call the broker's own bot follows is a row under Calls after People and Agents: its platform's icon,
    "Meet call", a green dot and "Live · the server", counted in Members. Opening it shows who started
    it, since when, what runs it and its voice, with none of a hosted call's key, model or lines; End
    this call (whoever may use Leave call) ends it, the bar goes, and focus lands where an agent's row's
    would. Fails if the row is missing or says something else, the details show a hosted call's fields,
    End this call is not there, or the call stays after it."""
    make_room(desk, "#bot")
    bot_call(desk, "#bot", "meet")
    page = open_page(ui, desk, "bot")
    members(page)
    expect(page.locator("#calls-title")).to_have_text("Calls (1)")
    expect(call_row(page)).to_have_count(1)
    expect(call_row(page).locator(".m-name")).to_have_text("Meet call")
    expect(call_row(page).locator(".m-status")).to_have_text("Live · the server")
    expect(call_row(page).locator(".avatar.call.h-meet svg.ic-call-meet")).to_have_count(1)
    expect(call_row(page).locator(".avatar .dot.s-idle")).to_have_count(1)
    expect(page.locator("#members-count")).to_have_text("2")  # alice and the call
    assert below(page, "buddy-me", "buddy-list", "call-list")  # You, the agents, then the calls
    expect(page.locator("#banner-meeting-mark svg.ic-call-meet")).to_have_count(
        1
    )  # the bar draws the same icon

    open_details(page)
    expect(page.locator("#insp-name")).to_have_text("Meet call")
    expect(page.locator("#insp-pos")).to_have_text("Call 1 of 1")
    expect(page.locator(".insp-status")).to_have_text(re.compile(r"^Live since \d\d:\d\d · transcribing$"))
    expect(page.locator(".insp-chips .tier")).to_have_text("server bot")
    expect(page.locator(".insp-chips .host-chip")).to_have_text("the server")
    expect(page.locator(".insp-id .avatar.call.lg svg.ic-call-meet")).to_have_count(1)
    facts = page.locator(".insp-facts")
    expect(facts).to_contain_text("Hosted on")
    expect(facts.locator("dt")).to_have_text(["Hosted on", "Started by", "Voice"])
    expect(facts.locator("dd")).to_have_text(["the server", "alice", "Off"])
    expect(page.locator(".insp-tier-how")).to_contain_text("The server's bot listens to the call")
    expect(end_button(page)).to_be_visible()
    expect(end_button(page)).to_have_text("End this call")
    expect(page.locator(".insp-actions .fine")).to_have_text(
        "The bot leaves the call. What it already posted stays in the room."
    )

    end_button(page).click()
    expect(page.locator("#members-view")).to_be_visible()  # the pane is back at Members
    expect(page.locator("#call-list")).to_be_hidden()
    expect(page.locator("#calls-title")).to_be_hidden()
    expect(page.locator("#banner-meeting")).to_be_hidden()
    expect(page.locator("#members-count")).to_have_text("1")
    expect(page.locator("#members-title")).to_be_focused()  # no agent in this room: the heading, not <body>


def test_a_hosted_call_names_its_machine_key_model_and_lines_and_can_copy_the_fingerprint(
    ui: UI, desk: InProcBroker
) -> None:
    """A call hosted on a person's machine says where it runs by the machine's label (what the call bar says),
    and its details add the key's short fingerprint, the model and the lines it has sent, as the broker holds
    them. The fingerprint's button copies exactly what is shown. Never the key or its hash: the page has only
    the fingerprint. Fails if the row or the details name another place, a line is not counted, or the copy
    is not the fingerprint."""
    make_room(desk, "#hosted")
    key = host_call(desk, "#hosted", None, "alice-laptop", platform="zoom")
    fingerprint = keys.fingerprint(keys.key_hash(key))

    def post_line() -> None:
        # the way a pushed line is posted: through the room service, which stores it with its call and tells
        # every page (a frame), so the page's count of lines can follow
        store = desk.state.store
        room, row = store.get_room("#hosted"), store.meeting_by_key(keys.key_hash(key))
        assert room is not None and row is not None
        posted = desk.state.service.post_meeting_line(
            room.id,
            sender_name="bob",
            text="Claude one, can you check the upload limit?",
            mentions=[],
            meeting_id=row.id,
            sender_host="alice-laptop",
        )
        assert posted is not None

    page = open_page(ui, desk, "hosted", permissions=["clipboard-read", "clipboard-write"])
    members(page)
    expect(call_row(page).locator(".m-name")).to_have_text("Zoom call")
    expect(call_row(page).locator(".m-status")).to_have_text("Live · alice-laptop")
    expect(call_row(page).locator(".avatar svg.ic-call-zoom")).to_have_count(1)
    open_details(page)
    expect(page.locator(".insp-chips .tier")).to_have_text("hosted bot")
    expect(page.locator(".insp-chips .host-chip")).to_have_text("alice-laptop")
    facts = page.locator(".insp-facts")
    expect(facts.locator("dt")).to_have_text(
        ["Hosted on", "Started by", "Key fingerprint", "Model", "Lines sent", "Voice"]
    )
    expect(facts.locator("dd").nth(0)).to_have_text("alice-laptop")
    expect(facts.locator("dd").nth(1)).to_have_text("alice")
    expect(facts.locator("dd").nth(2).locator("code.sess")).to_have_text(fingerprint)
    expect(facts.locator("dd").nth(3)).to_have_text("large-v3-turbo")
    expect(facts.locator("dd").nth(4)).to_have_text("0")
    expect(page.locator(".insp-tier-how")).to_contain_text("It pushes what it hears into this room")
    expect(page.locator(".insp-actions .fine")).to_have_text(
        "Only alice and an admin see this. The next push with this key is refused; lines already sent keep"
        " their label."
    )
    desk.on_loop(post_line)  # a line the call posted: the count follows without a reload
    expect(facts.locator("dd").nth(4)).to_have_text("1")

    page.click('[data-focus="copy-fingerprint"]')
    expect(page.locator('[data-focus="copy-fingerprint"]')).to_have_attribute(
        "aria-label", "Fingerprint copied"
    )
    assert page.evaluate("navigator.clipboard.readText()") == fingerprint
    text = page.locator("body").inner_text()
    assert key not in text and keys.key_hash(key) not in text  # the page was never given them
    assert "sbc_" not in page.content()


def test_joining_and_live_look_different_and_the_page_follows_the_call_going_live(
    ui: UI, desk: InProcBroker
) -> None:
    """While a call is joining its row says "Joining…" with a dashed dot, its details say "Joining…" and the
    bar says so; the host's first push makes it live (the broker's frame), and the row turns to "Live" with a
    solid green dot and the details to "Live since hh:mm · transcribing", with no reload. Fails if the two
    states read or look the same, or if the page does not follow the frame."""
    make_room(desk, "#joining")
    key = host_call(desk, "#joining", None, "alice-laptop", platform="meet", live=False)
    page = open_page(ui, desk, "joining")
    members(page)
    expect(page.locator("#banner-meeting-text")).to_contain_text("Joining a Meet call…")
    expect(call_row(page).locator(".m-status")).to_have_text("Joining… · alice-laptop")
    dot = call_row(page).locator(".avatar .dot")
    expect(dot).to_have_class(re.compile(r"s-starting"))
    assert dot.evaluate("el => getComputedStyle(el).borderStyle") == "dashed"
    open_details(page)
    expect(page.locator(".insp-status")).to_have_text("Joining…")
    expect(page.locator(".insp-id .avatar .dot.s-starting")).to_have_count(1)

    relay = relay_for(desk, key)
    try:
        assert relay.post("/mcp/meeting/push", json={"segments": []}).status_code == 200  # in the call
    finally:
        relay.close()
    expect(page.locator(".insp-status")).to_have_text(re.compile(r"^Live since \d\d:\d\d · transcribing$"))
    expect(page.locator(".insp-id .avatar .dot.s-idle")).to_have_count(1)
    page.click("#insp-back")
    expect(call_row(page).locator(".m-status")).to_have_text("Live · alice-laptop")
    dot = call_row(page).locator(".avatar .dot")
    expect(dot).to_have_class(re.compile(r"s-idle"))
    assert dot.evaluate("el => getComputedStyle(el).borderStyle") == "solid"


@pytest.mark.parametrize(
    ("platform", "name", "icon"),
    [
        ("zoom", "Zoom call", "call-zoom"),
        ("meet", "Meet call", "call-meet"),
        ("teams", "Teams call", "call-teams"),
    ]
    + [("unknown", "Call", "call")],
)
def test_each_platform_has_its_own_drawing_and_its_name_is_always_beside_it(
    ui: UI, desk: InProcBroker, platform: str, name: str, icon: str
) -> None:
    """Zoom, Meet, Teams and a hosted call that does not say its platform each have a drawing of switchboard's
    own, on the page's neutral tile in its own colours, and the platform's name is written beside it (a
    hosted call that says none is just "Call"). The drawings differ, none is an image loaded from
    anywhere, and the tile is the page's colour, not a brand's. Fails if two platforms share a drawing,
    the name is missing, or the tile carries an image."""
    room = f"plat-{platform}"
    make_room(desk, f"#{room}")
    host_call(desk, f"#{room}", None, "alice-laptop", platform=platform, key=key_for(room))
    page = open_page(ui, desk, room)
    members(page)
    expect(call_row(page).locator(".m-name")).to_have_text(name)
    avatar = call_row(page).locator(".avatar.call")
    expect(avatar.locator(f"svg.ic-{icon}")).to_have_count(1)
    look = avatar.evaluate(
        "el => { const s = getComputedStyle(el); return [s.backgroundImage, s.backgroundColor]; }"
    )
    assert look[0] == "none" and look[1] == "rgb(236, 236, 240)", look  # the neutral tile, no image
    open_details(page)
    expect(page.locator("#insp-name")).to_have_text(name)
    # the details and the bar draw the same icon as the row
    expect(page.locator(f".insp-id .avatar.call.lg svg.ic-{icon}")).to_have_count(1)
    expect(page.locator(f"#banner-meeting-mark svg.ic-{icon}")).to_have_count(1)


def test_the_drawings_are_all_different_and_none_is_an_image(ui: UI, desk: InProcBroker) -> None:
    """The four drawings (read from the page's own icon table through one row each) are four different
    paths, drawn as strokes in the tile's colour: an SVG path and nothing loaded. Fails if two platforms share
    a path, or the glyph is not drawn in the tile's own ink."""
    seen: dict[str, str] = {}
    for platform in ("zoom", "meet", "teams", "unknown"):
        room = f"look-{platform}"
        make_room(desk, f"#{room}")
        host_call(desk, f"#{room}", None, "alice-laptop", platform=platform, key=key_for(room))
        page = open_page(ui, desk, room)
        members(page)
        glyph = call_row(page).locator(".avatar.call svg.ic path")
        seen[platform] = glyph.get_attribute("d") or ""
        ink = (
            call_row(page)
            .locator(".avatar.call svg.ic")
            .evaluate("el => { const s = getComputedStyle(el); return [s.fill, s.stroke]; }")
        )
        assert ink[0] == "none" and ink[1] == "rgb(60, 60, 67)", (platform, ink)  # the tile's own ink
    assert all(seen.values()) and len(set(seen.values())) == 4, seen


# ------------------------------------------------------------------ who may end it
def test_end_this_call_is_shown_only_to_whoever_may_end_it_and_ends_it_for_everyone(
    ui: UI, team: Team
) -> None:
    """On a team server: carol's call (hosted from her laptop) can be ended by carol, whose call it is, and by
    alice, an admin, and not by bob; alice's own call, by alice, an admin (the owner) and no one else. Each
    person sees the row and the details of either call, only End this call differs. Carol ends hers from the
    page: it goes at once from alice's page too, and the next push with its key is refused. Fails if bob is
    shown the button, carol is not shown hers, or the key still works after it."""
    b = team.broker
    for room in ("#carol-call", "#alice-call"):
        make_room(b, room)
    carol_key = host_call(b, "#carol-call", "carol", "carol-laptop")
    host_call(b, "#alice-call", None, "alice-laptop", key=key_for("alice-call"))

    shown = {}
    pages = {}
    for person in ("alice", "bob", "carol"):
        for room in ("carol-call", "alice-call"):
            page = open_page(ui, b, room, cookie=team.cookies[person])
            open_details(page)
            expect(page.locator("#insp-name")).to_have_text("Zoom call")
            expect(page.locator(".insp-facts")).to_contain_text("Started by")
            started = "carol" if room == "carol-call" else "alice"
            expect(page.locator(".insp-facts dd").nth(1)).to_have_text(started)
            # the details come from the broker's answer, so wait for it before looking for the button
            expect(page.locator(".insp-facts dd").nth(4)).to_have_text("0")
            shown[(person, room)] = end_button(page).count() == 1
            pages[(person, room)] = page
            page.click("#insp-back")
            assert below(
                page, "buddy-me", "buddy-list", "room-people", "call-list"
            )  # after People and Agents
            page.click("#call-list .m-open")
            expect(page.locator("#insp-name")).to_be_focused()
    assert shown == {
        ("alice", "carol-call"): True,  # an admin
        ("alice", "alice-call"): True,  # her own
        ("bob", "carol-call"): False,
        ("bob", "alice-call"): False,
        ("carol", "carol-call"): True,  # whose call it is
        ("carol", "alice-call"): False,
    }
    # the bar's End call follows the same answer: bob, who would be refused, is not offered it
    # (the server's own refusal of /meeting leave is in tests/integration/test_meeting_hosted.py)
    for (person, room), may in shown.items():
        bar_end = pages[(person, room)].locator("#meeting-leave")
        if may:
            expect(bar_end).to_be_visible()
        else:
            expect(bar_end).to_be_hidden()

    carol_page, alice_page = pages[("carol", "carol-call")], pages[("alice", "carol-call")]
    relay = relay_for(b, carol_key)
    try:
        assert relay.post("/mcp/meeting/push", json={"segments": []}).status_code == 200
        end_button(carol_page).click()
        expect(carol_page.locator("#call-list")).to_be_hidden()
        # alice's page followed the broker's frame: its details closed on their own, and the row went
        expect(alice_page.locator("#pane")).not_to_have_class(re.compile(r"inspecting"))
        expect(alice_page.locator("#members-view")).to_be_visible()
        expect(alice_page.locator("#call-list")).to_be_hidden()
        refused = relay.post("/mcp/meeting/push", json={"segments": []})
        assert (refused.status_code, refused.json()["error"]) == (410, "ended")
    finally:
        relay.close()
    # alice's other call is as it was
    expect(call_row(pages[("alice", "alice-call")])).to_have_count(1)


def test_an_admin_the_owner_made_may_end_a_call_that_is_not_theirs(ui: UI, team: Team) -> None:
    """Admin is a flag the owner gives a person (§51): once bob is one, he is shown End this call on carol's
    call and ends it; before, he was not. Fails if the page keeps the answer it had, or the server's rule is
    not the one the button follows."""
    b = team.broker
    make_room(b, "#admin-call")
    key = host_call(b, "#admin-call", "carol", "carol-laptop-2", key=key_for("admin-call"))
    bob = open_page(ui, b, "admin-call", cookie=team.cookies["bob"])
    open_details(bob)
    expect(bob.locator(".insp-facts dd").nth(4)).to_have_text("0")
    assert end_button(bob).count() == 0
    bob_id = b.state.store.person_named("bob").id

    def make_admin(on: bool) -> None:
        with b.state.store.con:
            b.state.store.con.execute("UPDATE people SET admin=? WHERE id=?", (int(on), bob_id))

    b.on_loop(make_admin, True)
    try:
        page = open_page(ui, b, "admin-call", cookie=team.cookies["bob"])  # a fresh load asks again
        open_details(page)
        expect(end_button(page)).to_be_visible()
        end_button(page).click()
        expect(page.locator("#call-list")).to_be_hidden()
        relay = relay_for(b, key)
        try:
            assert relay.post("/mcp/meeting/push", json={"segments": []}).status_code == 410
        finally:
            relay.close()
    finally:
        b.on_loop(make_admin, False)


# ------------------------------------------------------------------ keyboard and focus
def test_the_details_open_and_close_by_keyboard_and_focus_goes_where_the_inspector_s_does(
    ui: UI, desk: InProcBroker
) -> None:
    """The same keys and focus as an agent's Inspector. Enter on the call's row opens its details and focus
    goes to its name; Tab reaches the copy button, and a frame that changes the call (voice turned on) redraws
    the details with focus kept on that button; Esc, and Back, close them and focus returns to the call's row;
    End this call by keyboard ends the call and focus goes to the heading, not to <body>. Fails if focus is
    lost on a redraw, Esc leaves the details open, or focus drops to the page."""
    make_room(desk, "#keys")
    key = host_call(desk, "#keys", None, "alice-laptop", key=key_for("keys"))
    page = open_page(ui, desk, "keys")
    members(page)
    row_button = page.locator("#call-list .m-open")
    row_button.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#insp-name")).to_be_focused()
    expect(page.locator("#pane")).to_have_class(re.compile(r"inspecting"))
    expect(page.locator(".insp-facts dd").nth(4)).to_have_text("0")  # the details have come
    expect(call_row(page)).to_have_attribute("aria-current", "true")

    page.keyboard.press("Tab")
    copy = page.locator('[data-focus="copy-fingerprint"]')
    expect(copy).to_be_focused()
    relay = relay_for(
        desk, key
    )  # the host is in the call: its first push, which gives the broker the call's voice
    web = desk.web_client()
    try:
        assert relay.post("/mcp/meeting/push", json={"segments": []}).status_code == 200
        said = web.post(
            "/api/rooms/keys/command", json={"text": "/meeting voice on"}, headers=desk.write_headers()
        )
        assert said.status_code == 200, said.text
    finally:
        web.close()
        relay.close()
    expect(page.locator(".insp-facts dd").nth(5)).to_have_text("On")  # the frame redrew the details
    expect(page.locator('[data-focus="copy-fingerprint"]')).to_be_focused()  # and focus stayed where it was

    page.keyboard.press("Escape")
    expect(page.locator("#pane")).not_to_have_class(re.compile(r"inspecting"))
    expect(page.locator("#call-list .m-open")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#insp-name")).to_be_focused()
    page.click("#insp-back")
    expect(page.locator("#call-list .m-open")).to_be_focused()

    page.keyboard.press("Enter")
    expect(page.locator("#insp-name")).to_be_focused()
    expect(end_button(page)).to_be_visible()
    page.keyboard.press("Tab")  # the copy button
    page.keyboard.press("Tab")  # End this call
    expect(end_button(page)).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#call-list")).to_be_hidden()
    expect(page.locator("#members-title")).to_be_focused()


def test_with_agents_in_the_room_focus_goes_to_the_agent_above_when_the_call_ends(ui: UI, team: Team) -> None:
    """The Calls group is last, so when its call ends with focus in the details, focus goes to the last
    agent's row, as it does for an agent's row that went away (the previous one when it was last). Fails if
    it drops to <body> or to the wrong row."""
    b = team.broker
    make_room(b, "#focus")
    host_call(b, "#focus", "carol", "carol-laptop-3", key=key_for("focus"))

    def agents() -> None:
        store = b.state.store
        room = store.get_room("#focus")
        assert room is not None
        for n, name in enumerate(("claude-1", "codex-1")):
            p = store.upsert_participant(
                "claude", session_key("claude", "carol-laptop-3", f"s{n}"), host="carol-laptop-3"
            )
            store.create_membership(room.id, p.id, name)
        b.state.hub.members_changed("#focus")

    b.on_loop(agents)
    page = open_page(ui, b, "focus", cookie=team.cookies["carol"])
    members(page)
    expect(page.locator("#buddy-list .member")).to_have_count(2)
    open_details(page)
    expect(page.locator(".insp-facts dd").nth(4)).to_have_text("0")
    end_button(page).click()
    expect(page.locator("#call-list")).to_be_hidden()
    expect(page.locator('#buddy-list .member[data-name="codex-1"] .m-open')).to_be_focused()


# ------------------------------------------------------------------ light, dark and a phone
def test_a_call_in_light_dark_and_on_a_phone_fits_and_shows_its_controls(ui: UI, desk: InProcBroker) -> None:
    """The row and the details in light and dark (the tile and the text change with the scheme), and on a
    phone Members is the sheet, the row opens the details in it, every control is inside the screen and
    nothing scrolls sideways, and Back and Esc work in the sheet. Fails if a colour does not follow the
    scheme, a control is cut off, or the phone scrolls sideways."""
    make_room(desk, "#looks")
    host_call(desk, "#looks", None, "alice-laptop-with-24-chr", key=key_for("looks"))  # the longest label
    page = open_page(ui, desk, "looks")
    members(page)
    tile = call_row(page).locator(".avatar.call")
    light = tile.evaluate("el => getComputedStyle(el).backgroundColor")
    page.emulate_media(color_scheme="dark")
    expect(tile).to_be_visible()
    dark = tile.evaluate("el => getComputedStyle(el).backgroundColor")
    assert light != dark, (light, dark)
    open_details(page)
    expect(page.locator(".insp-facts dd").nth(4)).to_have_text("0")
    expect(end_button(page)).to_be_visible()
    note = page.locator(".insp-actions .fine").evaluate("el => getComputedStyle(el).color")
    page.emulate_media(color_scheme="light")
    assert page.locator(".insp-actions .fine").evaluate("el => getComputedStyle(el).color") != note
    page.click("#insp-back")

    phone = open_page(ui, desk, "looks", **PHONE)
    members(phone)
    expect(phone.locator("#app")).to_have_class(re.compile(r"sheet-open"))
    expect(call_row(phone).locator(".m-name")).to_be_visible()
    inside_the_screen(phone, "#call-list .member", "#call-list .m-chev")
    open_details(phone, tap=True)
    inside_the_screen(
        phone,
        "#insp-body .insp-id",
        ".insp-chips .host-chip",
        ".insp-facts",
        '[data-focus="copy-fingerprint"]',
        "#insp-end-call",
    )
    phone.evaluate("() => document.getElementById('insp-end-call').scrollIntoView()")
    inside_the_screen(phone, "#insp-end-call", ".insp-actions .fine")
    # as for an agent's Inspector: Back returns to Members in the sheet with focus on the row, and Esc closes
    # the sheet (the topmost thing on a phone) with focus back on the pill that opened it
    phone.click("#insp-back")
    expect(phone.locator("#members-view")).to_be_visible()
    expect(phone.locator("#call-list .m-open")).to_be_focused()
    expect(phone.locator("#app")).to_have_class(re.compile(r"sheet-open"))
    phone.keyboard.press("Escape")
    expect(phone.locator("#app")).not_to_have_class(re.compile(r"sheet-open"))
    expect(phone.locator("#buddy-toggle")).to_be_focused()
    # the bar the call also draws still fits, with the icon in it
    inside_the_screen(phone, "#meeting-voice", "#meeting-snapshot", "#meeting-leave", "#banner-meeting-mark")


# ------------------------------------------------------------------ uploads: the platforms' icons
def tile_shows(avatar: Locator, platform: str, scheme: str) -> None:
    """The avatar draws the platform's uploaded file for ``scheme``, from the broker's own URL."""
    got = avatar.evaluate(PIXEL)
    assert got is not None, (platform, "no image on the avatar")
    assert got["url"] == f"/api/agent-icons/{platform}/{scheme}", got
    want = {"light": (200, 30, 30), "dark": (30, 90, 200)}[scheme]
    assert got["rgb"] == list(want), got


def png_for(scheme: str) -> bytes:
    rgb = {"light": (200, 30, 30), "dark": (30, 90, 200)}[scheme]
    return pictures.png(64, 64, (*rgb, 255))


def test_an_admin_replaces_a_platform_s_icon_and_removes_it_again(ui: UI, desk: InProcBroker) -> None:
    """Settings → Appearance lists the three platforms a call can be on under Call icons, beside the agents'
    avatars, with the same Upload, Replace and Reset. After a Zoom upload the Calls row, the call bar and the
    details show that file (the dark one in dark), Meet's and Teams' drawings are untouched, and Reset brings
    the drawing back everywhere. A file that is not an image is refused in words and nothing changes. Fails if
    a place still draws the glyph over the image, the other platforms change, or Reset leaves the picture."""
    make_room(desk, "#upload")
    host_call(desk, "#upload", None, "alice-laptop", platform="zoom", key=key_for("upload"))
    page = open_page(ui, desk, "upload")
    members(page)
    tile = call_row(page).locator(".avatar.call")
    expect(tile.locator("svg.ic-call-zoom")).to_be_visible()

    open_icons(page)
    expect(page.locator("#agent-icons-calls-title")).to_have_text("Call icons")
    expect(page.locator('.agent-icon-row[data-harness="zoom"] strong')).to_have_text("Zoom")
    expect(page.locator('.agent-icon-row[data-harness="meet"] strong')).to_have_text("Meet")
    expect(page.locator('.agent-icon-row[data-harness="teams"] strong')).to_have_text("Teams")
    expect(page.locator('.agent-icon-row[data-harness="zoom"]')).to_contain_text("The built-in icon")
    expect(page.locator("#agent-icon-reset-zoom")).to_be_disabled()

    refused = page.locator("#agent-icons-error")
    choose(page, "zoom", "light", pictures.SVG, "image/svg+xml", "logo.svg")
    expect(refused).to_contain_text("SVG")
    expect(page.locator("#agent-icon-reset-zoom")).to_be_disabled()  # nothing was stored
    # the browser logs the refused request as a console error: the one the server answered, and nothing else
    assert len(ui.problems) == 1 and re.search(r"status of 415 .*/api/agent-icons/zoom/light", ui.problems[0])
    ui.problems.clear()

    choose(page, "zoom", "light", png_for("light"), "image/png", "zoom-light.png")
    expect(page.locator("#agent-icons-status")).to_have_text("Set the light image for Zoom.")
    choose(page, "zoom", "dark", png_for("dark"), "image/png", "zoom-dark.png")
    expect(page.locator("#agent-icons-status")).to_have_text("Set the dark image for Zoom.")
    row = page.locator('.agent-icon-row[data-harness="zoom"]')
    expect(row).to_contain_text("Your light and dark images")
    tile_shows(row.locator(".avatar"), "zoom", "light")  # the card's own avatar shows it
    page.click("#settings-close")

    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        members(page)
        tile_shows(call_row(page).locator(".avatar.call"), "zoom", scheme)
        expect(call_row(page).locator(".avatar.call svg.ic")).to_be_hidden()  # the glyph is under the picture
        tile_shows(page.locator("#banner-meeting-mark .avatar"), "zoom", scheme)
        open_details(page)
        tile_shows(page.locator(".insp-id .avatar.call.lg"), "zoom", scheme)
        page.click("#insp-back")
    page.emulate_media(color_scheme="light")

    # the other platforms keep their drawings
    make_room(desk, "#other")
    host_call(desk, "#other", None, "bobs-laptop", platform="meet", key=key_for("other"))
    page.locator('#tabs .room[data-room="#other"]').click()
    expect(page.locator("#room-title")).to_have_text("other")
    expect(call_row(page).locator(".avatar svg.ic-call-meet")).to_be_visible()
    page.locator('#tabs .room[data-room="#upload"]').click()

    open_icons(page)
    page.click("#agent-icon-reset-zoom")
    expect(page.locator("#agent-icons-status")).to_have_text("Zoom is back to its built-in icon.")
    expect(row).to_contain_text("The built-in icon")
    page.click("#settings-close")
    members(page)
    expect(call_row(page).locator(".avatar.call svg.ic-call-zoom")).to_be_visible()
    got = call_row(page).locator(".avatar.call").evaluate(PIXEL)
    assert got is None
    expect(page.locator("#banner-meeting-mark svg.ic-call-zoom")).to_be_visible()


def test_a_person_who_is_not_an_admin_sees_the_icons_and_has_no_way_to_change_them(
    ui: UI, team: Team
) -> None:
    """On a team server the admin's Zoom icon is what everyone's call shows, and Settings holds no icon
    section for bob (the routes refuse him too, in the integration tests). Fails if bob is offered an
    upload."""
    b = team.broker
    make_room(b, "#shared")
    host_call(b, "#shared", None, "alice-laptop", platform="zoom", key=key_for("shared"))
    admin = open_page(ui, b, "shared", cookie=team.cookies["alice"])
    open_icons(admin)
    choose(admin, "zoom", "light", png_for("light"), "image/png", "zoom-light.png")
    expect(admin.locator("#agent-icons-status")).to_have_text("Set the light image for Zoom.")
    bob = open_page(ui, b, "shared", cookie=team.cookies["bob"])
    members(bob)
    tile_shows(call_row(bob).locator(".avatar.call"), "zoom", "light")
    bob.click("#me-settings")
    expect(bob.locator("#app-dialog")).to_be_visible()
    bob.click("#settings-tab-appearance")
    expect(bob.locator("#settings-agent-icons")).to_have_count(0)
    origin = b.state.web_origin.origin
    h = {"Origin": origin, "X-Switchboard": "1"}
    with httpx.Client(
        base_url=origin, headers=h, cookies={"switchboard_session": team.cookies["alice"]}
    ) as c:
        assert c.delete("/api/agent-icons/zoom").status_code == 200  # as it was, for the tests that follow
