"""Customizing the agents' avatars in a real browser (#498, DESIGN.md §29.10): Settings → Appearance →
Agent icons, for the admin only. The admin uploads a light and a dark image for a harness, sees each
one, and every avatar of that harness (Members, the log, a reply's preview, the Inspector, the @ list,
the welcome page's cards) shows it, in light and dark, on a desktop and on a phone, for the person who
uploaded it and for every other page, live. Reset brings the built-in avatar back. A file that is an
SVG, mislabelled, too big or too wide is refused with a message, and nothing changes.

The pictures are generated solid colours (``tests/fakes/pictures.py``), so a pixel read from the
avatar's own background says which file is on screen. Marker ``e2e``; the ``UI`` fixture class of
tests/e2e/test_web_ui.py watches every page for console errors, page errors and CSP violations, so a
CSP that blocked the images or the stylesheet fails these tests."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from typing import Any, NamedTuple

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from fakes import pictures
from playwright.sync_api import Browser, Locator, Page, expect
from test_private_rooms_ui import person_cookie
from test_web_ui import PHASE_REPORTS, PHONE, UI, no_horizontal_scroll, open_room, open_settings
from ui_world import UIWorld, wait_js

from switchboard import db
from switchboard.broker.auth import WebOrigin

pytestmark = pytest.mark.e2e

ADMIN_PW = "correct horse battery"
HEADING = "Customize your coding harnesses' avatars!"
TIP = (
    "Some companies don't allow their logos to be used, so switchboard can't ship them. "
    "You're welcome to upload any replacement you like."
)
# a solid colour per harness and scheme, so the middle pixel of the image on an avatar names its file
COLORS = {
    "claude": {"light": (200, 30, 30), "dark": (240, 120, 20)},
    "codex": {"light": (30, 160, 60), "dark": (20, 200, 200)},
    "cursor": {"light": (120, 40, 200), "dark": (230, 40, 160)},
    "devin": {"light": (230, 200, 20), "dark": (20, 30, 120)},
}
NAMES = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "devin": "Devin"}
BUILT_IN = {"codex": "/static/agents/codex-light.png", "devin": "/static/agents/devin-light.png"}

# The image an element draws as its background: its path, its width and its middle pixel. A decode
# the CSP blocks, or a file the browser can't read, throws and fails the test.
PIXEL = """async (el) => {
  const m = getComputedStyle(el).backgroundImage.match(/^url\\("(.*)"\\)$/);
  if (!m) return null;
  const im = new Image(); im.src = m[1]; await im.decode();
  const c = document.createElement('canvas'); c.width = im.naturalWidth; c.height = im.naturalHeight;
  const x = c.getContext('2d'); x.drawImage(im, 0, 0);
  const d = x.getImageData(Math.floor(im.naturalWidth / 2), Math.floor(im.naturalHeight / 2), 1, 1).data;
  return {url: new URL(m[1]).pathname, rgb: [d[0], d[1], d[2]], width: im.naturalWidth};
}"""


def picture(harness: str, scheme: str) -> tuple[bytes, str]:
    """The harness's solid-colour file for the scheme: a PNG, or (Cursor's) a WebP."""
    r, g, b = COLORS[harness][scheme]
    if harness == "cursor":
        return pictures.webp(48, 48, (r, g, b, 255)), "image/webp"
    return pictures.png(64, 64, (r, g, b, 255)), "image/png"


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def world(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[UIWorld]:
    """The seeded desktop broker (the same world test_web_ui.py uses), once for this module."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        w = UIWorld().start()
        try:
            yield w
        finally:
            w.stop()


@pytest.fixture
def ui(browser: Browser, world: UIWorld, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, world)
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)
    reset_all(world)  # the next test starts from the built-in avatars


