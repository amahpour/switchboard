"""Screenshots of the web UI (issue #19) for the README, docs/USAGE.md and the PR.

    uv run python docs/media/ui_shots.py              # writes docs/media/ui/*.png
    uv run python docs/media/ui_shots.py --out /tmp/shots

Run by hand (and by CI's ``frontend`` job, into a temp dir it uploads), never by pytest: it is
not under tests/ and nothing imports it. It needs Playwright's Chromium, once per machine::

    uv run playwright install chromium          # add --with-deps on a bare Linux box

What it does:
- **A throwaway home.** HOME points at a temp dir, the agent-harness variables are dropped and
  the human's name is pinned (what ``tests/conftest.py``'s ``sanitize_env`` does), so
  ``~/.switchboard`` is never read or written.
- **Seeded state**, from ``tests/ui_world.py`` (shared with the Playwright tests in
  ``tests/e2e/``): three rooms (``#docs`` closed), a never-enabled ``fpga-pi`` remote and four
  agents in ``#build`` with a Markdown-rich conversation. See that module for how.
- **Headless Chromium through Playwright**, in UTC: sign in through a one-time link, wait for
  "Connected", and shoot at 1440x900 (DPR 2) and 390x844 (a phone, DPR 3), light and dark.
  The phone shots use a second browser context that carries the desktop's session cookie (a
  second sign-in would post a live "new web login" notice into the open page's log); the
  Welcome view and the sign-in page use contexts of their own.

The outputs: desktop-{light,dark}, markdown-{light,dark}, palette-light, mention-light,
mention-midname-{light,dark,phone-light} (a mid-name match, #112),
mention-highlight-{light,dark,phone-light} (a known @mention styled as you type it, #110),
broadcast-popover-{light,dark,phone-light} (@here/@everyone above the agents in the @ popover,
#111), broadcast-mention-{light,dark,phone-light} (a sent @everyone styled as a mention, #111),
humans-popover-{light,dark,phone-light} (@humans above @here/@everyone in the @ popover, #138),
humans-mention-{light,dark,phone-light} (an agent's own @humans styled as a mention, #138),
composer-multiline-{light,dark,phone-light} (the composer grown with wrapped text, #130),
room-badges-{light,dark,phone-light} (the red count beside the quiet dot, #109),
focus-mode-{light,dark,phone-light} (agent chat collapsed to one line, a reply to you left
expanded, #99), settings-rooms-{light,dark} and wake-settings-{light,dark,phone-light} (the
default wake budget and hop limit, and a room's own rate, #131), settings-appearance-{light,dark},
settings-phone-list-{light,dark} and settings-phone-rooms-light (Settings' tabbed layout and
phone list, #232),
closed-light, remotes-light, inspector-light, inspector-remote-light, inspector-dark-parked,
phone-light, phone-dark-sheet, offline-light, offline-dark, offline-phone-dark (#129), welcome-light and login-light, plus, from a hosted broker
(``tests/ui_world.py``'s ``HostedWorld``, with Chromium's virtual authenticators as the
passkeys; issues #41 and #61): signin-setup-light, setup-light, people-light, people-dark,
passkeys-light, passkeys-dark, people-opening-{light,dark,phone-light} (initial People fetch held),
people-edit-{light,dark,phone-light} (Copy's fade timer while an email is selected),
confirm-light, signin-light, setup-person-light and
signin-phone-dark, and from a second one at
``http://localhost:<port>``, with test machines that dial in (``tests/fakes/fake_machine.py``:
made-up facts, the real dialer): machines-pairing-light, machines-pairing-dark,
machines-approve-light, machines-approve-dark, machines-up-light and machines-phone-dark (all
.png). In those, the pairing command's ``http://localhost:<port>`` reads ``https://sb.example.com``,
the address a real deployment shows.

Every wait has a deadline (Playwright's auto-waiting ``expect`` and ``ui_world.wait_js``); a
timeout exits non-zero. The agents, both brokers and the browser are stopped in ``finally``;
the temp homes are removed unless ``--keep``.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "media" / "ui"

# (viewport, device pixel ratio, phone) for the two sizes the pictures come in
DESKTOP: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 2}
PHONE: dict[str, Any] = {
    "viewport": {"width": 390, "height": 844},
    "device_scale_factor": 3,
    "is_mobile": True,
    "has_touch": True,
}
TALL = 2400  # a desktop viewport tall enough for the whole #build conversation, cropped to it

# A message with no '\n' in it, long enough to wrap onto several lines (issue #130: the
# composer growing with wrapped text, not just explicit line breaks).
MULTILINE_MESSAGE = (
    "Pulled the composer bug today: autoGrow only counted explicit line breaks, so a long "
    "message that just wrapped never grew the box, and the first lines scrolled out of view "
    "above the caret."
)

# Chromium's own HOME: macOS Chrome's network stack can hang under a HOME with no keychain,
# so the browser alone keeps the real one (it still runs in a temporary profile).
REAL_HOME = os.environ.get("HOME", "")

WAIT_MS = 20_000  # the default deadline for every wait


# ------------------------------------------------------------------ isolation
def isolate() -> Path:
    """What tests/conftest.py's ``sanitize_env`` does, without pytest's fixtures: drop the
    agent-harness variables, point HOME at a temp dir and pin the human's name."""
    for k in list(os.environ):
        if k.startswith(("CLAUDE", "CODEX_", "CURSOR_", "DEVIN_", "CHISEL_", "AI_AGENT")):
            del os.environ[k]
    fake_home = Path(tempfile.mkdtemp(prefix="yk-home-", dir="/tmp"))
    os.environ["HOME"] = str(fake_home)
    os.environ.pop("SWITCHBOARD_HOME", None)
    os.environ["SWITCHBOARD_TEST"] = "1"
    os.environ["TZ"] = "UTC"
    sys.path.insert(0, str(ROOT / "tests"))  # conftest, fakes/ and ui_world
    from conftest import TEST_HUMAN

    import switchboard.config

    switchboard.config.getpass = types.SimpleNamespace(getuser=lambda: TEST_HUMAN)  # type: ignore[assignment]
    return fake_home


