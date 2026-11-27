"""A layout sweep of every dialog and menu (#995, step 3, UI.md "Checking it").

Each surface is opened the way a person opens it, at a desktop (1440x900) and a phone (390x844), each at
the Default and the Larger text size, one test id per surface and size. Three things fail it:

1. a control's text cut or spilling: the rectangles of its text nodes (``Range.getClientRects``) against
   its padding box, 1 px of tolerance; not ``scrollWidth``, which counts a corner badge or an absolutely
   positioned child as overflow (the one-off sweep wrongly reported the phone's Members button that way);
   and an active ``text-overflow: ellipsis`` on a control whose text is wider than its box;
2. a dialog taller than the window, or its ``.app-dialog-buttons`` out of the viewport;
3. sideways scroll: ``scrollWidth > innerWidth + 1``.

Two lists ratchet, so they only shrink. ``KNOWN_SMALL_TARGETS`` holds the buttons under 44x44 px on a phone
that #995 found and nobody has fixed; ``KNOWN_CUT`` holds a cut label still on ``main``. The test fails on
a small target or a cut label that is not listed (a new one), and on a listed one that is no longer there
(fixed: delete the line). A new entry in either needs a reason and an issue (``web/REVIEW.md``).

A console error, a page error or a CSP violation fails the test too (``test_web_ui.UI``). Marker ``e2e``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from test_web_ui import PHONE, UI, open_room, open_settings, review_room, ui, world  # noqa: F401
from ui_world import wait_js

pytestmark = pytest.mark.e2e

# (surface, element) pairs, written per surface: a button under 44x44 px on the phone. #995's sweep found
# 113 of them; 8 are left, all in the room header, which gets its own redesign (#995, finding 7), and each
# is resized there, so its line goes with it. Everything else is 44 px under a coarse pointer (UI.md,
# "Touch"); a target that is small again fails the sweep. Fixing one means deleting its line.
_SMALL_ON_A_PHONE: dict[str, list[str]] = {
    "room": [
        "button#buddy-toggle",
        "button#focus-toggle",
        "button#past-boards-toggle",
        "button#room-calls",
        "button#room-rules",
        "button#room-title-button",
        "button#room-wake",
        "button#st-approvals",
    ],
}
KNOWN_SMALL_TARGETS: set[tuple[str, str]] = {
    (surface, element) for surface, elements in _SMALL_ON_A_PHONE.items() for element in elements
}

# (surface, element): a label cut at some size that is still on main (#995). Same rule: fix it, delete it.
KNOWN_CUT: set[tuple[str, str]] = set()

SETTINGS_GROUPS = ("profile", "appearance", "rooms", "agents", "files", "links", "backups")

MEASURE = """({scope, phone}) => {
  const root = document.querySelector(scope) || document.body;
  const CONTROLS = 'button, [role=button], [role=menuitem], [role=tab], [role=option], .menu-item, .chip,' +
    ' label, summary, a.btn';
  const visible = (e) => {
    if (e.closest('.sr-only, [hidden], .hidden')) return false;
    const cs = getComputedStyle(e);
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const name = (e) => e.id ? e.tagName.toLowerCase() + '#' + e.id
    : e.tagName.toLowerCase() + (e.classList.length ? '.' + e.classList[0] : '');
  const label = (e) => (e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 40);
  const out = {cut: [], ellipsis: [], small: [], dialog: [], sideways: null};

  for (const e of root.querySelectorAll(CONTROLS)) {
    if (e.matches('input[type=file]') || !visible(e)) continue;
    const cs = getComputedStyle(e);
    const box = e.getBoundingClientRect();
    // the padding box, inside the border: text past it is cut or spills out of the control. Text that
    // only reaches into the padding is still whole on screen, and how far it reaches depends on the
    // machine's fonts (CI's put "Default" 1.6 px into a phone button's padding where others did not)
    const left = box.left + parseFloat(cs.borderLeftWidth);
    const right = box.right - parseFloat(cs.borderRightWidth);
    const walker = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
    let worst = 0;
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (!n.nodeValue.trim()) continue;
      let skip = false;
      for (let p = n.parentElement; p && p !== e; p = p.parentElement) {
        const pc = getComputedStyle(p);
        // a corner badge or a nested control is measured on its own, not as this one's overflow
        if (pc.position === 'absolute' || pc.position === 'fixed' || p.matches(CONTROLS) || !visible(p)) {
          skip = true; break;
        }
      }
      if (skip) continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      for (const r of range.getClientRects()) {
        worst = Math.max(worst, left - r.left, r.right - right);
      }
    }
    if (worst > 1) out.cut.push([name(e) + ' "' + label(e) + '"', Math.round(worst * 10) / 10]);
  }

  for (const e of root.querySelectorAll('*')) {
    if (getComputedStyle(e).textOverflow !== 'ellipsis' || !e.closest(CONTROLS) || !visible(e)) continue;
    if (e.scrollWidth > e.clientWidth + 1) {
      out.ellipsis.push([name(e) + ' "' + label(e) + '"', e.scrollWidth - e.clientWidth]);
    }
  }

  if (phone) {
    const seen = new Set();
    for (const e of root.querySelectorAll('button, [role=button], [role=menuitem], [role=tab], .menu-item')) {
      if (!visible(e)) continue;
      const r = e.getBoundingClientRect();
      if (r.width < 43.5 || r.height < 43.5) seen.add(name(e));
    }
    out.small = [...seen];
  }

  for (const d of document.querySelectorAll('dialog[open]')) {
    const r = d.getBoundingClientRect();
    const id = d.id || 'dialog';
    if (r.height > innerHeight + 1 || r.width > innerWidth + 1 || r.top < -1 || r.bottom > innerHeight + 1) {
      out.dialog.push(id + ' is ' + Math.round(r.width) + 'x' + Math.round(r.height) + ' at y=' +
        Math.round(r.top) + ' in a ' + innerWidth + 'x' + innerHeight + ' window');
    }
    for (const b of d.querySelectorAll('.app-dialog-buttons')) {
      for (const btn of b.querySelectorAll('button')) {
        if (!visible(btn)) continue;
        const br = btn.getBoundingClientRect();
        if (br.top < -1 || br.bottom > innerHeight + 1 || br.left < -1 || br.right > innerWidth + 1) {
          out.dialog.push(id + ' button "' + label(btn) + '" is out of view (y ' + Math.round(br.top) +
            ' to ' + Math.round(br.bottom) + ', window ' + innerHeight + ')');
        }
      }
    }
  }

  const de = document.documentElement;
  if (de.scrollWidth > innerWidth + 1) out.sideways = de.scrollWidth + ' > ' + innerWidth;
  return out;
}"""


def set_size(page: Page, size: str) -> None:
    page.evaluate("s => { document.documentElement.dataset.textSize = s; }", size)
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


def to_rooms_list(page: Page, phone: bool) -> None:
    """The phone keeps the room list, Home, Usage, Settings and the closed rooms in a drawer."""
    if phone:
        if page.locator("#rooms-toggle").get_attribute("aria-expanded") != "true":
            page.click("#rooms-toggle")
        expect(page.locator("#app")).to_have_class(re.compile("nav-open"))
        wait_js(page, "() => document.querySelector('#new-room').getBoundingClientRect().left >= 0")


# ------------------------------------------------------------------ surfaces
# Each opens a surface and returns the selector of what to measure. ``slug`` is a room of its own.
Opener = Callable[[UI, Page, bool, str], str]


def s_room(env: UI, page: Page, phone: bool, slug: str) -> str:
    return "#app"


def s_board(env: UI, page: Page, phone: bool, slug: str) -> str:
    page.click("#board-toggle")
    expect(page.locator("#board .board-card").first).to_be_visible()
    return "#board"


def s_board_card(env: UI, page: Page, phone: bool, slug: str) -> str:
    s_board(env, page, phone, slug)
    page.locator("#board .board-card").first.click()
    expect(page.locator("#board .board-detail")).to_be_visible()
    return "#board"


def s_whiteboard(env: UI, page: Page, phone: bool, slug: str) -> str:
    page.get_by_role("button", name="Whiteboard", exact=True).click()
    expect(page.locator("#whiteboard-status")).to_contain_text("Live")
    return "#whiteboard-host"


def dialog_from(selector: str, title: str) -> Opener:
    def opener(env: UI, page: Page, phone: bool, slug: str) -> str:
        page.click(selector)
        expect(page.locator("#app-dialog")).to_be_visible()
        expect(page.locator("#app-dialog h2").first).to_contain_text(title)
        return "#app-dialog"

    return opener


def s_new_room(env: UI, page: Page, phone: bool, slug: str) -> str:
    to_rooms_list(page, phone)
    page.click("#new-room")
    expect(page.locator("#app-dialog-title")).to_have_text("New room")
    return "#app-dialog"


def s_coordinator(env: UI, page: Page, phone: bool, slug: str) -> str:
    if phone:
        page.click("#buddy-toggle")
    page.click('#buddy-list .member[data-name="codex-1"]')
    page.click("#insp-coordinator")
    expect(page.locator("#app-dialog h2")).to_have_text("Coordinator: codex-1")
    return "#app-dialog"


def s_members(env: UI, page: Page, phone: bool, slug: str) -> str:
    if phone:
        page.click("#buddy-toggle")
    expect(page.locator("#buddy-list .member")).to_have_count(2)
    return "#buddy-list"


def s_agent_panel(env: UI, page: Page, phone: bool, slug: str) -> str:
    if phone:
        page.click("#buddy-toggle")
    page.click('#buddy-list .member[data-name="codex-1"]')
    expect(page.locator("#insp-catchup")).to_be_visible()
    return "#insp-body"


def s_catchup(env: UI, page: Page, phone: bool, slug: str) -> str:
    s_agent_panel(env, page, phone, slug)
    page.click("#insp-catchup")
    expect(page.locator("#catchup-menu")).to_be_visible()
    return "#catchup-menu"


def s_palette(env: UI, page: Page, phone: bool, slug: str) -> str:
    page.locator("#input").focus()
    page.keyboard.type("/")
    expect(page.locator("#palette")).to_be_visible()
    return "#palette"


def s_mentions(env: UI, page: Page, phone: bool, slug: str) -> str:
    page.locator("#input").focus()
    page.keyboard.type("@")
    expect(page.locator("#mentions")).to_be_visible()
    return "#mentions"


def s_search(env: UI, page: Page, phone: bool, slug: str) -> str:
    env.world.say(slug, "the migration rollback path is documented")
    if phone:
        page.click("#rooms-toggle")
        page.locator("#search-input").focus()  # hands over to the page's own box
        box = page.locator("#search-page-input")
    else:
        box = page.locator("#search-input")
    box.fill("migration rollback")
    expect(page.locator("#search-results .search-hit").first).to_be_visible()
    return "#app"


def s_home(env: UI, page: Page, phone: bool, slug: str) -> str:
    to_rooms_list(page, phone)
    page.click("#home-nav")
    expect(page.locator("#room-title")).to_have_text("Home")
    return "#home"


def s_usage(env: UI, page: Page, phone: bool, slug: str) -> str:
    to_rooms_list(page, phone)
    page.click("#usage-nav")
    expect(page.locator("#usage")).to_be_visible()
    expect(page.locator("#usage-tile-wakes")).to_be_visible()
    return "#usage"


def s_closed(env: UI, page: Page, phone: bool, slug: str) -> str:
    to_rooms_list(page, phone)
    page.click("#closed-rooms")
    expect(page.locator("#closed-panel")).to_be_visible()
    return "#closed-panel"


def settings_tab(group: str) -> Opener:
    def opener(env: UI, page: Page, phone: bool, slug: str) -> str:
        to_rooms_list(page, phone)  # on a phone Settings is in the drawer
        open_settings(page, group)
        expect(page.locator("#app-dialog")).to_be_visible()
        return "#app-dialog"

    return opener


SURFACES: dict[str, tuple[Opener, bool]] = {
    # name: (opener, needs a seeded review board)
    "room": (s_room, False),
    "board": (s_board, True),
    "board-card": (s_board_card, True),
    "whiteboard": (s_whiteboard, False),
    "room-rules": (dialog_from("#room-rules", "Rules for"), False),
    "wake-settings": (dialog_from("#room-wake", "Wake"), False),
    "calls": (dialog_from("#room-calls", "Calls in"), False),
    "rename": (dialog_from("#room-title-button", "Rename"), False),
    "new-room": (s_new_room, False),
    "coordinator": (s_coordinator, False),
    "members": (s_members, False),
    "agent-panel": (s_agent_panel, False),
    "catchup-menu": (s_catchup, False),
    "palette": (s_palette, False),
    "mentions": (s_mentions, False),
    "search": (s_search, False),
    "home": (s_home, False),
    "usage": (s_usage, False),
    "closed-rooms": (s_closed, False),
    **{f"settings-{g}": (settings_tab(g), False) for g in SETTINGS_GROUPS},
}

SIZES = [
    pytest.param("desktop", "default", id="desktop-default"),
    pytest.param("desktop", "larger", id="desktop-larger"),
    pytest.param("phone", "default", id="phone-default"),
    pytest.param("phone", "larger", id="phone-larger"),
]


def measure(env: UI, surface: str, device: str, size: str, tag: str = "") -> dict[str, Any]:
    """Opens ``surface`` in a page of its own at ``device`` and ``size``; returns what ``MEASURE`` found."""
    phone = device == "phone"
    opener, needs_board = SURFACES[surface]
    # a short room name, one per page: the world is shared, and a long name is cut in the sidebar by design
    slug = f"sw{list(SURFACES).index(surface)}{device[0]}{size[0]}{tag}"
    options = PHONE if phone else {}
    if needs_board:
        page = review_room(env, slug, **options)
    else:
        env.world.create_room("#" + slug)
        env.world.add_agents("#" + slug, ("claude-1", "codex-1"))
        page = env.open(room=None, **options)
        open_room(page, slug)
    set_size(page, size)
    scope = opener(env, page, phone, slug)
    set_size(page, size)
    found: dict[str, Any] = page.evaluate(MEASURE, {"scope": scope, "phone": phone})
    return found


@pytest.mark.parametrize("device,size", SIZES)
@pytest.mark.parametrize("surface", list(SURFACES))
def test_a_surface_has_no_cut_label_no_dialog_off_the_window_and_no_sideways_scroll(
    ui: UI,  # noqa: F811
    surface: str,
    device: str,
    size: str,
) -> None:
    """Opens one surface at one window and text size and fails on: a control's text wider than its box (a
    cut or spilling label, or an active ellipsis), a dialog taller than the window or its buttons out of
    view, sideways scroll, a phone button under 44x44 px that ``KNOWN_SMALL_TARGETS`` does not list, and a
    cut label ``KNOWN_CUT`` does not list. A badge on a corner is not a cut."""
    found = measure(ui, surface, device, size)
    problems: list[str] = []
    for item in found["cut"]:
        if (surface, item[0]) not in KNOWN_CUT:
            problems.append(f"text cut or spilling by {item[1]} px: {item[0]}")
    for item in found["ellipsis"]:
        if (surface, item[0]) not in KNOWN_CUT:
            problems.append(f"ellipsis active, text {item[1]} px wider than its box: {item[0]}")
    problems += found["dialog"]
    if found["sideways"]:
        problems.append("sideways scroll: " + found["sideways"])
    for name in found["small"]:
        if (surface, name) not in KNOWN_SMALL_TARGETS:
            problems.append(f"touch target under 44x44 px: {name}")
    assert not problems, f"{surface} at {device}, {size} text:\n  " + "\n  ".join(problems)


def test_the_known_lists_hold_only_what_is_still_there(ui: UI) -> None:  # noqa: F811
    """The ratchet's other half: every entry of ``KNOWN_CUT`` and ``KNOWN_SMALL_TARGETS`` still shows at one
    of the four sizes. Fails on an entry for something that has been fixed (delete its line), or that names
    a surface or element that does not exist, so the lists only shrink."""
    surfaces = sorted({s for s, _ in KNOWN_CUT | KNOWN_SMALL_TARGETS})
    unknown = [s for s in surfaces if s not in SURFACES]
    assert not unknown, f"known lists name surfaces the sweep does not open: {unknown}"
    cut: set[tuple[str, str]] = set()
    small: set[tuple[str, str]] = set()
    cut_surfaces = {s for s, _ in KNOWN_CUT}
    for surface in surfaces:
        for device, size in (
            ("desktop", "default"),
            ("desktop", "larger"),
            ("phone", "default"),
            ("phone", "larger"),
        ):
            # a small target only counts on the phone; a cut label on any window of a surface that lists one
            if device == "desktop" and surface not in cut_surfaces:
                continue
            found = measure(ui, surface, device, size, tag="k")
            cut |= {(surface, c[0]) for c in found["cut"] + found["ellipsis"]}
            small |= {(surface, n) for n in found["small"]}
            # each page starts its own stand-in agents: end them with the page, not with the test
            ui.contexts.pop().close()
            ui.world.close_extra_agents()
    assert sorted(KNOWN_CUT - cut) == [], "no longer cut: delete from KNOWN_CUT"
    assert sorted(KNOWN_SMALL_TARGETS - small) == [], "no longer small: delete from KNOWN_SMALL_TARGETS"


@pytest.mark.parametrize("device", ["phone", "desktop"])
def test_the_touch_target_is_44_px_on_a_touch_screen_and_32_on_a_desktop(
    ui: UI,  # noqa: F811
    device: str,
) -> None:
    """``--target`` is 44 px under a coarse pointer (the touch phone context) and 32 px under a fine one, and
    the agent panel's copy-session button, 24 px on a phone before #995, is at least 44x44 px on the phone and
    is not stretched on a desktop. Fails if the ``pointer: coarse`` rules stop applying to a control, or start
    applying to a mouse."""
    phone = device == "phone"
    page = ui.open(**(PHONE if phone else {}))
    if phone:
        page.click("#buddy-toggle")
    page.click('#buddy-list .member[data-name="bench"]')  # the seeded agent that has a session
    expect(page.locator('.copy-btn[data-focus="copy-session"]')).to_be_visible()  # the details load after
    found = page.evaluate(
        """() => {
          const r = document.querySelector('.copy-btn[data-focus="copy-session"]').getBoundingClientRect();
          return {target: getComputedStyle(document.documentElement).getPropertyValue('--target').trim(),
                  w: r.width, h: r.height};
        }"""
    )
    if phone:
        assert found["target"] == "44px"
        assert found["w"] >= 43.5 and found["h"] >= 43.5, found
    else:
        assert found["target"] == "32px"
        assert found["w"] < 44 and found["h"] < 44, found


def test_a_members_name_keeps_its_tap_without_loosening_its_row(ui: UI) -> None:  # noqa: F811
    """On a touch screen a member's name is a 44 px tap, but the box reaches into the row instead of pushing
    the status line down: before that, a 12 px gap opened under every name in Members and a flag sat apart
    from its name. Fails if the name's box shrinks under 44 px, or the status line drifts from the name."""
    page = ui.open(**PHONE)
    page.click("#buddy-toggle")
    row = page.locator('#buddy-list .member[data-name="claude-1"]')
    expect(row).to_be_visible()
    found = row.evaluate(
        """li => {
          const box = e => li.querySelector(e).getBoundingClientRect();
          const name = document.createRange();
          name.selectNodeContents(li.querySelector('.m-name'));
          const bottom = name.getBoundingClientRect().bottom;
          return {tap: box('.m-open').height, gap: box('.m-status').top - bottom};
        }"""
    )
    assert found["tap"] >= 43.5, found
    assert found["gap"] <= 6, found