def put_icon(
    world: UIWorld, harness: str, scheme: str, data: bytes | None = None, mime: str | None = None
) -> None:
    if data is None:
        data, mime = picture(harness, scheme)
    assert world.web is not None
    h = {"Origin": world.broker.origin, "X-Switchboard": "1", "Content-Type": mime or "image/png"}
    r = world.web.put(f"/api/agent-icons/{harness}/{scheme}", content=data, headers=h)
    assert r.status_code == 200, r.text


def reset_all(world: UIWorld) -> None:
    assert world.web is not None
    for harness in COLORS:
        world.web.delete(f"/api/agent-icons/{harness}", headers=world.broker.write_headers())


def add_cursor_agent(world: UIWorld, room: str) -> None:
    """A room with one Cursor agent that has said something (no test agent is a Cursor one: the stand-in
    is rewritten, as ui_world does for the others), for the places a Cursor avatar shows."""
    world.create_room(room)
    world.add_agents(f"#{room}", ("cursor-1",))
    world.agent_say(f"#{room}", "cursor-1", "Cursor stand-in reporting.")
    b = world.broker

    def rewrite() -> None:
        s = b.state.store
        member = s.find_member(s.get_room(f"#{room}").id, "cursor-1")
        with db.tx(s.con):
            s.con.execute("UPDATE participants SET harness='cursor' WHERE id=?", (member.participant_id,))
            s.con.execute(
                "UPDATE messages SET sender_harness='cursor' WHERE sender_membership_id=?",
                (member.membership_id,),
            )
        b.state.hub.members_changed(f"#{room}")

    b.on_loop(rewrite)


# ------------------------------------------------------------------ checking what is on screen
def shows(avatar: Locator, harness: str, scheme: str, *, where: str = "") -> None:
    """The avatar draws the harness's uploaded file for ``scheme``, from the broker's own URL."""
    got = avatar.evaluate(PIXEL)
    assert got is not None, (where, "no image on the avatar")
    assert got["url"] == f"/api/agent-icons/{harness}/{scheme}", (where, got)
    assert got["rgb"] == list(COLORS[harness][scheme]), (where, got)


def built_in(avatar: Locator, harness: str) -> None:
    got = avatar.evaluate(PIXEL)
    assert got is None or got["url"] == BUILT_IN[harness], got


def member(page: Page, name: str) -> Locator:
    return page.locator(f'#buddy-list .member[data-name="{name}"] .avatar')


def inspect(page: Page, name: str) -> Locator:
    with page.expect_response(lambda r: f"/members/{name}" in r.url and r.request.method == "GET"):
        page.click(f'#buddy-list .member[data-name="{name}"]')
    expect(page.locator("#insp-name")).to_contain_text(name)
    return page.locator("#insp-body .insp-id > .avatar")


def every_place(page: Page, scheme: str, *, phone: bool = False) -> None:
    """Every place a #build agent's avatar shows has its harness's uploaded image for ``scheme``: the
    Members list, a log line, a reply's preview, the @ list and the Inspector. Claude Code's tile
    has no glyph or word over the image."""
    if phone:
        page.click("#buddy-toggle")
    for name, harness in (
        ("claude-1", "claude"),
        ("bench", "claude"),
        ("codex-1", "codex"),
        ("devin-1", "devin"),
    ):
        shows(member(page, name), harness, scheme, where=f"members {name}")
    claude_member = member(page, "claude-1")
    expect(claude_member.locator("svg.ic")).to_be_hidden()  # the drawn bubble is gone
    if phone:
        page.click("#buddy-toggle")  # the sheet closes, so the log is what is on screen
    for name, harness in (("claude-1", "claude"), ("codex-1", "codex"), ("devin-1", "devin")):
        line = page.locator(f'#log .line[data-from="{name}"] > .avatar').first
        line.scroll_into_view_if_needed()
        shows(line, harness, scheme, where=f"log {name}")
    reply = page.locator('#log .line.k-chat[data-from="codex-1"] .reply-to .avatar.mini').first
    reply.scroll_into_view_if_needed()
    shows(reply, "claude", scheme, where="reply preview")  # codex-1's review answers claude-1's code
    box = page.locator("#input")
    box.focus()
    box.fill("")
    page.keyboard.type("@")
    expect(page.locator("#mentions")).to_be_visible()
    for name, harness in (("claude-1", "claude"), ("codex-1", "codex"), ("devin-1", "devin")):
        shows(page.locator(f"#men-{name} .avatar"), harness, scheme, where=f"@ list {name}")
    page.keyboard.press("Escape")
    box.fill("")
    if phone:
        page.click("#buddy-toggle")
    for name, harness in (("claude-1", "claude"), ("codex-1", "codex"), ("devin-1", "devin")):
        avatar = inspect(page, name)
        shows(avatar, harness, scheme, where=f"inspector {name}")
        expect(avatar.locator(".av-word")).to_have_count(0 if harness != "claude" else 1)
        if harness == "claude":
            expect(avatar.locator(".av-word")).to_be_hidden()  # no "Claude" written over the image
        page.click("#insp-back")