# ------------------------------------------------------------------ page helpers
def settle(page: Any) -> None:
    """Fonts loaded, two frames drawn, and no CSS transition or animation still running."""
    from ui_world import wait_js  # not page.wait_for_function: the page's CSP refuses its eval

    page.evaluate("document.fonts ? document.fonts.ready.then(() => true) : true")
    wait_js(
        page,
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r("
        "document.getAnimations().every(a => a.playState !== 'running')))))",
        timeout_ms=10_000,
    )


def shot(page: Any, out: Path, name: str, clip: dict[str, float] | None = None) -> None:
    settle(page)
    path = out / name
    page.screenshot(path=str(path), clip=clip, animations="disabled", caret="hide")
    print(path)


def connected(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#st-conn")).to_have_text("Connected", timeout=WAIT_MS)


def sign_in(page: Any, b: Any) -> None:
    page.goto(b.login_url())  # the one-time link answers 303 to "/", which sets the cookie
    connected(page)


def open_build(page: Any, n_chat: int) -> None:
    from playwright.sync_api import expect

    page.evaluate("location.hash = 'build'")
    expect(page.locator("#room-title")).to_have_text("build", timeout=WAIT_MS)
    from ui_world import wait_js

    wait_js(
        page, f"() => document.querySelectorAll('#log .line.k-chat').length >= {n_chat}", timeout_ms=WAIT_MS
    )
    expect(page.locator("#buddy-list .member")).to_have_count(4, timeout=WAIT_MS)


def fresh(page: Any, n_chat: int) -> None:
    """Reload the page and reopen #build: every shot after a tall crop, a sheet or a scheme
    change starts from a clean page, with the log scrolled to its end."""
    page.reload()
    connected(page)
    open_build(page, n_chat)


def inspect(page: Any, name: str) -> None:
    """Open ``name`` in the Inspector and wait for its detail GET (which fills the session and
    the timeline)."""
    from playwright.sync_api import expect

    with page.expect_response(
        lambda r: f"/members/{name}" in r.url and r.request.method == "GET", timeout=WAIT_MS
    ):
        page.click(f'#buddy-list .member[data-name="{name}"]')
    expect(page.locator("#insp-name")).to_contain_text(name, timeout=WAIT_MS)


def back(page: Any) -> None:
    from playwright.sync_api import expect

    page.click("#insp-back")
    expect(page.locator("#pane")).not_to_have_class(re.compile(r"\binspecting\b"), timeout=WAIT_MS)


def type_in(page: Any, text: str) -> None:
    page.focus("#input")
    page.keyboard.insert_text(text)  # fires the input event the popovers listen to


def clear_input(page: Any) -> None:
    page.keyboard.press("Escape")
    page.fill("#input", "")
    page.evaluate("document.getElementById('input').blur()")


def log_clip(page: Any) -> dict[str, float]:
    """The log's box, from its first row to its last (12 px margins)."""
    return page.evaluate(
        "(() => { const log = document.getElementById('log'), rows = [...log.children]"
        ".filter(e => e.classList.contains('line') || e.classList.contains('day'));"
        " const L = log.getBoundingClientRect(), a = rows[0].getBoundingClientRect(),"
        " z = rows[rows.length - 1].getBoundingClientRect();"
        " return {x: L.left, y: Math.max(0, a.top - 12), width: L.width,"
        " height: z.bottom + 12 - Math.max(0, a.top - 12)}; })()"
    )


# ------------------------------------------------------------------ the shots
def shoot(browser: Any, out: Path, world: Any) -> None:
    from playwright.sync_api import expect

    b, empty, n_chat = world.broker, world.empty, world.n_chat
    base = {"timezone_id": "UTC", "color_scheme": "light", "locale": "en-US"}
    desk = browser.new_context(**base, **DESKTOP)
    desk.set_default_timeout(WAIT_MS)
    page = desk.new_page()
    try:
        sign_in(page, b)
        open_build(page, n_chat)
        shot(page, out, "desktop-light.png")

        page.set_viewport_size({"width": 1440, "height": TALL})
        shot(page, out, "markdown-light.png", log_clip(page))
        page.set_viewport_size(DESKTOP["viewport"])

        type_in(page, "/")
        expect(page.locator("#palette")).to_be_visible()
        shot(page, out, "palette-light.png")
        clear_input(page)
        type_in(page, "@co")
        expect(page.locator("#mentions")).to_be_visible()
        shot(page, out, "mention-light.png")
        clear_input(page)

        # the composer growing with wrapped text (#130), not just explicit line breaks
        type_in(page, MULTILINE_MESSAGE)
        shot(page, out, "composer-multiline-light.png")
        clear_input(page)

        page.click("#closed-rooms")
        expect(page.locator("#closed-body .closed-card").first).to_be_visible()
        shot(page, out, "closed-light.png")
        page.click("#closed-close")
        page.click("#remotes .remote")
        expect(page.locator("#remotes-body .remote-card").first).to_be_visible()
        shot(page, out, "remotes-light.png")
        page.click("#remotes-close")

        fresh(page, n_chat)
        inspect(page, "bench")
        shot(page, out, "inspector-remote-light.png")
        back(page)

        page.emulate_media(color_scheme="dark")
        fresh(page, n_chat)
        shot(page, out, "desktop-dark.png")
        page.set_viewport_size({"width": 1440, "height": TALL})
        shot(page, out, "markdown-dark.png", log_clip(page))
        page.set_viewport_size(DESKTOP["viewport"])
        fresh(page, n_chat)
        inspect(page, "devin-1")
        shot(page, out, "inspector-dark-parked.png")
        back(page)
        type_in(page, MULTILINE_MESSAGE)
        shot(page, out, "composer-multiline-dark.png")
        clear_input(page)
        page.emulate_media(color_scheme="light")

        # the phone: a context of its own (DPR 3, touch) with this one's session cookie
        phone_ctx = browser.new_context(**base, **PHONE, storage_state=desk.storage_state())
        phone_ctx.set_default_timeout(WAIT_MS)
        try:
            ph = phone_ctx.new_page()
            ph.goto(b.base + "/")
            connected(ph)
            open_build(ph, n_chat)
            shot(ph, out, "phone-light.png")
            type_in(ph, MULTILINE_MESSAGE)
            shot(ph, out, "composer-multiline-phone-light.png")
            clear_input(ph)
            ph.emulate_media(color_scheme="dark")
            ph.click("#buddy-toggle")
            expect(ph.locator("#app")).to_have_class(re.compile(r"\bsheet-open\b"))
            shot(ph, out, "phone-dark-sheet.png")
        finally:
            phone_ctx.close()

        fresh(page, n_chat)
        # /hold is shown in one shot (the Inspector's Held chip and Release button), then released
        world.command("build", "/hold codex-1")
        expect(page.locator('#buddy-list .member[data-name="codex-1"]')).to_contain_text("held")
        inspect(page, "codex-1")
        page.click("#insp-body .queue-toggle")
        expect(page.locator("#insp-queue")).to_be_visible()
        shot(page, out, "inspector-light.png")
        world.command("build", "/release codex-1")
    finally:
        desk.close()

    # first run: the broker with no rooms, in a context of its own
    first = browser.new_context(**base, **DESKTOP)
    first.set_default_timeout(WAIT_MS)
    try:
        p = first.new_page()
        sign_in(p, empty)
        expect(p.locator("#empty")).to_be_visible()
        shot(p, out, "welcome-light.png")
    finally:
        first.close()

    # the sign-in page: no cookie at all
    anon = browser.new_context(**base, **DESKTOP)
    anon.set_default_timeout(WAIT_MS)
    try:
        p = anon.new_page()
        p.goto(empty.base + "/")
        expect(p.locator("body")).to_contain_text("switchboard login")
        shot(p, out, "login-light.png")
    finally:
        anon.close()


def _offline_routes(down: dict[str, bool], live: list[Any], held: list[Any]) -> tuple[Any, Any]:
    """The WebSocket route (connects, and remembers the socket) and the ``/api/me`` route (held
    while ``down``) for one context: one-argument handlers, as Playwright calls them."""

    def socket(ws: Any) -> None:
        ws.connect_to_server()
        live.append(ws)

    def me(route: Any) -> None:
        if down["on"]:
            held.append(route)
        else:
            route.continue_()

    return socket, me


def shoot_offline(browser: Any, out: Path, world: Any) -> None:
    """The page after its connection dropped (#129), in #build: the band, the You row, the
    header chip and the dimmed members (light, dark), and the phone (dark). The socket is
    closed through Playwright's WebSocket routing, and the ``/api/me`` check the page makes
    before it reconnects is held until the shot is taken."""
    from playwright.sync_api import expect

    b, n_chat = world.broker, world.n_chat
    base = {"timezone_id": "UTC", "color_scheme": "light", "locale": "en-US"}
    for name, size, scheme in (
        ("offline-light.png", DESKTOP, "light"),
        ("offline-dark.png", DESKTOP, "dark"),
        ("offline-phone-dark.png", PHONE, "dark"),
    ):
        down = {"on": False}
        live: list[Any] = []
        held: list[Any] = []
        socket, me = _offline_routes(down, live, held)

        ctx = browser.new_context(**{**base, "color_scheme": scheme}, **size)
        ctx.set_default_timeout(WAIT_MS)
        ctx.route_web_socket("**/ws", socket)
        ctx.route("**/api/me", me)
        try:
            page = ctx.new_page()
            sign_in(page, b)
            open_build(page, n_chat)
            down["on"] = True
            with page.expect_request("**/api/me"):
                live[-1].close()
            expect(page.locator("#st-conn")).to_have_text("Reconnecting…")
            page.evaluate("document.activeElement && document.activeElement.blur()")
            shot(page, out, name)
        finally:
            down["on"] = False
            while held:
                held.pop().continue_()
            ctx.close()


def shoot_board(browser: Any, out: Path, world: Any) -> None:
    """The review board (#80): a room of its own, after every #build shot, so none of those
    shows its board button. Its question open on the right, in light, dark and on a phone."""
    from playwright.sync_api import expect

    world.create_room("#shop-review")
    world.add_agents("#shop-review", ("claude-1", "codex-1"))
    world.seed_review("#shop-review")
    base = {"timezone_id": "UTC", "locale": "en-US"}
    for name, ctx_args in (
        ("board-light.png", {**DESKTOP, "color_scheme": "light"}),
        ("board-dark.png", {**DESKTOP, "color_scheme": "dark"}),
        ("board-phone-light.png", {**PHONE, "color_scheme": "light"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="#shop-review"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            page.click("#board-toggle")
            page.click('#board .board-card[data-item="Q1"]')
            expect(page.locator("#board .board-detail")).to_be_visible()
            settle(page)
            shot(page, out, name)
        finally:
            ctx.close()


def shoot_mentions(browser: Any, out: Path, world: Any) -> None:
    """The @ popover on a mid-name match (issue #112): a room of its own, with agent names long
    enough that "@skill" matches inside one, not at its start. Light, dark and a phone."""
    from playwright.sync_api import expect

    world.create_room("#ops-room")
    world.add_agents("#ops-room", ("darius-skills-agent", "mr-owner-2"))
    base = {"timezone_id": "UTC", "locale": "en-US"}
    for name, ctx_args in (
        ("mention-midname-light.png", {**DESKTOP, "color_scheme": "light"}),
        ("mention-midname-dark.png", {**DESKTOP, "color_scheme": "dark"}),
        ("mention-midname-phone-light.png", {**PHONE, "color_scheme": "light"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="#ops-room"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            type_in(page, "@skill")
            expect(page.locator("#mentions")).to_be_visible()
            expect(page.locator("#men-darius-skills-agent")).to_be_visible()
            shot(page, out, name)
        finally:
            ctx.close()


def shoot_mention_highlight(browser: Any, out: Path, world: Any) -> None:
    """The composer's mirror layer behind #input (issue #110), in #build: @claude-1 (an active
    member) gets the same highlighted look a sent message's mention gets, the moment it's typed;
    @nobody (no such member or person) stays plain text. Light, dark and a phone."""
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "locale": "en-US"}
    for name, ctx_args in (
        ("mention-highlight-light.png", {**DESKTOP, "color_scheme": "light"}),
        ("mention-highlight-dark.png", {**DESKTOP, "color_scheme": "dark"}),
        ("mention-highlight-phone-light.png", {**PHONE, "color_scheme": "light"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            open_build(page, world.n_chat)
            type_in(page, "ask @claude-1 to take a look, cc @nobody")
            expect(page.locator("#input-mirror .mention-hl")).to_have_text("@claude-1")
            shot(page, out, name)
            clear_input(page)
        finally:
            ctx.close()


def shoot_broadcast_popover(browser: Any, out: Path, world: Any) -> None:
    """@here/@everyone (issue #111): the @ popover, above the agent list, each with its own
    one-line description. A room of its own; light, dark and the phone."""
    from playwright.sync_api import expect

    world.create_room("#ops-broadcast")
    world.add_agents("#ops-broadcast", ("claude-1", "codex-1"))
    base = {"timezone_id": "UTC", "locale": "en-US"}
    for name, ctx_args in (
        ("broadcast-popover-light.png", {**DESKTOP, "color_scheme": "light"}),
        ("broadcast-popover-dark.png", {**DESKTOP, "color_scheme": "dark"}),
        ("broadcast-popover-phone-light.png", {**PHONE, "color_scheme": "light"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="#ops-broadcast"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            type_in(page, "@")
            expect(page.locator("#men-here")).to_be_visible()
            expect(page.locator("#men-everyone")).to_be_visible()
            shot(page, out, name)
        finally:
            ctx.close()


def shoot_broadcast_mention(browser: Any, out: Path, world: Any) -> None:
    """A sent @everyone message (issue #111), styled as a mention in the log (#110) the same
    way a real @name is. The same room as shoot_broadcast_popover, one message posted once;
    light, dark and the phone."""
    from playwright.sync_api import expect

    world.say("ops-broadcast", "@everyone the deploy's done, status check when you can")
    base = {"timezone_id": "UTC", "locale": "en-US"}
    for name, ctx_args in (
        ("broadcast-mention-light.png", {**DESKTOP, "color_scheme": "light"}),
        ("broadcast-mention-dark.png", {**DESKTOP, "color_scheme": "dark"}),
        ("broadcast-mention-phone-light.png", {**PHONE, "color_scheme": "light"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="#ops-broadcast"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            expect(page.locator("#log .md-mention").last).to_have_text("@everyone")
            shot(page, out, name)
        finally:
            ctx.close()


def shoot_humans_popover(browser: Any, out: Path, world: Any) -> None:
    """@humans (issue #138): the @ popover, leading @here/@everyone, with its own one-line
    description. A room of its own; light, dark and the phone."""
    from playwright.sync_api import expect

    world.create_room("#ops-humans")
    world.add_agents("#ops-humans", ("claude-1", "codex-1"))
    base = {"timezone_id": "UTC", "locale": "en-US"}
    for name, ctx_args in (
        ("humans-popover-light.png", {**DESKTOP, "color_scheme": "light"}),
        ("humans-popover-dark.png", {**DESKTOP, "color_scheme": "dark"}),
        ("humans-popover-phone-light.png", {**PHONE, "color_scheme": "light"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="#ops-humans"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            type_in(page, "@")
            expect(page.locator("#men-humans")).to_be_visible()
            expect(page.locator("#men-here")).to_be_visible()
            expect(page.locator("#men-everyone")).to_be_visible()
            shot(page, out, name)
        finally:
            ctx.close()


def shoot_humans_mention(browser: Any, out: Path, world: Any) -> None:
    """An agent's own @humans message (issue #138), styled as a mention in the log (#110) the
    same way a real @name is -- from the agent's say(), not a person's broadcast. The same room
    as shoot_humans_popover, one message posted once; light, dark and the phone."""
    from playwright.sync_api import expect

    world.agent_say(
        "#ops-humans", "codex-1", "@humans need a call: ship the migration now or wait for the freeze?"
    )
    base = {"timezone_id": "UTC", "locale": "en-US"}
    for name, ctx_args in (
        ("humans-mention-light.png", {**DESKTOP, "color_scheme": "light"}),
        ("humans-mention-dark.png", {**DESKTOP, "color_scheme": "dark"}),
        ("humans-mention-phone-light.png", {**PHONE, "color_scheme": "light"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="#ops-humans"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            expect(page.locator("#log .md-mention").last).to_have_text("@humans")
            shot(page, out, name)
        finally:
            ctx.close()


def shoot_room_badges(browser: Any, out: Path, world: Any) -> None:
    """Room badges (#109): the sidebar's red count for a message addressed to the person (an
    @mention here) beside the quiet dot for other agent chatter, in the same room list, so the
    two never look alike. Two rooms of their own, left as they are while #build stays open;
    light, dark and the phone's rooms drawer."""
    from conftest import TEST_HUMAN
    from playwright.sync_api import expect

    world.create_room("#ops-watch")
    world.create_room("#ops-mentioned")
    world.add_agents("#ops-watch", ("scout",))
    world.add_agents("#ops-mentioned", ("scout",))
    world.agent_say("#ops-watch", "scout", "running the nightly sweep now, nothing urgent")
    world.agent_say("#ops-mentioned", "scout", f"@{TEST_HUMAN} the sweep found something, can you look?")

    base = {"timezone_id": "UTC", "locale": "en-US"}
    for name, ctx_args in (
        ("room-badges-light.png", {**DESKTOP, "color_scheme": "light"}),
        ("room-badges-dark.png", {**DESKTOP, "color_scheme": "dark"}),
        ("room-badges-phone-light.png", {**PHONE, "color_scheme": "light"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            watch_tab = page.locator('#tabs .room[data-room="#ops-watch"]')
            mentioned_tab = page.locator('#tabs .room[data-room="#ops-mentioned"]')
            if not watch_tab.is_visible():
                page.click("#rooms-toggle")
            # wait for each room's marker (its badge or its dot) to land before shooting
            expect(watch_tab.locator(".badge, .dot")).to_be_visible()
            expect(mentioned_tab.locator(".badge, .dot")).to_be_visible()
            shot(page, out, name)
        finally:
            ctx.close()


def shoot_focus_mode(browser: Any, out: Path, world: Any) -> None:
    """Focus mode (#99): the header toggle collapses agent chat not addressed to the person to
    one line - the same addressedToMe check as the room badges (#109) - while a reply to their
    own message stays expanded. A room of its own, left with Focus on; light, dark and the
    phone."""
    from playwright.sync_api import expect
    from ui_world import wait_js

    world.create_room("#focus-demo")
    world.add_agents("#focus-demo", ("scout",))
    asked = world.say("focus-demo", "any update on the sweep?")
    world.agent_say("#focus-demo", "scout", "on it, checking now", reply_to=asked)
    world.agent_say("#focus-demo", "scout", "ran the full sweep: nothing out of range, logs attached")
    world.agent_say("#focus-demo", "scout", "closing this out unless you want a second pass")

    base = {"timezone_id": "UTC", "locale": "en-US"}
    for name, ctx_args in (
        ("focus-mode-light.png", {**DESKTOP, "color_scheme": "light"}),
        ("focus-mode-dark.png", {**DESKTOP, "color_scheme": "dark"}),
        ("focus-mode-phone-light.png", {**PHONE, "color_scheme": "light"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="#focus-demo"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            expect(page.locator("#room-title")).to_have_text("focus-demo", timeout=WAIT_MS)
            wait_js(
                page, "() => document.querySelectorAll('#log .line.k-chat').length >= 4", timeout_ms=WAIT_MS
            )
            page.click("#focus-toggle")
            expect(page.locator("#focus-toggle")).to_have_attribute("aria-pressed", "true")
            expect(page.locator(".collapsed-line").first).to_be_visible()
            shot(page, out, name)
        finally:
            ctx.close()


def shoot_wake_settings(browser: Any, out: Path, world: Any) -> None:
    """Default wake budget and hop limit, and a room's own rate (#131): Settings' New rooms
    group (budget and hop limit beside the existing room rules default; light, dark), and a
    room's own Wake settings dialog with the hop-limit-0 warning showing (light, dark, phone).
    Neither Save is clicked, so nothing here changes the seeded world for a shot after it."""
    from playwright.sync_api import expect

    world.create_room("#wake-demo")

    base = {"timezone_id": "UTC", "locale": "en-US"}
    for name, ctx_args in (
        ("settings-rooms-light.png", {**DESKTOP, "color_scheme": "light"}),
        ("settings-rooms-dark.png", {**DESKTOP, "color_scheme": "dark"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            page.click("#me-settings")
            expect(page.locator("#app-dialog")).to_be_visible()
            page.click("#settings-tab-rooms")  # Settings is tabbed (#232); New rooms isn't the default
            page.fill("#settings-budget", "120")
            page.fill("#settings-hops", "0")
            page.locator("#settings-hops-warn").scroll_into_view_if_needed()
            page.evaluate("document.activeElement && document.activeElement.blur()")
            shot(page, out, name)
        finally:
            ctx.close()

    for name, ctx_args in (
        ("wake-settings-light.png", {**DESKTOP, "color_scheme": "light"}),
        ("wake-settings-dark.png", {**DESKTOP, "color_scheme": "dark"}),
        ("wake-settings-phone-light.png", {**PHONE, "color_scheme": "light"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="#wake-demo"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            expect(page.locator("#room-title")).to_have_text("wake-demo", timeout=WAIT_MS)
            page.click("#room-wake")
            expect(page.locator("#app-dialog")).to_be_visible()
            page.fill("#app-dialog-budget", "300")
            page.fill("#app-dialog-hops", "0")
            expect(page.locator("#app-dialog-wake-warn")).to_be_visible()
            page.evaluate("document.activeElement && document.activeElement.blur()")
            shot(page, out, name)
        finally:
            ctx.close()


def shoot_settings_tabs(browser: Any, out: Path, world: Any) -> None:
    """Settings' tabbed layout (#232, closes #212): the Appearance group on a desktop (light,
    dark), and on a phone the group list and one group's own page (light)."""
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "locale": "en-US"}
    for name, ctx_args in (
        ("settings-appearance-light.png", {**DESKTOP, "color_scheme": "light"}),
        ("settings-appearance-dark.png", {**DESKTOP, "color_scheme": "dark"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            page.click("#me-settings")
            expect(page.locator("#app-dialog")).to_be_visible()
            page.click("#settings-tab-appearance")
            page.evaluate("document.activeElement && document.activeElement.blur()")
            shot(page, out, name)
        finally:
            ctx.close()

    for name, ctx_args in (
        ("settings-phone-list-light.png", {**PHONE, "color_scheme": "light"}),
        ("settings-phone-list-dark.png", {**PHONE, "color_scheme": "dark"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            page.click("#rooms-toggle")
            page.click("#me-settings")
            expect(page.locator("#settings-list")).to_be_visible()
            shot(page, out, name)
        finally:
            ctx.close()

    ctx = browser.new_context(**base, **PHONE, color_scheme="light")
    ctx.set_default_timeout(WAIT_MS)
    try:
        page = ctx.new_page()
        sign_in(page, world.broker)
        page.click("#rooms-toggle")
        page.click("#me-settings")
        page.click("#settings-row-rooms")
        expect(page.locator("#app-dialog-title")).to_have_text("New rooms")
        shot(page, out, "settings-phone-rooms-light.png")
    finally:
        ctx.close()


# ---------------------------------------------------------- the hosted shots
AUTHENTICATOR = {
    "protocol": "ctap2",
    "transport": "internal",
    "hasResidentKey": True,
    "hasUserVerification": True,
    "isUserVerified": True,
    "automaticPresenceSimulation": True,
}


def virtual_authenticator(ctx: Any, page: Any) -> Any:
    """A passkey device for ``page``: Chromium's virtual authenticator, through CDP."""
    return device(ctx, page)[0]


def device(ctx: Any, page: Any) -> tuple[Any, str]:
    """``virtual_authenticator``, with the authenticator's id (to copy its passkeys)."""
    cdp = ctx.new_cdp_session(page)
    cdp.send("WebAuthn.enable", {"enableUI": False})
    return cdp, cdp.send("WebAuthn.addVirtualAuthenticator", {"options": AUTHENTICATOR})["authenticatorId"]


def shoot_hosted(browser: Any, out: Path, world: Any) -> None:
    """A hosted broker (issues #41, #61): the sign-in page before it's set up, Choose how you'll
    sign in with the admin's email, the People sheet with an invite to send (light, dark), Settings'
    Profile and Sign-in & security groups (light, dark, #232), Confirm it's you, the sign-in page's
    three ways in, a teammate's own Choose page, and the sign-in page on a phone (dark). The
    invite's address reads https://sb.example.com."""
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "color_scheme": "light", "locale": "en-US", "reduced_motion": "reduce"}
    origin = world.origin
    one_time = world.claim_link().split("#t=", 1)[1]
    admin_pw, bob_pw = "correct horse battery", "bob's own secret 1"

    def as_deployed(p: Any) -> None:
        p.evaluate(PEOPLE_AS_DEPLOYED_JS, [origin, AS_DEPLOYED])

    ctx = browser.new_context(**base, **DESKTOP)
    ctx.set_default_timeout(WAIT_MS)
    try:
        page = ctx.new_page()
        virtual_authenticator(ctx, page)
        page.goto(origin + "/")
        expect(page.locator("#login-first")).to_be_visible()
        shot(page, out, "signin-setup-light.png")
        page.fill("#signin-password", one_time)
        page.click("#password-btn")
        expect(page.locator("#step-choose")).to_be_visible()
        page.fill("#setup-first", "Alice")  # who the admin is (#192)
        page.fill("#setup-last", "Liddell")
        page.fill("#setup-email", "alice@example.com")
        shot(page, out, "setup-light.png")
        page.fill("#new-password", admin_pw)
        page.fill("#new-password-2", admin_pw)
        page.click("#password-btn")
        connected(page)
        page.click("#open-people")
        expect(page.locator("#people-panel")).to_be_visible()
        page.fill("#person-first", "Bob")
        page.fill("#person-last", "Builder")
        page.fill("#person-email", "bob@example.com")
        page.click("#person-add")
        expect(page.locator("#copy-invite")).to_be_visible()
        invite = page.locator("#invite-text").inner_text()
        bob_otp = invite.split("one-time password ", 1)[1].split(" ", 1)[0]
        as_deployed(page)
        page.mouse.move(1, 1)  # nothing drawn hovered
        page.evaluate("document.activeElement && document.activeElement.blur()")
        shot(page, out, "people-light.png")
        page.emulate_media(color_scheme="dark")
        shot(page, out, "people-dark.png")
        page.emulate_media(color_scheme="light")
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
        page.evaluate("""() => {
          const original = window.setTimeout;
          window.setTimeout = (fn, delay, ...args) => {
            if (delay === 1600 && !window.releaseCopy) {
              window.setTimeout = original;
              window.releaseCopy = () => fn(...args);
              return 0;
            }
            return original(fn, delay, ...args);
          };
        }""")
        page.click("#copy-invite")
        expect(page.locator("#copy-invite")).to_have_attribute("aria-label", "Copied")
        page.click("#invite-done")
        email = page.locator(".person-card").nth(1).locator('input[type="email"]')
        email.click()
        email.press("ControlOrMeta+A")
        page.evaluate("() => window.releaseCopy()")
        page.keyboard.insert_text("robert@example.com")
        shot(page, out, "people-edit-light.png")
        page.emulate_media(color_scheme="dark")
        shot(page, out, "people-edit-dark.png")
        page.emulate_media(color_scheme="light")
        page.set_viewport_size(PHONE["viewport"])
        email.scroll_into_view_if_needed()
        shot(page, out, "people-edit-phone-light.png")
        page.set_viewport_size(DESKTOP["viewport"])
        page.keyboard.press("Escape")
        # Hold the initial People refresh to show what is visible on a slow connection.
        page.evaluate("""() => {
          const original = window.fetch;
          window.fetch = (...args) => {
            if (String(args[0]).endsWith('/api/people') &&
                (!args[1] || !args[1].method || args[1].method === 'GET')) {
              return new Promise(resolve => {
                window.releasePeople = () => { window.fetch = original; resolve(original(...args)); };
              });
            }
            return original(...args);
          };
        }""")
        page.click("#open-people")
        shot(page, out, "people-opening-light.png")
        page.emulate_media(color_scheme="dark")
        shot(page, out, "people-opening-dark.png")
        page.emulate_media(color_scheme="light")
        page.set_viewport_size(PHONE["viewport"])
        shot(page, out, "people-opening-phone-light.png")
        page.set_viewport_size(DESKTOP["viewport"])
        page.evaluate("() => window.releasePeople()")
        expect(page.locator("#person-first")).to_be_visible()
        page.keyboard.press("Escape")
        page.click("#me-settings")
        expect(page.locator("#app-dialog")).to_be_visible()
        expect(page.locator("#settings-name")).to_have_value("alice")  # Your name, first (#114)
        page.evaluate("document.activeElement && document.activeElement.blur()")
        shot(page, out, "settings-name-light.png")
        page.emulate_media(color_scheme="dark")
        shot(page, out, "settings-name-dark.png")
        page.emulate_media(color_scheme="light")
        page.click("#settings-tab-security")  # Settings is tabbed (#232); passkeys sit there now
        page.fill("#passkey-name", "iPhone")
        page.mouse.move(1, 1)
        shot(page, out, "passkeys-light.png")
        page.emulate_media(color_scheme="dark")
        shot(page, out, "passkeys-dark.png")
        page.emulate_media(color_scheme="light")
        page.keyboard.press("Escape")
        # the admin's check ran out: adding someone asks for the password first
        b = world.broker
        b.on_loop(lambda: b.state.passkey_checks.clear())
        page.click("#open-people")
        page.fill("#person-first", "Carol")
        page.fill("#person-last", "Ng")
        page.fill("#person-email", "carol@example.com")
        page.click("#person-add")
        expect(page.locator("#confirm-dialog")).to_be_visible()
        shot(page, out, "confirm-light.png")
        page.click("#confirm-cancel")
        page.keyboard.press("Escape")
        page.click("#me-settings")
        page.click("#settings-sign-out")
        expect(page.locator("#login-choose")).to_be_visible()
        expect(page.locator("#passkey-btn")).to_be_visible()
        page.evaluate("document.activeElement && document.activeElement.blur()")
        shot(page, out, "signin-light.png")
    finally:
        ctx.close()

    bob_ctx = browser.new_context(**base, **DESKTOP)
    bob_ctx.set_default_timeout(WAIT_MS)
    try:
        bob = bob_ctx.new_page()
        bob.goto(origin + "/")
        bob.fill("#signin-name", "bob@example.com")  # added by email (#192)
        bob.fill("#signin-password", bob_otp)
        bob.click("#password-btn")
        expect(bob.locator("#step-choose")).to_be_visible()
        bob.fill("#new-password", bob_pw)
        bob.fill("#new-password-2", bob_pw)
        shot(bob, out, "setup-person-light.png")
    finally:
        bob_ctx.close()

    phone_ctx = browser.new_context(**{**base, "color_scheme": "dark"}, **PHONE)
    phone_ctx.set_default_timeout(WAIT_MS)
    try:
        ph = phone_ctx.new_page()
        ph.goto(origin + "/")
        expect(ph.locator("#login-choose")).to_be_visible()
        ph.evaluate("document.activeElement && document.activeElement.blur()")
        shot(ph, out, "signin-phone-dark.png")
    finally:
        phone_ctx.close()


# the People sheet's text as a deployment shows it: the test broker's origin in the invite
PEOPLE_AS_DEPLOYED_JS = """([from, to]) => {
  const w = document.createTreeWalker(document.getElementById('people-body'), NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) n.nodeValue = n.nodeValue.split(from).join(to);
}"""


# -------------------------------------------------------- the machines' shots
AS_DEPLOYED = "https://sb.example.com"

# the sheet's text as a deployment shows it: the test broker's origin in the pairing command
AS_DEPLOYED_JS = """([from, to]) => {
  const w = document.createTreeWalker(document.getElementById('machines-body'), NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) n.nodeValue = n.nodeValue.split(from).join(to);
}"""


def shoot_machines(browser: Any, out: Path, world: Any) -> None:
    """Machines that dial in (issue #41 part 3): a pairing under way beside a machine that is up,
    the approval card of the one that dialed in with the code, both up, and a phone."""
    from fakes.fake_machine import TestMachine
    from playwright.sync_api import expect

    origin = world.origin
    base = {"timezone_id": "UTC", "color_scheme": "light", "locale": "en-US", "reduced_motion": "reduce"}
    machines: list[Any] = []
    ctx = browser.new_context(**base, **DESKTOP)
    ctx.set_default_timeout(WAIT_MS)
    try:
        page = ctx.new_page()
        cdp, laptop = device(ctx, page)
        page.goto(world.claim_link())
        expect(page.locator("#step-choose")).to_be_visible()
        page.fill("#setup-first", "Alice")  # who the admin is (#192)
        page.fill("#setup-last", "Liddell")
        page.fill("#setup-email", "alice@example.com")
        page.click("#passkey-btn")  # set up with a passkey instead of a password (§32.4)
        expect(page.locator("#step-backup")).to_be_visible()
        page.click("#skip-btn")
        connected(page)
        world.set_test_mode(True)  # the page has no TEST MODE band; the test machines may link now

        def add(name: str) -> str:
            page.fill("#machine-name", name)
            page.click("#machine-pair-btn")
            expect(page.locator("#pair-join")).to_be_visible()
            return page.locator("#pair-join").inner_text().split()[-1]

        def dial_in(name: str, code: str, facts: dict[str, Any]) -> None:
            m = TestMachine(origin, facts=facts)
            machines.append(m)
            m.pair(code)
            m.start()
            card = page.locator(f'.machine-card.st-pending[data-machine="{name}"]')
            expect(card).to_contain_text("It dialed in")

        def up(name: str) -> None:
            page.click(f'[data-focus="approve:{name}"]')
            expect(page.locator(f'#machines .remote.st-up[data-focus="machine:{name}"]')).to_be_visible()

        def as_deployed(p: Any) -> None:
            p.evaluate(AS_DEPLOYED_JS, [origin, AS_DEPLOYED])
            p.mouse.move(1, 1)

        page.click("#add-machine")
        expect(page.locator("#machines-panel")).to_be_visible()
        dial_in(
            "lab-pc",
            add("lab-pc"),
            {
                "hostname": "lab-pc",
                "os": "Ubuntu 24.04",
                "arch": "x86_64",
                "version": "0.6.5",
                "harnesses": ["claude", "codex"],
            },
        )
        up("lab-pc")
        code = add("work-laptop")
        as_deployed(page)
        shot(page, out, "machines-pairing-light.png")
        page.emulate_media(color_scheme="dark")
        shot(page, out, "machines-pairing-dark.png")
        page.emulate_media(color_scheme="light")
        dial_in(
            "work-laptop",
            code,
            {
                "hostname": "work-laptop",
                "os": "macOS 15.6",
                "arch": "arm64",
                "version": "0.6.5",
                "harnesses": ["claude", "codex"],
            },
        )
        page.mouse.move(1, 1)
        shot(page, out, "machines-approve-light.png")
        page.emulate_media(color_scheme="dark")
        shot(page, out, "machines-approve-dark.png")
        page.emulate_media(color_scheme="light")
        up("work-laptop")
        page.locator('.machine-card[data-machine="work-laptop"]').scroll_into_view_if_needed()
        page.mouse.move(1, 1)
        shot(page, out, "machines-up-light.png")

        # a phone: the same passkey (copied to its own authenticator), the rooms drawer, the sheet
        creds = cdp.send("WebAuthn.getCredentials", {"authenticatorId": laptop})["credentials"]
        ph_ctx = browser.new_context(**{**base, "color_scheme": "dark"}, **PHONE)
        ph_ctx.set_default_timeout(WAIT_MS)
        try:
            ph = ph_ctx.new_page()
            pcdp, phone = device(ph_ctx, ph)
            for c in creds:  # ahead of the counter the broker saw
                pcdp.send(
                    "WebAuthn.addCredential",
                    {"authenticatorId": phone, "credential": {**c, "signCount": c.get("signCount", 0) + 100}},
                )
            ph.goto(origin + "/")
            ph.click("#passkey-btn")
            connected(ph)
            ph.click("#rooms-toggle")
            ph.click("#add-machine")
            expect(ph.locator("#machines-panel")).to_be_visible()
            ph.fill("#machine-name", "build-box")
            ph.click("#machine-pair-btn")
            expect(ph.locator("#pair-join")).to_be_visible()
            as_deployed(ph)
            ph.locator("#pair-join").scroll_into_view_if_needed()
            shot(ph, out, "machines-phone-dark.png")
        finally:
            ph_ctx.close()
    finally:
        ctx.close()
        for m in machines:
            m.close()


# -------------------------------------------------------------------- main
def run(args: argparse.Namespace, pw: Any) -> None:
    from ui_world import HostedWorld, UIWorld

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    world = UIWorld(keep=args.keep)
    hosted = HostedWorld(keep=args.keep)
    # a second hosted broker, for the machines that dial in: behind localhost, which their
    # dialers (other processes) resolve too
    fleet = HostedWorld(keep=args.keep, host="localhost")
    try:
        world.start()
        hosted.start()
        fleet.start()
        env = {**os.environ, "TZ": "UTC", "HOME": REAL_HOME or os.environ["HOME"]}
        browser = pw.chromium.launch(executable_path=args.chrome or None, env=env)
        try:
            shoot(browser, out, world)
            shoot_board(browser, out, world)
            shoot_mentions(browser, out, world)
            shoot_mention_highlight(browser, out, world)
            shoot_broadcast_popover(browser, out, world)
            shoot_broadcast_mention(browser, out, world)
            shoot_humans_popover(browser, out, world)
            shoot_humans_mention(browser, out, world)
            shoot_offline(browser, out, world)
            # last of this world's shots: its two rooms' unread backlog would otherwise show up
            # (unread is recomputed per page load) in shoot_offline's sidebar too
            shoot_room_badges(browser, out, world)
            shoot_focus_mode(browser, out, world)
            shoot_wake_settings(browser, out, world)
            shoot_settings_tabs(browser, out, world)
            shoot_hosted(browser, out, hosted)
            shoot_machines(browser, out, fleet)
        finally:
            browser.close()
    finally:
        fleet.stop()
        hosted.stop()
        world.stop()
        if args.keep:
            print(f"kept: {world.home} {world.home2} {hosted.home} {fleet.home}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--out", default=str(OUT), help="output directory (default docs/media/ui)")
    ap.add_argument(
        "--chrome", help="a Chrome or Chromium binary to use instead of Playwright's own Chromium"
    )
    ap.add_argument("--keep", action="store_true", help="keep the temp homes")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    # Playwright's driver starts before HOME moves: it finds its downloaded browsers under the
    # real HOME's cache (unless PLAYWRIGHT_BROWSERS_PATH says otherwise)
    pw = sync_playwright().start()
    fake_home = isolate()
    try:
        run(args, pw)
    finally:
        pw.stop()
        shutil.rmtree(fake_home, ignore_errors=True)


if __name__ == "__main__":
    main()