def settings_icons(page: Page) -> Locator:
    return page.locator("#settings-agent-icons")


def open_icons(page: Page) -> None:
    """Settings → Appearance, with the four harnesses' rows loaded."""
    if (page.viewport_size or {}).get("width", 1440) <= 760:
        page.click("#rooms-toggle")  # a phone's sidebar is a drawer, and Settings is at its foot
    open_settings(page, "appearance")
    expect(settings_icons(page)).to_be_visible()
    expect(page.locator(".agent-icon-row")).to_have_count(4)


def choose(page: Page, harness: str, scheme: str, data: bytes, mime: str, name: str = "avatar.png") -> None:
    """What a person does: press Upload, and pick a file in the file chooser it opens."""
    with page.expect_file_chooser() as chooser:
        page.click(f"#agent-icon-upload-{harness}-{scheme}")
    chooser.value.set_files({"name": name, "mimeType": mime, "buffer": data})


# ------------------------------------------------------------------ the heading and its tip
def test_the_heading_and_its_tip_read_as_the_maintainer_wrote_them_and_work_by_keyboard(ui: UI) -> None:
    """The section's visible heading is upbeat, and a small info button beside it explains why, without
    naming any company (its text is exactly the maintainer's): it is reached by Tab, shows its text on
    focus, is the button's accessible description (a screen reader reads it), closes on Esc without
    closing Settings, and shows on hover too. Fails if the tip is only a title attribute, unreachable by
    keyboard, or another text."""
    page = ui.open()
    open_icons(page)
    expect(page.locator("#agent-icons-title")).to_have_text(HEADING)
    expect(settings_icons(page)).to_have_attribute("aria-labelledby", "agent-icons-title")
    info = page.locator("#agent-icons-info")
    tip = page.locator("#agent-icons-tip")
    expect(tip).to_be_hidden()
    page.focus("#settings-text-larger")
    page.keyboard.press("Tab")
    expect(info).to_be_focused()
    expect(tip).to_be_visible()
    expect(tip).to_have_text(TIP)
    expect(info).to_have_accessible_description(TIP)
    expect(info).to_have_accessible_name(re.compile(r"ship|avatar", re.I))
    assert info.evaluate("e => e.tagName") == "BUTTON"
    page.keyboard.press("Escape")
    expect(tip).to_be_hidden()
    expect(page.locator("#app-dialog")).to_be_visible()  # the first Esc closed the tip only
    page.keyboard.press("Tab")
    page.keyboard.press("Shift+Tab")
    expect(tip).to_be_visible()  # back on the button: shown again
    page.mouse.move(0, 0)
    page.keyboard.press("Escape")
    expect(tip).to_be_hidden()
    info.hover()
    expect(tip).to_be_visible()
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    expect(page.locator("#app-dialog")).to_be_hidden()


def test_the_info_button_works_by_a_tap_on_a_phone(ui: UI) -> None:
    page = ui.open(**PHONE)
    open_icons(page)
    no_horizontal_scroll(page)
    tip = page.locator("#agent-icons-tip")
    expect(tip).to_be_hidden()
    page.tap("#agent-icons-info")
    expect(tip).to_be_visible()
    box = tip.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390, box  # it stays on the screen
    page.tap("#agent-icons-info")
    expect(tip).to_be_hidden()


# ------------------------------------------------------------------ the admin's flow
def test_the_admin_uploads_light_and_dark_images_sees_them_and_resets(ui: UI) -> None:
    """The whole round on a desktop: pressing Upload opens the file chooser, each file shows in its slot
    on the surface it is for, the row's own avatar and the state line follow, one image is used for both
    schemes until there are two, every avatar of that harness shows the right file in light and dark,
    a reload keeps it from the first paint, and Reset brings the built-in avatar back. Fails if a slot
    shows the wrong file, an avatar in Members, the log, a reply, the @ list or the Inspector isn't
    updated without a reload, the dark file is used in light mode, or Reset leaves the image."""
    page = ui.open(color_scheme="light")
    open_icons(page)
    row = page.locator('.agent-icon-row[data-harness="codex"]')
    expect(row).to_contain_text("The built-in avatar")
    expect(page.locator("#agent-icon-reset-codex")).to_be_disabled()
    built_in(member(page, "codex-1"), "codex")

    data, mime = picture("codex", "light")
    choose(page, "codex", "light", data, mime, "codex-light.png")
    expect(page.locator("#agent-icons-status")).to_have_text("Set the light image for Codex.")
    expect(page.locator("#agent-icon-upload-codex-light")).to_have_text("Replace")
    expect(page.locator("#agent-icon-upload-codex-light")).to_be_focused()  # focus is where the person was
    expect(row).to_contain_text("Your light image, used for dark too")
    expect(row.locator(".agent-icon-slot.light")).to_have_class(re.compile(r"slot-codex-light"))
    shows(row.locator(".agent-icon-slot.light"), "codex", "light", where="light slot")
    expect(row.locator(".agent-icon-slot.dark")).to_have_class(re.compile(r"empty"))
    shows(row.locator(".avatar"), "codex", "light", where="the row's avatar")
    shows(member(page, "codex-1"), "codex", "light", where="members, at once")  # no reload
    page.emulate_media(color_scheme="dark")  # one image serves both schemes
    shows(member(page, "codex-1"), "codex", "light", where="members in dark, one image")
    page.emulate_media(color_scheme="light")

    data, mime = picture("codex", "dark")
    choose(page, "codex", "dark", data, mime, "codex-dark.png")
    expect(page.locator("#agent-icons-status")).to_have_text("Set the dark image for Codex.")
    expect(row).to_contain_text("Your light and dark images")
    shows(row.locator(".agent-icon-slot.dark"), "codex", "dark", where="dark slot")
    expect(page.locator("#agent-icon-reset-codex")).to_be_enabled()
    page.emulate_media(color_scheme="dark")
    shows(member(page, "codex-1"), "codex", "dark", where="members in dark")
    shows(row.locator(".avatar"), "codex", "dark", where="the row's avatar in dark")
    page.emulate_media(color_scheme="light")
    # the other harnesses are as they were
    built_in(member(page, "devin-1"), "devin")
    expect(member(page, "claude-1").locator("svg.ic-bubble")).to_be_visible()

    page.click("#settings-close")
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        for place, avatar in (
            ("members", member(page, "codex-1")),
            ("log", page.locator('#log .line[data-from="codex-1"] > .avatar').first),
        ):
            shows(avatar, "codex", scheme, where=f"{place} {scheme}")
        shows(inspect(page, "codex-1"), "codex", scheme, where=f"inspector {scheme}")
        page.click("#insp-back")
    page.emulate_media(color_scheme="light")
    # the saved Theme beats the system's scheme, as for the built-in icons (#443)
    for theme, scheme, want in (
        ("dark", "light", "dark"),
        ("light", "dark", "light"),
        ("system", "dark", "dark"),
    ):
        page.emulate_media(color_scheme=scheme)
        page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)
        shows(member(page, "codex-1"), "codex", want, where=f"theme {theme} on a {scheme} system")
    page.evaluate("() => { document.documentElement.dataset.theme = 'system'; }")
    page.emulate_media(color_scheme="light")

    page.reload()
    expect(page.locator("#st-conn")).to_have_text("Connected")
    welcome = page.locator(".harness-head .avatar.h-codex")  # in the page's own HTML: there at first paint
    shows(welcome, "codex", "light", where="the welcome card after a reload")

    open_icons(page)
    page.click("#agent-icon-reset-codex")
    expect(page.locator("#agent-icons-status")).to_have_text("Codex is back to its built-in avatar.")
    expect(row).to_contain_text("The built-in avatar")
    expect(page.locator("#agent-icon-upload-codex-light")).to_be_focused()
    expect(row.locator(".agent-icon-slot.empty")).to_have_count(2)
    built_in(member(page, "codex-1"), "codex")
    built_in(row.locator(".avatar"), "codex")
    page.click("#settings-close")
    built_in(page.locator(".harness-head .avatar.h-codex"), "codex")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_harness_s_uploaded_icon_shows_everywhere_its_avatar_does(ui: UI, scheme: str) -> None:
    """All four harnesses, one of them a WebP: in Members, the log, a reply's preview, the @ list and the
    Inspector (and on the welcome page's cards), each agent's avatar is its harness's uploaded image for
    the scheme, and Claude Code's drawn bubble and the word under it are gone, for both of its agents.
    Fails if any place still draws the built-in avatar or the monogram."""
    room = f"icons-{scheme}"
    add_cursor_agent(ui.world, room)
    for harness in COLORS:
        for s in ("light", "dark"):
            put_icon(ui.world, harness, s)
    page = ui.open(color_scheme=scheme)
    every_place(page, scheme)
    for harness in COLORS:
        shows(page.locator(f".harness-head .avatar.h-{harness}"), harness, scheme, where=f"welcome {harness}")
    open_room(page, room)
    expect(page.locator("#buddy-list .member")).to_have_count(1)
    shows(member(page, "cursor-1"), "cursor", scheme, where="members cursor-1")
    shows(
        page.locator('#log .line[data-from="cursor-1"] > .avatar').first,
        "cursor",
        scheme,
        where="log cursor-1",
    )
    box = page.locator("#input")
    box.focus()
    page.keyboard.type("@")
    shows(page.locator("#men-cursor-1 .avatar"), "cursor", scheme, where="@ list cursor-1")
    page.keyboard.press("Escape")
    box.fill("")
    shows(inspect(page, "cursor-1"), "cursor", scheme, where="inspector cursor-1")
    got = page.locator("#insp-body .insp-id > .avatar").evaluate(PIXEL)
    assert got["width"] == 48  # the WebP's own size, decoded


def test_a_phone_shows_them_too(ui: UI) -> None:
    """The phone's Members sheet, its 28 px log avatars and the Inspector, with the images set by the
    admin from the phone's own Settings page (one slot, through the chooser) and the rest by the API.
    Fails if the phone page lacks the stylesheet, the sheet overflows, or the upload control isn't there."""
    put_icon(ui.world, "claude", "light")
    put_icon(ui.world, "claude", "dark")
    put_icon(ui.world, "devin", "dark")
    page = ui.open(**PHONE)
    open_icons(page)
    no_horizontal_scroll(page)
    row = page.locator('.agent-icon-row[data-harness="devin"]')
    expect(row).to_contain_text("Your dark image, used for light too")
    data, mime = picture("devin", "light")
    choose(page, "devin", "light", data, mime)
    expect(page.locator("#agent-icons-status")).to_have_text("Set the light image for Devin.")
    no_horizontal_scroll(page)
    page.click("#settings-close")
    log_avatar = page.locator('#log .line[data-from="devin-1"] > .avatar').first
    log_avatar.scroll_into_view_if_needed()
    shows(log_avatar, "devin", "light", where="phone log")
    box = log_avatar.bounding_box()
    assert box and round(box["width"]) == 28 == round(box["height"]), box
    page.click("#buddy-toggle")
    shows(member(page, "devin-1"), "devin", "light", where="phone members")
    shows(member(page, "claude-1"), "claude", "light", where="phone members claude")
    avatar = inspect(page, "claude-1")
    shows(avatar, "claude", "light", where="phone inspector")
    expect(avatar.locator(".av-word")).to_be_hidden()
    no_horizontal_scroll(page)


# ------------------------------------------------------------------ refusals
def test_a_file_the_server_refuses_says_why_and_changes_nothing(ui: UI) -> None:
    """An SVG, a PNG labelled as a WebP, a picture over 512 pixels and one over 256 KB each give their own
    message in the section (a role=alert line), the avatar stays what it was, and the next good file
    still works. Fails if a refusal is silent, shows another file's message, or replaces the image."""
    put_icon(ui.world, "codex", "light")
    page = ui.open(color_scheme="light")
    open_icons(page)
    error = page.locator("#agent-icons-error")
    big = pictures.png(64, 64, pad=300_000)  # refused by the page itself: it never reaches the server
    cases = (
        (pictures.SVG, "image/svg+xml", "logo.svg", "SVG isn't accepted, because it can carry script"),
        (
            pictures.png(32, 32),
            "image/webp",
            "logo.webp",
            "That file is a PNG image but was sent as image/webp",
        ),
        (
            pictures.png_with_size(600, 100),
            "image/png",
            "wide.png",
            "That image is 600 × 100 pixels: the most is 512 × 512",
        ),
        (
            pictures.png(64, 64, pad=300_000),
            "image/png",
            "big.png",
            f"That file is {-(-len(big) // 1024)} KB: the most is 256 KB",
        ),
        (b"just some text", "image/png", "notes.png", "That file isn't a PNG or a WebP image"),
    )
    for data, mime, name, message in cases:
        choose(page, "codex", "dark", data, mime, name)
        expect(error).to_contain_text(message)
        expect(page.locator("#agent-icons-status")).to_have_text("")
        expect(page.locator('.agent-icon-row[data-harness="codex"]')).to_contain_text(
            "Your light image, used for dark too"
        )
        shows(member(page, "codex-1"), "codex", "light", where=f"after refusing {name}")
        expect(page.locator("#agent-icon-upload-codex-dark")).to_be_enabled()
    # the browser logs each refused request as a console error: the four the server answered (the page
    # sends nothing for the file that is over the limit), and nothing else
    assert len(ui.problems) == 4 and all(
        re.search(r"status of 4\d\d .*/api/agent-icons/codex/dark", p) for p in ui.problems
    ), ui.problems
    ui.problems.clear()
    data, mime = picture("codex", "dark")
    choose(page, "codex", "dark", data, mime)
    expect(page.locator("#agent-icons-status")).to_have_text("Set the dark image for Codex.")
    expect(error).to_have_text("")


# ------------------------------------------------------------------ other pages
def wait_background(page: Page, name: str, pattern: str, present: bool = True) -> None:
    """Wait until the member's avatar background does (or doesn't) match ``pattern``."""
    wait_js(
        page,
        """([name, pattern, present]) => {
          const el = document.querySelector('#buddy-list .member[data-name="' + name + '"] .avatar');
          return new RegExp(pattern).test(getComputedStyle(el).backgroundImage) === present;
        }""",
        [name, pattern, present],
    )


def test_another_page_follows_an_upload_and_a_reset_without_a_reload(ui: UI) -> None:
    """A second viewer's page, open before the change: it gets an ``icons`` frame and refetches the
    stylesheet. The reset matters most: the page's first stylesheet (linked, with the image in it) must
    stop applying, or the old image stays until a reload. Fails if either needs a reload."""
    put_icon(ui.world, "devin", "light")  # in the linked stylesheet of a page that loads now
    viewer = ui.open(color_scheme="light")
    shows(member(viewer, "devin-1"), "devin", "light", where="the viewer, on load")
    put_icon(ui.world, "codex", "light")
    wait_background(viewer, "codex-1", "agent-icons[/]codex")
    shows(member(viewer, "codex-1"), "codex", "light", where="the viewer, another harness set")
    shows(member(viewer, "devin-1"), "devin", "light", where="the viewer, the first stays")
    assert ui.world.web is not None
    ui.world.web.delete("/api/agent-icons/devin", headers=ui.world.broker.write_headers())
    wait_background(viewer, "devin-1", "agent-icons[/]devin", present=False)
    built_in(member(viewer, "devin-1"), "devin")
    shows(member(viewer, "codex-1"), "codex", "light", where="the viewer, the other harness stays")


class Hosted(NamedTuple):
    broker: InProcBroker
    admin: str  # the admin's session cookie
    bob: str  # bob's


@pytest.fixture(scope="module")
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Hosted]:
    """A hosted broker set up through its API: the admin with a password, bob who signs in, a room."""
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
            assert c.post("/api/rooms", json={"name": "#build"}).status_code == 200
            admin = c.cookies.get("switchboard_session")
        assert admin
        try:
            yield Hosted(b, admin, person_cookie((b, admin), "bob"))
        finally:
            b.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def hosted_ui(browser: Browser, hosted: Hosted, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, None)  # type: ignore[arg-type]  # no seeded world: the pages are opened by hand
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    h = {"Origin": hosted.broker.state.web_origin.origin, "X-Switchboard": "1"}
    with httpx.Client(
        base_url=hosted.broker.state.web_origin.origin,
        headers=h,
        cookies={"switchboard_session": hosted.admin},
    ) as c:
        c.delete("/api/agent-icons/codex")
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def open_as(ui: UI, hosted: Hosted, cookie: str) -> Page:
    origin = hosted.broker.state.web_origin.origin
    ctx = ui.context()
    ctx.add_cookies([{"name": "switchboard_session", "value": cookie, "url": origin}])
    page = ctx.new_page()
    page.goto(origin + "/")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    return page


def test_a_person_who_is_not_the_admin_sees_the_icons_but_not_the_section(
    hosted_ui: UI, hosted: Hosted
) -> None:
    """On a hosted broker the admin's uploads are what everyone sees (bob's welcome page draws the admin's
    Codex image), and bob's Settings → Appearance has no Agent icons section at all, while the admin's
    does. Fails if the section is shown to anyone but the admin, or bob's page lacks the stylesheet."""
    origin = hosted.broker.state.web_origin.origin
    data, mime = picture("codex", "light")
    h = {"Origin": origin, "X-Switchboard": "1", "Content-Type": mime}
    with httpx.Client(base_url=origin, headers=h, cookies={"switchboard_session": hosted.admin}) as c:
        assert c.put("/api/agent-icons/codex/light", content=data).status_code == 200
    admin = open_as(hosted_ui, hosted, hosted.admin)
    open_settings(admin, "appearance")
    expect(admin.locator("#agent-icons-title")).to_have_text(HEADING)
    expect(admin.locator(".agent-icon-row")).to_have_count(4)
    bob = open_as(hosted_ui, hosted, hosted.bob)
    shows(bob.locator(".harness-head .avatar.h-codex"), "codex", "light", where="bob's welcome page")
    open_settings(bob, "appearance")
    expect(bob.locator("#settings-theme-system")).to_be_visible()
    expect(bob.locator("#settings-agent-icons")).to_have_count(0)
    expect(bob.locator("#agent-icons-title")).to_have_count(0)
    # and the server says no to what the page would not have offered
    cookie = {"switchboard_session": hosted.bob}
    with httpx.Client(base_url=origin, headers=h, cookies=cookie) as c:
        assert c.put("/api/agent-icons/codex/light", content=data).status_code == 403
        assert c.delete("/api/agent-icons/codex").status_code == 403
