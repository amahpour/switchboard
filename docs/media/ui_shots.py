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
  ``tests/e2e/``): three rooms (``#docs`` closed) and four agents in ``#build`` with a
  Markdown-rich conversation. See that module for how.
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
agent-icons-{before,after}-{light,dark,phone-light} (Members and the log with the built-in avatars, then
with images an admin uploaded, #498), agent-icons-settings-{light,dark,phone-light} (Settings →
Appearance → Agent icons) and agent-icons-tip-light (its info tip, open by keyboard),
settings-agents-{empty,made,revoke}-{light,dark} and settings-agents-phone-light (Settings →
Agents: a device's agent token made, shown once and revoked, #382),
connect-codex-{light,dark,phone-light} (Connect an agent's Codex step: the plugin, config.toml, and the
environment lines, now with the server's address for the plugin's approval hook, #472),
oauth-consent-{light,dark,phone-light} (the card an agent that signs in through the browser sends the
person to, #386), oauth-confirm-light (the same on a hosted broker whose last check ran out: Confirm
it's you comes first), oauth-signin-light (a browser that isn't signed in yet) and
settings-agents-signin-light (the sign-in listed beside a token, with Revoke),
closed-light, inspector-light, inspector-claude-{light,dark,phone-light} (Claude Code's
bubble with its name written under it, #62), inspector-device-light, inspector-dark-parked,
room-rename-before-{light,dark,phone-light}, room-rename-after-{light,dark,phone-light} (#316),
phone-light, phone-dark-sheet, offline-light, offline-dark, offline-phone-dark (#129),
welcome-light and login-light, plus, from a hosted broker
(``tests/ui_world.py``'s ``HostedWorld``, with Chromium's virtual authenticators as the
passkeys; issues #41 and #61): signin-setup-light, setup-light, people-light, people-dark,
passkeys-light, passkeys-dark, people-opening-{light,dark,phone-light} (initial People fetch held),
common-password-{light,dark,phone-light} (a common chosen password refused, #181),
mention-people-{popover,badge,line}-{light,dark,phone-light} (the @ popover listing people after
the agents, bob's @alice as a red count on a room's tab, and as a mention line, #418),
people-name-clash-{light,dark,phone-light} (an agent holds the requested person's name),
people-edit-{light,dark,phone-light} (Copy's fade timer while an email is selected),
proof-before-{light,dark,phone-light} and proof-after-{light,dark,phone-light} (a contested
review finding before its fix, then a fix's proof waiting for confirmation, #251),
handoff-blocked-, handoff-ready-, handoff-dialog- and handoff-after-{light,dark,phone-light}
(Hand off in the board's header locked with what is left, then ready, then the one review
summary and Posted by choice, then the selected agent's handoff, #252 and #425),
outdated-before-{light,dark,phone-light} and outdated-after-{light,dark,phone-light}
(a board's unresolved findings before and after an agent moves its head, #291),
board-tabs-{light,dark,phone-light} (a room with two open boards: tabs on the header's first row,
the second on show, #445),
gitlab-lines-{light,dark,phone-light} (a GitLab finding's Lines row: its lines at the head commit and
its file in the MR diff, #442),
icon-theme-dark-on-light, icon-theme-light-on-dark and icon-theme-dark-on-light-phone (Codex's and
Cursor's icons with the saved Theme against the system's scheme, #443),
check-waiting-{light,dark,phone-light} and check-confirmed-{light,dark,phone-light} (a fix awaiting
its raiser has no check; once confirmed, its card gets a green one in the corner, #427),
approval-board-{before,after}-{light,dark,phone-light},
approval-inspector-{before,after}-{light,dark,phone-light} and approval-members-{before,after}-phone-light
(an agent a review card waits on, busy as every agent at a prompt looked before, then waiting for approval
because its person has a permission prompt open: the card, the Inspector and Members, #472),
confirm-light, signin-light, setup-person-light and
signin-phone-dark (all .png).

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
        # an infinite one (a working agent's pulsing dot, #407) never ends: the shot disables it
        "document.getAnimations().every(a => a.playState !== 'running'"
        " || a.effect.getComputedTiming().iterations === Infinity)))))",
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

        fresh(page, n_chat)
        inspect(page, "bench")
        shot(page, out, "inspector-device-light.png")
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


def shoot_claude_avatar(browser: Any, out: Path, world: Any) -> None:
    """Claude Code's avatar: switchboard's bubble everywhere, and on the Inspector's large one the
    name written under it, light, dark and the phone."""
    from playwright.sync_api import expect

    b, n_chat = world.broker, world.n_chat
    base = {"timezone_id": "UTC", "color_scheme": "light", "locale": "en-US"}
    desk = browser.new_context(**base, **DESKTOP)
    desk.set_default_timeout(WAIT_MS)
    page = desk.new_page()
    try:
        sign_in(page, b)
        open_build(page, n_chat)
        inspect(page, "claude-1")
        shot(page, out, "inspector-claude-light.png")
        page.emulate_media(color_scheme="dark")
        fresh(page, n_chat)
        inspect(page, "claude-1")
        shot(page, out, "inspector-claude-dark.png")
        phone_ctx = browser.new_context(**base, **PHONE, storage_state=desk.storage_state())
        phone_ctx.set_default_timeout(WAIT_MS)
        try:
            ph = phone_ctx.new_page()
            ph.goto(b.base + "/")
            connected(ph)
            open_build(ph, n_chat)
            ph.click("#buddy-toggle")
            expect(ph.locator("#app")).to_have_class(re.compile(r"\bsheet-open\b"))
            inspect(ph, "claude-1")
            shot(ph, out, "inspector-claude-phone-light.png")
        finally:
            phone_ctx.close()
    finally:
        desk.close()


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


def shoot_claims(browser: Any, out: Path, world: Any) -> None:
    """Outside-work claims (#293): Members before and after one agent claims a build."""
    from playwright.sync_api import expect

    room = "#ops-claims"
    world.create_room(room)
    world.add_agents(room, ("claude-1", "codex-1"))
    base = {"timezone_id": "UTC", "locale": "en-US"}
    for stage in ("before", "after"):
        if stage == "after":
            result = world.agent_call(room, "claude-1", "claim", key="build", ttl=3600)
            assert result["ok"], result
        for theme, ctx_args in (
            ("light", {**DESKTOP, "color_scheme": "light"}),
            ("dark", {**DESKTOP, "color_scheme": "dark"}),
            ("phone-light", {**PHONE, "color_scheme": "light"}),
        ):
            ctx = browser.new_context(**base, **ctx_args)
            ctx.set_default_timeout(WAIT_MS)
            try:
                page = ctx.new_page()
                sign_in(page, world.broker)
                tab = page.locator(f'#tabs .room[data-room="{room}"]')
                if not tab.is_visible():
                    page.click("#rooms-toggle")
                tab.click()
                if theme == "phone-light":
                    page.click("#buddy-toggle")
                mark = page.locator('#buddy-list .member[data-name="claude-1"] .m-claim-entry')
                if stage == "after":
                    expect(mark).to_contain_text("Claim: build · Expires")
                else:
                    expect(mark).to_have_count(0)
                settle(page)
                shot(page, out, f"claims-{stage}-{theme}.png")
            finally:
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


REVIEW_CARD_DETAIL = (
    "## Why it matters\n\n"
    "- `total()` adds the tax **before** it takes the discount off\n"
    "- the receipt shows the other order, so the two disagree by $1.60\n\n"
    "```python\nreturn tax(price) - discount\n```\n\n"
    "```mermaid\nflowchart LR\n  A[cart] --> B[tax] --> C[discount] --> D[total]\n```"
)


def shoot_review_card(browser: Any, out: Path, world: Any) -> None:
    """#423: a card whose detail is Markdown with a drawn Mermaid diagram, and a question whose
    options say who recommends them: one agent's pick, then every agent's, then a split."""
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "locale": "en-US"}
    views = (
        ("light", {**DESKTOP, "color_scheme": "light"}),
        ("dark", {**DESKTOP, "color_scheme": "dark"}),
        ("phone-light", {**PHONE, "color_scheme": "light"}),
    )
    for slug, ctx_args in views:
        room = "#card-md-" + slug
        world.create_room(room)
        world.add_agents(room, ("claude-1", "codex-1"))
        call = world.agent_call
        call(
            room,
            "codex-1",
            "review",
            action="open",
            url="https://example.com/shop/pull/7",
            head="4f2c9e1a7b30",
        )
        call(room, "codex-1", "review", action="raise", title="Discount is taken after tax",
             where="shop/cart.py:40-58", detail=REVIEW_CARD_DETAIL)  # fmt: skip
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="' + room + '"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            page.click("#board-toggle")
            page.click('#board .board-card[data-item="F1"]')
            box = page.locator("#board .board-detail .md-pre").nth(1)
            box.locator("button.md-show-diagram").click()
            expect(box).to_have_class(re.compile(r"\bmd-showing-diagram\b"))
            if slug == "phone-light":
                page.locator("#board .board-detail").scroll_into_view_if_needed()
            settle(page)
            shot(page, out, "review-card-markdown-" + slug + ".png")
        finally:
            ctx.close()

    for slug, ctx_args in views:
        room = "#options-" + slug
        world.create_room(room)
        world.add_agents(room, ("claude-1", "codex-1"))
        world.seed_review(room)
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="' + room + '"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            page.click("#board-toggle")
            page.click('#board .board-card[data-item="Q1"]')
            buttons = page.locator("#board .board-detail button[data-option]")
            expect(buttons.nth(0)).to_have_text("Before tax (claude-1 recommends this)")
            if slug == "light":
                shot(page, out, "review-options-one-pick-light.png")
            world.agent_call(room, "codex-1", "review", action="concur", item="Q1", recommend=0)
            expect(buttons.nth(0)).to_have_text("Before tax (recommended by claude-1 and codex-1)")
            if slug == "phone-light":
                page.locator("#board .board-detail").scroll_into_view_if_needed()
            shot(page, out, "review-options-agreed-" + slug + ".png")
            if slug == "light":
                world.agent_call(room, "codex-1", "review", action="concur", item="Q1", recommend=1)
                expect(buttons.nth(1)).to_have_text("After tax (picked by codex-1)")
                shot(page, out, "review-options-split-light.png")
        finally:
            ctx.close()


def shoot_contest_flow(browser: Any, out: Path, world: Any) -> None:
    """#424: a contested finding waits in Agents working on its raiser, then, once the raiser insists,
    comes to Needs you with both reasons beside each other."""
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "locale": "en-US"}
    for slug, ctx_args in (
        ("light", {**DESKTOP, "color_scheme": "light"}),
        ("dark", {**DESKTOP, "color_scheme": "dark"}),
        ("phone-light", {**PHONE, "color_scheme": "light"}),
    ):
        room = "#contest-" + slug
        world.create_room(room)
        world.add_agents(room, ("claude-1", "codex-1"))
        call = world.agent_call
        call(
            room,
            "codex-1",
            "review",
            action="open",
            url="https://example.com/shop/pull/7",
            head="4f2c9e1a7b30",
        )
        call(room, "codex-1", "review", action="raise", title="Rounding drops a cent on $19.99",
             where="shop/money.py:8", detail="Half-up rounding, but the ledger rounds half-even.")  # fmt: skip
        call(room, "claude-1", "review", action="contest", item="F1",
             reason="the ledger is the one to change: half-up is what the receipt shows")  # fmt: skip
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="' + room + '"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            page.click("#board-toggle")
            page.click('#board .board-card[data-item="F1"]')
            expect(page.locator('#board .lane-working .board-card[data-item="F1"]')).to_be_visible()
            if slug == "phone-light":
                page.locator("#board .board-detail").scroll_into_view_if_needed()
            shot(page, out, "contest-waiting-" + slug + ".png")
            call(room, "codex-1", "review", action="insist", item="F1",
                 reason="the ledger is the books: the receipt has to match it")  # fmt: skip
            expect(page.locator('#board .lane-you .board-card[data-item="F1"]')).to_be_visible()
            expect(page.locator("#board .board-insist")).to_have_count(1)
            if slug == "phone-light":
                page.locator("#board .board-detail").scroll_into_view_if_needed()
            shot(page, out, "contest-insisted-" + slug + ".png")
        finally:
            ctx.close()


def shoot_board_tabs(browser: Any, out: Path, world: Any) -> None:
    """#445: a room with two open boards. Tabs on the header's first row name each pull request and
    count what needs a person on it; the second board is on show, with its own lanes."""
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "locale": "en-US"}
    for slug, size, scheme in (
        ("light", DESKTOP, "light"),
        ("dark", DESKTOP, "dark"),
        ("phone-light", PHONE, "light"),
    ):
        room = "#tabs-" + slug
        world.create_room(room)
        world.add_agents(room, ("claude-1", "codex-1"))
        world.seed_review(room, "https://github.com/example/shop/pull/7")
        world.seed_second_review(room)
        ctx = browser.new_context(**base, **size, color_scheme=scheme)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="' + room + '"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            page.click("#board-toggle")
            expect(page.locator("#board .board-tab")).to_have_count(2)
            page.locator("#board .board-tab").nth(1).click()
            page.click('#board .board-card[data-item="Q1"]')
            expect(page.locator("#board .board-detail")).to_be_visible()
            settle(page)
            shot(page, out, "board-tabs-" + slug + ".png")
        finally:
            ctx.close()


def shoot_gitlab_lines(browser: Any, out: Path, world: Any) -> None:
    """#442: a GitLab finding's Lines row, on a merge request: the lines at the board's head commit and
    the file in the MR diff, each labelled for what it opens. The fixed finding's card, in light, dark
    and on a phone."""
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "locale": "en-US"}
    for slug, size, scheme in (
        ("light", DESKTOP, "light"),
        ("dark", DESKTOP, "dark"),
        ("phone-light", PHONE, "light"),
    ):
        room = "#gitlab-lines-" + slug
        world.create_room(room)
        world.add_agents(room, ("claude-1", "codex-1"))
        world.seed_review(room, "https://gitlab.com/example-group/shop/-/merge_requests/12")
        ctx = browser.new_context(**base, **size, color_scheme=scheme)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="' + room + '"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            page.click("#board-toggle")
            page.click('#board .board-card[data-item="F2"]')
            detail = page.locator("#board .board-detail")
            expect(detail.locator("dd a.board-link").first).to_be_visible()
            if size is PHONE:
                detail.scroll_into_view_if_needed()
            settle(page)
            shot(page, out, "gitlab-lines-" + slug + ".png")
        finally:
            ctx.close()


def shoot_icon_theme(browser: Any, out: Path, world: Any) -> None:
    """#443: Codex's and Cursor's icons follow the saved Theme, not only the system's scheme. The
    welcome page's harness cards, with Theme set to Dark on a light system, Light on a dark one, and
    Dark on a light phone. (The page's data-theme is what Settings saves.)"""
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "locale": "en-US"}
    for name, size, scheme, theme in (
        ("icon-theme-dark-on-light.png", DESKTOP, "light", "dark"),
        ("icon-theme-light-on-dark.png", DESKTOP, "dark", "light"),
        ("icon-theme-dark-on-light-phone.png", PHONE, "light", "dark"),
    ):
        ctx = browser.new_context(**base, **size, color_scheme=scheme)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.empty)
            expect(page.locator("#empty")).to_be_visible()
            page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)
            cursor = page.locator(".harness-head .avatar.h-cursor")
            cursor.scroll_into_view_if_needed()
            expect(cursor).to_be_visible()
            settle(page)
            shot(page, out, name)
        finally:
            ctx.close()


def shoot_outdated_board(browser: Any, out: Path, world: Any) -> None:
    """#291: the same open finding before and after an agent moves the board's head."""
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "locale": "en-US"}
    for slug, size, scheme in (
        ("light", DESKTOP, "light"),
        ("dark", DESKTOP, "dark"),
        ("phone-light", PHONE, "light"),
    ):
        room = "#outdated-" + slug
        world.create_room(room)
        world.add_agents(room, ("claude-1", "codex-1"))
        world.seed_review(room)
        ctx = browser.new_context(**base, **size, color_scheme=scheme)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="' + room + '"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            page.click("#board-toggle")
            page.click('#board .board-card[data-item="F3"]')
            detail = page.locator("#board .board-detail")
            expect(detail).to_be_visible()
            if size is PHONE:
                detail.scroll_into_view_if_needed()
            settle(page)
            shot(page, out, "outdated-before-" + slug + ".png")
            moved = world.agent_call(
                room,
                "codex-1",
                "review",
                action="open",
                url="https://example.com/shop/pull/7",
                head="7b9c2a4d6e8f",
            )
            assert moved["ok"], moved
            expect(detail.locator(".board-outdated")).to_have_text("Outdated at 7b9c2a4")
            if size is PHONE:
                detail.scroll_into_view_if_needed()
            settle(page)
            shot(page, out, "outdated-after-" + slug + ".png")
        finally:
            ctx.close()


def shoot_proof_board(browser: Any, out: Path, world: Any) -> None:
    """#251: a contested card before the fix, then the proof awaiting the raiser's check."""
    from playwright.sync_api import expect

    for slug, ctx_args in (
        ("light", {**DESKTOP, "color_scheme": "light"}),
        ("dark", {**DESKTOP, "color_scheme": "dark"}),
        ("phone-light", {**PHONE, "color_scheme": "light"}),
    ):
        room = "#proof-" + slug
        world.create_room(room)
        world.add_agents(room, ("claude-1", "codex-1"))
        world.seed_review(room)
        ctx = browser.new_context(timezone_id="UTC", locale="en-US", **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="' + room + '"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            page.click("#board-toggle")
            page.click('#board .board-card[data-item="F3"]')
            expect(page.locator("#board .board-side")).to_have_count(2)
            if slug == "phone-light":
                page.locator("#board .board-detail").scroll_into_view_if_needed()
            settle(page)
            shot(page, out, "proof-before-" + slug + ".png")
            world.agent_call(room, "claude-1", "review", action="concede", item="F3")
            world.agent_call(
                room,
                "claude-1",
                "review",
                action="fix",
                item="F3",
                commit="abc1234",
                proof="tests/test_money.py::test_rounding",
            )
            expect(page.locator('#board .lane-working .board-card[data-item="F3"]')).to_be_visible()
            expect(page.locator("#board .board-detail .board-rows")).to_contain_text("Proof")
            if slug == "phone-light":
                page.locator("#board .board-detail").scroll_into_view_if_needed()
            settle(page)
            shot(page, out, "proof-after-" + slug + ".png")
        finally:
            ctx.close()


def center(page: Any, selector: str) -> None:
    """Scroll ``selector`` to the middle of the page, as ``reveal`` does for the nearest edge: one
    evaluate, so a redraw of the board between finding the card and scrolling can't fail it."""
    page.evaluate(
        "s => { const e = document.querySelector(s); if (e) e.scrollIntoView({ block: 'center' }); }",
        selector,
    )


def shoot_confirmed_check(browser: Any, out: Path, world: Any) -> None:
    """#427: a fix still waiting for its raiser has no check; once the raiser confirms it, its
    card gets a green one in the corner. On a phone the lanes stack, so the shot scrolls the card
    into view. It waits for the card to move, never for the check: the same script shoots the
    board before the change."""
    from playwright.sync_api import expect

    for slug, ctx_args in (
        ("light", {**DESKTOP, "color_scheme": "light"}),
        ("dark", {**DESKTOP, "color_scheme": "dark"}),
        ("phone-light", {**PHONE, "color_scheme": "light"}),
    ):
        room = "#check-" + slug
        world.create_room(room)
        world.add_agents(room, ("claude-1", "codex-1"))
        world.seed_review(room)
        call = world.agent_call
        call(room, "claude-1", "review", action="concede", item="F3")
        call(room, "claude-1", "review", action="fix", item="F3", commit="abc1234",
             proof="tests/test_money.py::test_rounding")  # fmt: skip
        ctx = browser.new_context(timezone_id="UTC", locale="en-US", **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="' + room + '"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            page.click("#board-toggle")
            waiting = page.locator('#board .lane-working .board-card[data-item="F3"]')
            expect(waiting).to_contain_text("Fixed")
            if slug == "phone-light":
                center(page, '#board .lane-working .board-card[data-item="F3"]')
            shot(page, out, "check-waiting-" + slug + ".png")
            call(room, "codex-1", "review", action="confirm", item="F3")
            confirmed = page.locator('#board .lane-settled .board-card[data-item="F3"]')
            expect(confirmed).to_contain_text("confirmed by codex-1")
            if slug == "phone-light":
                center(page, '#board .lane-settled .board-card[data-item="F3"]')
            shot(page, out, "check-confirmed-" + slug + ".png")
        finally:
            ctx.close()


def reveal(page: Any, selector: str) -> None:
    """Scroll ``selector`` into view if it isn't. Not Locator.scroll_into_view_if_needed: the
    board is redrawn by every live frame, and that call fails if the element is replaced while
    it waits for it to be stable."""
    page.evaluate(
        "s => { const e = document.querySelector(s); if (e) e.scrollIntoView({ block: 'nearest' }); }",
        selector,
    )


def shoot_handoff_board(browser: Any, out: Path, world: Any) -> None:
    """#252, #425: Hand off in the board's header, in its states: locked with what is left,
    ready, the exact one-review plan in the dialog, then the chosen agent's handoff. Each shot
    scrolls Hand off into view first, which costs nothing where it is already on screen (the
    header, with the phone's sticky one) and shows where a person had to go before #425, when
    it was a card in the Settled lane."""
    from playwright.sync_api import expect

    for slug, ctx_args in (
        ("light", {**DESKTOP, "color_scheme": "light"}),
        ("dark", {**DESKTOP, "color_scheme": "dark"}),
        ("phone-light", {**PHONE, "color_scheme": "light"}),
    ):
        room = "#handoff-" + slug
        world.create_room(room)
        world.add_agents(room, ("claude-1", "codex-1"))
        call = world.agent_call
        call(room, "codex-1", "review", action="open", url="https://example.com/shop/pull/7")
        call(room, "codex-1", "review", action="raise", title="Tax is applied before discount",
             where="shop/cart.py:40-58", detail="The receipt uses the discounted total.")  # fmt: skip
        call(
            room, "claude-1", "review", action="contest", item="F1", reason="The old order matched the ledger"
        )
        call(room, "claude-1", "review", action="ask", title="Discount before or after tax?",
             options=["Before tax", "After tax"], recommend=0)  # fmt: skip
        ctx = browser.new_context(timezone_id="UTC", locale="en-US", **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="' + room + '"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            page.click("#board-toggle")
            handoff = page.locator("#board-handoff")
            expect(handoff).to_be_disabled()
            reveal(page, "#board-handoff")
            shot(page, out, "handoff-blocked-" + slug + ".png")
            page.click('#board .board-card[data-item="F1"]')
            page.select_option("#board-owner", "claude-1")
            page.click("#board-concede")
            expect(page.locator('#board .board-card[data-item="F1"]')).to_contain_text("Being fixed")
            call(room, "claude-1", "review", action="fix", item="F1", commit="abc1234",
                 proof="tests/test_cart.py::test_tax_order")  # fmt: skip
            call(room, "codex-1", "review", action="confirm", item="F1")
            expect(page.locator('#board .lane-settled .board-card[data-item="F1"]')).to_contain_text(
                "confirmed by codex-1"
            )
            page.click('#board .board-card[data-item="Q1"]')
            page.locator('#board .board-detail button[data-option="0"]').click()
            expect(handoff).to_be_enabled()
            reveal(page, "#board-handoff")
            shot(page, out, "handoff-ready-" + slug + ".png")
            handoff.click()
            expect(page.locator("#app-dialog .handoff-comment")).to_contain_text("shop/cart.py:58")
            if slug == "phone-light":
                page.locator("#app-dialog-agent").scroll_into_view_if_needed()
            shot(page, out, "handoff-dialog-" + slug + ".png")
            page.click("#app-dialog-action")
            expect(page.locator("#board-status")).to_have_text("Handed off to codex-1")
            reveal(page, "#board-handoff-status")
            shot(page, out, "handoff-after-" + slug + ".png")
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


def shoot_room_rename(browser: Any, out: Path, world: Any) -> None:
    """The small title dialog and its result (#316), before and after in light, dark and phone."""
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "locale": "en-US"}
    for suffix, ctx_args in (
        ("light", {**DESKTOP, "color_scheme": "light"}),
        ("dark", {**DESKTOP, "color_scheme": "dark"}),
        ("phone-light", {**PHONE, "color_scheme": "light"}),
    ):
        old, new = "#sprint-" + suffix + "-typo", "#sprint-" + suffix
        world.create_room(old)
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="' + old + '"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            page.click("#room-title-button")
            expect(page.locator("#app-dialog-name")).to_be_focused()
            page.fill("#app-dialog-name", new)
            shot(page, out, "room-rename-before-" + suffix + ".png")
            page.click("#app-dialog-action")
            expect(page.locator("#room-title")).to_have_text(new[1:])
            expect(page.locator("#log")).to_contain_text("renamed " + old + " to " + new)
            shot(page, out, "room-rename-after-" + suffix + ".png")
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
        page.fill("#settings-room-rules", "A draft for the next room.")
        shot(page, out, "settings-phone-rooms-light.png")
    finally:
        ctx.close()


# ----------------------------------------------------- customizing the agents' avatars (#498)
def badge(top: tuple[int, int, int], bottom: tuple[int, int, int], shape: str) -> bytes:
    """A 128 px PNG drawn here for the pictures: a vertical gradient with a white ring, dot or bars on
    it. Not a logo of anyone's: it is what an admin might upload."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    size, rows = 128, []
    for y in range(size):
        base = tuple(round(top[i] + (bottom[i] - top[i]) * y / (size - 1)) for i in range(3))
        row = bytearray([0])
        for x in range(size):
            dx, dy = x - 63.5, y - 63.5
            d = (dx * dx + dy * dy) ** 0.5
            inked = (
                (shape == "ring" and 26 <= d <= 38)
                or (shape == "dot" and d <= 24)
                or (shape == "bars" and abs(dx) <= 32 and 24 <= y <= 104 and (y // 16) % 2 == 0)
            )
            row += bytes((255, 255, 255) if inked else base) + b"\xff"
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows)))
    return png + chunk(b"IEND", b"")


# what the admin uploads in the pictures: a light and a dark image for Claude Code and Codex, and
# one image for Devin (used for both schemes); Cursor keeps its built-in avatar
UPLOADS = {
    ("claude", "light"): ((233, 150, 110), (198, 90, 60), "ring"),
    ("claude", "dark"): ((120, 60, 44), (70, 34, 26), "ring"),
    ("codex", "light"): ((120, 200, 190), (40, 140, 130), "dot"),
    ("codex", "dark"): ((26, 70, 74), (14, 38, 42), "dot"),
    ("devin", "light"): ((130, 130, 230), (70, 70, 180), "bars"),
}


def shoot_agent_icons(browser: Any, out: Path, world: Any) -> None:
    """#498: the same Members list and log with the built-in avatars and with uploaded ones (light,
    dark, the phone's Members sheet), and Settings → Appearance → Agent icons (light, dark, the phone's
    page), with its info tip open by keyboard. The uploads go in through the API, as the page's file
    chooser would send them, and are reset after, so the world's other shots are untouched."""
    from playwright.sync_api import expect

    b, n_chat, web = world.broker, world.n_chat, world.web
    base = {"timezone_id": "UTC", "locale": "en-US"}

    def put(harness: str, scheme: str, args: tuple[Any, Any, str]) -> None:
        headers = {"Origin": b.origin, "X-Switchboard": "1", "Content-Type": "image/png"}
        r = web.put(f"/api/agent-icons/{harness}/{scheme}", content=badge(*args), headers=headers)
        assert r.status_code == 200, r.text

    def members(page: Any, name: str, phone: bool) -> None:
        open_build(page, n_chat)
        if phone:
            page.click("#buddy-toggle")
            expect(page.locator("#app")).to_have_class(re.compile(r"\bsheet-open\b"))
        page.locator('#log .line[data-from="codex-1"]').first.scroll_into_view_if_needed()
        shot(page, out, name)

    def pass_over(kind: str) -> None:
        for size, scheme, tag in (
            (DESKTOP, "light", "light"),
            (DESKTOP, "dark", "dark"),
            (PHONE, "light", "phone-light"),
        ):
            ctx = browser.new_context(**base, **size, color_scheme=scheme)
            ctx.set_default_timeout(WAIT_MS)
            try:
                page = ctx.new_page()
                sign_in(page, b)
                members(page, f"agent-icons-{kind}-{tag}.png", size is PHONE)
            finally:
                ctx.close()

    def settings_page(name: str, size: dict[str, Any], scheme: str, tip: bool = False) -> None:
        ctx = browser.new_context(**base, **size, color_scheme=scheme)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, b)
            if size is PHONE:
                page.click("#rooms-toggle")
            page.click("#me-settings")
            expect(page.locator("#app-dialog")).to_be_visible()
            page.click("#settings-row-appearance" if size is PHONE else "#settings-tab-appearance")
            expect(page.locator(".agent-icon-row")).to_have_count(4)
            expect(page.locator('.agent-icon-row[data-harness="codex"]')).to_contain_text(
                "Your light and dark images"
            )
            title = page.locator("#agent-icons-title")
            title.scroll_into_view_if_needed()
            if tip:
                page.focus("#settings-text-larger")
                page.keyboard.press("Tab")
                expect(page.locator("#agent-icons-tip")).to_be_visible()
            else:
                page.evaluate("document.activeElement && document.activeElement.blur()")
            shot(page, out, name)
        finally:
            ctx.close()

    try:
        pass_over("before")
        for slot, args in UPLOADS.items():
            put(*slot, args)
        pass_over("after")
        settings_page("agent-icons-settings-light.png", DESKTOP, "light")
        settings_page("agent-icons-settings-dark.png", DESKTOP, "dark")
        settings_page("agent-icons-settings-phone-light.png", PHONE, "light")
        settings_page("agent-icons-tip-light.png", DESKTOP, "light", tip=True)
    finally:
        for harness in ("claude", "codex", "devin"):
            web.delete(f"/api/agent-icons/{harness}", headers=b.write_headers())


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


def shoot_settings_agents(browser: Any, out: Path, world: Any) -> None:
    """Settings → Agents (#382): the empty group, a token just made (shown once, beside the device
    it's for), and Revoke asking on its row, on a desktop (light, dark) and a phone (light). The
    tokens are the throwaway broker's own, dead with its temp home."""
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "locale": "en-US"}

    def made(page: Any, device: str) -> None:
        page.fill("#agent-token-device", device)
        page.click("#agent-token-make")
        expect(page.locator("#agent-token-value")).to_be_visible()

    def revoke_all() -> None:
        st = world.broker.state
        for t in world.broker.on_loop(lambda: st.store.agent_tokens_of(None)):
            world.broker.on_loop(lambda t=t: st.store.agent_token_revoke(t.id, None))

    for suffix, ctx_args in (
        ("light", {**DESKTOP, "color_scheme": "light"}),
        ("dark", {**DESKTOP, "color_scheme": "dark"}),
    ):
        revoke_all()
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            page.click("#me-settings")
            page.click("#settings-tab-agents")
            expect(page.locator("#settings-panel-agents .passkey-count")).to_have_text("none yet")
            page.evaluate("document.activeElement && document.activeElement.blur()")
            shot(page, out, "settings-agents-empty-" + suffix + ".png")
            made(page, "build-box")
            made(page, "work-laptop")
            page.evaluate("document.activeElement && document.activeElement.blur()")
            shot(page, out, "settings-agents-made-" + suffix + ".png")
            page.locator("#agent-tokens .agent-token", has_text="build-box").locator("button").click()
            shot(page, out, "settings-agents-revoke-" + suffix + ".png")
        finally:
            ctx.close()

    revoke_all()
    ctx = browser.new_context(**base, **PHONE, color_scheme="light")
    ctx.set_default_timeout(WAIT_MS)
    try:
        page = ctx.new_page()
        sign_in(page, world.broker)
        page.click("#rooms-toggle")
        page.click("#me-settings")
        page.click("#settings-row-agents")
        made(page, "work-laptop")
        page.evaluate("document.activeElement && document.activeElement.blur()")
        shot(page, out, "settings-agents-phone-light.png")
    finally:
        ctx.close()
    revoke_all()


def shoot_connect_codex(browser: Any, out: Path, world: Any) -> None:
    """#472: Settings → Agents, the Codex step of Connect an agent, in light, dark and on a phone: the
    plugin, the ``config.toml`` entry, and the environment lines. The plugin's approval hook is a command, so
    the step also gives it the server's address (``SWITCHBOARD_URL``) beside the token. It waits for the
    step's last line, never for its words, so the same script shoots the page before the change."""
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "locale": "en-US"}

    def revoke_all() -> None:
        st = world.broker.state
        for t in world.broker.on_loop(lambda: st.store.agent_tokens_of(None)):
            world.broker.on_loop(lambda t=t: st.store.agent_token_revoke(t.id, None))

    for suffix, ctx_args in (
        ("light", {**DESKTOP, "color_scheme": "light"}),
        ("dark", {**DESKTOP, "color_scheme": "dark"}),
        ("phone-light", {**PHONE, "color_scheme": "light"}),
    ):
        revoke_all()
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            if suffix.startswith("phone"):
                page.click("#rooms-toggle")
                page.click("#me-settings")
                page.click("#settings-row-agents")
            else:
                page.click("#me-settings")
                page.click("#settings-tab-agents")
            page.fill("#agent-token-device", "work-laptop")
            page.click("#agent-token-make")
            expect(page.locator("#agent-token-value")).to_be_visible()
            expect(page.locator("#agent-connect-codex-env")).to_be_visible()
            step = page.locator("#agent-connect .agent-connect-step").nth(1)
            step.scroll_into_view_if_needed()
            page.evaluate("document.activeElement && document.activeElement.blur()")
            settle(page)
            path = out / f"connect-codex-{suffix}.png"
            step.screenshot(path=str(path), animations="disabled", caret="hide")
            print(path)
        finally:
            ctx.close()
    revoke_all()


# ---------------------------------------------------------- the consent page (#386)
CONSENT_REDIRECT = "http://localhost:41234/callback"


class SignInAgent:
    """An agent that signs in through the browser (#386, DESIGN.md §49.9): it registers the way Claude
    Code does and builds the address it sends its person's browser to. Its loopback address is a route
    the page's navigation is answered by (``answer_loopback``), so nothing listens on a port."""

    def __init__(self, origin: str, name: str = "Claude Code (switchboard)") -> None:
        import secrets

        import httpx

        from switchboard.broker import oauth

        self.origin, self.s256 = origin, oauth.s256
        self.verifier = secrets.token_urlsafe(32)
        self.state = secrets.token_urlsafe(12)
        r = httpx.post(
            origin + "/oauth/register",
            json={
                "client_name": name,
                "redirect_uris": [CONSENT_REDIRECT],
                "token_endpoint_auth_method": "none",
            },
            timeout=10.0,
        )
        r.raise_for_status()
        self.client_id = r.json()["client_id"]

    def url(self) -> str:
        import urllib.parse

        q = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "code_challenge": self.s256(self.verifier),
                "code_challenge_method": "S256",
                "redirect_uri": CONSENT_REDIRECT,
                "state": self.state,
                "resource": self.origin + "/mcp",
            }
        )
        return f"{self.origin}/oauth/authorize?{q}"

    def exchange(self, answered: str) -> None:
        import urllib.parse

        import httpx

        code = urllib.parse.parse_qs(urllib.parse.urlsplit(answered).query)["code"][0]
        r = httpx.post(
            self.origin + "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": self.verifier,
                "redirect_uri": CONSENT_REDIRECT,
                "client_id": self.client_id,
            },
            timeout=10.0,
        )
        r.raise_for_status()


def answer_loopback(ctx: Any, got: list[str]) -> None:
    """The agent's own address, answered by the context: the page's navigation lands here, and the address
    (with the code) is kept for the agent to swap."""

    def handle(route: Any) -> None:
        got.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body="<p>The agent has its answer.</p>")

    ctx.route("http://localhost:41234/**", handle)


def shoot_oauth_consent(browser: Any, out: Path, world: Any, hosted: Any) -> None:
    """The consent page of an agent that signs in through the browser (#386): the card on a desktop
    broker (light, dark) and a phone, the sign-in step of a browser with no session, the same card on a
    hosted broker whose person must confirm it's them first, and the sign-in listed in Settings →
    Agents beside a token. The agent and its address are made up for the pictures."""
    import httpx
    from playwright.sync_api import expect

    base = {"timezone_id": "UTC", "locale": "en-US", "reduced_motion": "reduce"}
    b = world.broker

    def revoke_all(broker: Any) -> None:
        for t in broker.on_loop(lambda: broker.state.store.agent_tokens_of(None)):
            broker.on_loop(lambda t=t: broker.state.store.agent_token_revoke(t.id, None))

    revoke_all(b)
    agent = SignInAgent(b.origin)
    # signed out first: the page asks the browser to sign in
    ctx = browser.new_context(**base, **DESKTOP, color_scheme="light")
    ctx.set_default_timeout(WAIT_MS)
    try:
        page = ctx.new_page()
        page.goto(agent.url())
        expect(page.locator("#step-signin")).to_be_visible()
        expect(page.locator("#signin-desktop")).to_be_visible()
        shot(page, out, "oauth-signin-light.png")
    finally:
        ctx.close()

    for suffix, ctx_args in (
        ("light", {**DESKTOP, "color_scheme": "light"}),
        ("dark", {**DESKTOP, "color_scheme": "dark"}),
        ("phone-light", {**PHONE, "color_scheme": "light"}),
    ):
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, b)
            page.goto(agent.url())
            expect(page.locator("#step-consent")).to_be_visible()
            page.fill("#consent-device", "work-laptop")
            page.evaluate("document.activeElement && document.activeElement.blur()")
            shot(page, out, f"oauth-consent-{suffix}.png")
            if suffix != "light":
                continue
            # approve it, so Settings → Agents has the sign-in to list
            got: list[str] = []
            answer_loopback(ctx, got)
            page.click("#consent-approve")
            expect(page).to_have_url(re.compile(re.escape(CONSENT_REDIRECT) + r"\?"))
            agent.exchange(got[0])
            app = ctx.new_page()
            app.goto(b.origin + "/")
            connected(app)
            app.click("#me-settings")
            app.click("#settings-tab-agents")
            expect(app.locator("#agent-tokens .agent-token")).to_have_count(1)
            app.evaluate("document.activeElement && document.activeElement.blur()")
            shot(app, out, "settings-agents-signin-light.png")
        finally:
            ctx.close()
    revoke_all(b)

    # a hosted broker, whose person's last check ran out: Confirm it's you comes first
    origin = hosted.origin
    one_time = hosted.claim_link().split("#t=", 1)[1]
    hdr = {"Origin": origin, "X-Switchboard": "1"}
    with httpx.Client(base_url=origin, headers=hdr, timeout=10.0) as c:
        c.post("/api/signin/password", json={"name": "admin", "password": one_time}).raise_for_status()
        c.post("/api/setup/password", json={"password": "correct horse battery"}).raise_for_status()
        cookie = c.cookies.get("switchboard_session")
    hosted.broker.on_loop(lambda: hosted.broker.state.passkey_checks.clear())
    agent = SignInAgent(origin)
    ctx = browser.new_context(**base, **DESKTOP, color_scheme="light")
    ctx.set_default_timeout(WAIT_MS)
    try:
        ctx.add_cookies([{"name": "switchboard_session", "value": cookie, "url": origin}])
        page = ctx.new_page()
        page.goto(agent.url())
        expect(page.locator("#consent-confirm")).to_be_visible()
        page.fill("#consent-device", "work-laptop")
        page.evaluate("document.activeElement && document.activeElement.blur()")
        shot(page, out, "oauth-confirm-light.png")
    finally:
        ctx.close()


def shoot_hosted(browser: Any, out: Path, world: Any) -> None:
    """A hosted broker (issues #41, #61): the sign-in page before it's set up, Choose how you'll
    sign in with the admin's email, the People sheet with an invite to send (light, dark), Settings'
    Profile and Sign-in & security groups (light, dark, #232), Confirm it's you, the sign-in page's
    three ways in, a teammate's own Choose page, and the sign-in page on a phone (dark). The
    invite's address reads https://sb.example.com."""
    from playwright.sync_api import expect
    from ui_world import wait_js

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
        # A real agent holds robert in #build. Adding a person under that name shows the
        # broker's refusal (#180); the before shot instead shows the unintended invite.
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        from fakes.fake_agent import FakeAgent

        async def agent_holds_robert() -> None:
            async with FakeAgent(world.broker.home, "shots-name-collision") as agent:
                reply = await agent.join("#build", "robert")
                assert reply["ok"], reply

        world.set_test_mode(True)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(lambda: asyncio.run(agent_holds_robert())).result()
        world.set_test_mode(False)
        page.fill("#person-first", "Bob")
        page.fill("#person-last", "Builder")
        page.fill("#person-email", "bob2@example.com")
        page.fill("#person-name", "robert")
        page.click("#person-add")
        wait_js(
            page,
            """() => !!document.querySelector('#invite-done') ||
          !!document.querySelector('#people-result-add')?.textContent.trim()""",
        )
        if page.locator("#invite-done").is_visible():
            page.click("#invite-done")  # never put a one-time password in a preview
        as_deployed(page)
        shot(page, out, "people-name-clash-light.png")
        page.emulate_media(color_scheme="dark")
        shot(page, out, "people-name-clash-dark.png")
        page.emulate_media(color_scheme="light")
        page.set_viewport_size(PHONE["viewport"])
        shot(page, out, "people-name-clash-phone-light.png")
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
# the address a real deployment shows, in the pictures of the invite text
AS_DEPLOYED = "https://sb.example.com"

PEOPLE_AS_DEPLOYED_JS = """([from, to]) => {
  const w = document.createTreeWalker(document.getElementById('people-body'), NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) n.nodeValue = n.nodeValue.split(from).join(to);
}"""


def shoot_common_password(browser: Any, out: Path, world: Any) -> None:
    """A common chosen password is refused; on the old broker it completed setup (#181)."""
    from playwright.sync_api import expect

    ctx = browser.new_context(timezone_id="UTC", color_scheme="light", locale="en-US", **DESKTOP)
    ctx.set_default_timeout(WAIT_MS)
    try:
        page = ctx.new_page()
        page.goto(world.origin + "/")
        page.fill("#signin-password", world.claim_link().split("#t=", 1)[1])
        page.click("#password-btn")
        expect(page.locator("#step-choose")).to_be_visible()
        page.fill("#setup-first", "Alice")
        page.fill("#setup-last", "Liddell")
        page.fill("#setup-email", "alice@example.com")
        page.fill("#new-password", "password123")
        page.fill("#new-password-2", "password123")
        with page.expect_response(lambda r: r.url.endswith("/api/setup/password")) as result:
            page.click("#password-btn")
        if result.value.status == 200:
            connected(page)  # before: the weak password was accepted
        else:
            expect(page.locator("#setup-error")).to_contain_text("less common")
        shot(page, out, "common-password-light.png")
        page.emulate_media(color_scheme="dark")
        shot(page, out, "common-password-dark.png")
        page.emulate_media(color_scheme="light")
        page.set_viewport_size(PHONE["viewport"])
        shot(page, out, "common-password-phone-light.png")
    finally:
        ctx.close()


def shoot_mention_people(browser: Any, out: Path, world: Any) -> None:
    """Mentioning a person (#418), on a hosted broker: alice (the admin, whose page this is), bob
    (his page is open, so he's online), carol (not here) and one agent, claude-1, in #build. The
    @ popover lists the people after the agents; bob's @alice in #ops, a room alice isn't viewing,
    is a red count on its tab (the phone's Rooms toggle too), and a mention line once she opens
    it. Light, dark and the phone."""
    import asyncio
    import threading

    import httpx
    from fakes.fake_agent import FakeAgent
    from playwright.sync_api import expect

    origin = world.origin
    headers = {"Origin": origin, "X-Switchboard": "1"}
    admin_pw, bob_pw = "correct horse battery", "bob's own secret 1"
    with httpx.Client(base_url=origin, headers=headers) as c:
        one_time = world.claim_link().split("#t=", 1)[1]
        assert c.post("/api/signin/password", json={"name": "admin", "password": one_time}).status_code == 200
        assert c.post("/api/setup/password", json={"password": admin_pw}).status_code == 200
        assert c.post("/api/rooms", json={"name": "#ops"}).status_code == 200
        bob_otp = c.post("/api/people", json={"name": "bob", "email": "bob@example.com"}).json()["password"]
        assert c.post("/api/people", json={"name": "carol", "email": "carol@example.com"}).status_code == 200
        admin_cookie = c.cookies.get("switchboard_session")
    with httpx.Client(base_url=origin, headers=headers) as c:
        signed = c.post("/api/signin/password", json={"name": "bob@example.com", "password": bob_otp})
        assert signed.status_code == 200, signed.text
        assert c.post("/api/me/password", json={"password": bob_pw}).status_code == 200
        bob_cookie = c.cookies.get("switchboard_session")
    assert admin_cookie and bob_cookie

    def say_as(cookie: str, room: str, text: str) -> None:
        with httpx.Client(
            base_url=origin, headers=headers, cookies={"switchboard_session": cookie}
        ) as client:
            assert client.post(f"/api/rooms/{room}/say", json={"text": text}).status_code == 200

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    def run_agent(coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(60)

    agent = FakeAgent(world.broker.home, "shots-mention-people")
    base = {"timezone_id": "UTC", "locale": "en-US"}

    def alice_context(**ctx_args: Any) -> Any:
        ctx = browser.new_context(**base, **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        ctx.add_cookies([{"name": "switchboard_session", "value": admin_cookie, "url": origin}])
        return ctx

    bob_ctx = browser.new_context(**base, **DESKTOP)
    bob_ctx.set_default_timeout(WAIT_MS)
    bob_ctx.add_cookies([{"name": "switchboard_session", "value": bob_cookie, "url": origin}])
    try:
        world.set_test_mode(True)  # a test agent joins only in test mode: off again before any page loads
        try:
            run_agent(agent.start())
            assert run_agent(agent.join("#build", "claude-1"))["ok"]
        finally:
            world.set_test_mode(False)
        bob = bob_ctx.new_page()
        bob.goto(origin + "/")
        connected(bob)  # his page is open: bob is online in alice's list

        for name, ctx_args in (
            ("mention-people-popover-light.png", {**DESKTOP, "color_scheme": "light"}),
            ("mention-people-popover-dark.png", {**DESKTOP, "color_scheme": "dark"}),
            ("mention-people-popover-phone-light.png", {**PHONE, "color_scheme": "light"}),
        ):
            ctx = alice_context(**ctx_args)
            try:
                page = ctx.new_page()
                page.goto(origin + "/")
                connected(page)
                tab = page.locator('#tabs .room[data-room="#build"]')
                if not tab.is_visible():
                    page.click("#rooms-toggle")
                tab.click()
                type_in(page, "@")
                expect(page.locator("#men-bob .m-status")).to_have_text("Online · Person")
                expect(page.locator("#men-carol .m-status")).to_have_text("Offline · Person")
                # the list scrolls: end on the people, under the sticky heads of both groups
                page.evaluate("document.getElementById('mentions').scrollTop = 1e6")
                shot(page, out, name)
            finally:
                ctx.close()

        say_as(bob_cookie, "ops", "@alice can you review the migration before we ship it tonight?")
        for name, ctx_args in (
            ("mention-people-badge-light.png", {**DESKTOP, "color_scheme": "light"}),
            ("mention-people-badge-dark.png", {**DESKTOP, "color_scheme": "dark"}),
            ("mention-people-badge-phone-light.png", {**PHONE, "color_scheme": "light"}),
        ):
            ctx = alice_context(**ctx_args)
            try:
                page = ctx.new_page()
                page.goto(origin + "/")
                connected(page)
                tab = page.locator('#tabs .room[data-room="#ops"]')
                if not tab.is_visible():
                    page.click("#rooms-toggle")
                expect(tab.locator(".badge")).to_have_text("1")
                shot(page, out, name)
            finally:
                ctx.close()

        for name, ctx_args in (
            ("mention-people-line-light.png", {**DESKTOP, "color_scheme": "light"}),
            ("mention-people-line-dark.png", {**DESKTOP, "color_scheme": "dark"}),
            ("mention-people-line-phone-light.png", {**PHONE, "color_scheme": "light"}),
        ):
            ctx = alice_context(**ctx_args)
            try:
                page = ctx.new_page()
                page.goto(origin + "/")
                connected(page)
                tab = page.locator('#tabs .room[data-room="#ops"]')
                if not tab.is_visible():
                    page.click("#rooms-toggle")
                tab.click()
                expect(page.locator("#log .line.mention")).to_have_count(1)
                shot(page, out, name)
            finally:
                ctx.close()
    finally:
        bob_ctx.close()
        run_agent(agent.close())
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        loop.close()


def shoot_approval(browser: Any, out: Path, world: Any) -> None:
    """#472: an agent whose person has a permission prompt open, in the board card that waits on it, the
    Inspector and (on a phone) Members. "before" is every agent at a prompt until now (busy); "after" is
    waiting for approval. The state is set as the Notification hook sets it, through the store: no scripted
    agent can reach it. It waits for the card's state, never for its words, so the same script shoots the
    page before the change."""
    from playwright.sync_api import expect

    for slug, ctx_args in (
        ("light", {**DESKTOP, "color_scheme": "light"}),
        ("dark", {**DESKTOP, "color_scheme": "dark"}),
        ("phone-light", {**PHONE, "color_scheme": "light"}),
    ):
        room = "#approval-" + slug
        world.create_room(room)
        world.add_agents(room, ("claude-1", "codex-1"))
        world.seed_review(room)
        ctx = browser.new_context(timezone_id="UTC", locale="en-US", **ctx_args)
        ctx.set_default_timeout(WAIT_MS)
        try:
            page = ctx.new_page()
            sign_in(page, world.broker)
            tab = page.locator('#tabs .room[data-room="' + room + '"]')
            if not tab.is_visible():
                page.click("#rooms-toggle")
            tab.click()
            page.click("#board-toggle")
            for stage, status in (("before", "busy"), ("after", "waiting-approval")):
                world.set_member_status(room, "codex-1", status, "hook:Notification")
                pulse = page.locator('#board .board-card[data-item="F4"] .board-pulse')
                expect(pulse).to_have_class(re.compile(r"\bs-" + status + r"\b"))
                if slug == "phone-light":
                    center(page, '#board .board-card[data-item="F4"]')
                settle(page)
                shot(page, out, f"approval-board-{stage}-{slug}.png")
                phone = slug == "phone-light"
                if phone:  # Members is a sheet on a phone, with the Inspector inside it
                    page.click("#buddy-toggle")
                    expect(page.locator("#app")).to_have_class(re.compile(r"\bsheet-open\b"))
                    settle(page)
                    shot(page, out, f"approval-members-{stage}-{slug}.png")
                with page.expect_response(
                    lambda r: "/members/codex-1" in r.url and r.request.method == "GET", timeout=WAIT_MS
                ):
                    page.click('#buddy-list .member[data-name="codex-1"] .m-open')
                expect(page.locator("#insp-name")).to_contain_text("codex-1")
                settle(page)
                shot(page, out, f"approval-inspector-{stage}-{slug}.png")
                back(page)
                if phone:
                    page.keyboard.press("Escape")
                    expect(page.locator("#app")).not_to_have_class(re.compile(r"\bsheet-open\b"))
        finally:
            ctx.close()


# -------------------------------------------------------------------- main
def run(args: argparse.Namespace, pw: Any) -> None:
    from ui_world import HostedWorld, UIWorld

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    world = UIWorld(keep=args.keep)
    hosted = HostedWorld(keep=args.keep)
    weak = HostedWorld(keep=args.keep)
    people = HostedWorld(keep=args.keep)  # alice, bob and carol, for mentioning a person (#418)
    consent = HostedWorld(keep=args.keep)  # an admin whose check has run out, for the consent page (#386)
    try:
        world.start()
        hosted.start()
        weak.start()
        people.start()
        consent.start()
        env = {**os.environ, "TZ": "UTC", "HOME": REAL_HOME or os.environ["HOME"]}
        browser = pw.chromium.launch(executable_path=args.chrome or None, env=env)
        try:
            shoot(browser, out, world)
            shoot_board(browser, out, world)
            shoot_review_card(browser, out, world)
            shoot_contest_flow(browser, out, world)
            shoot_board_tabs(browser, out, world)
            shoot_outdated_board(browser, out, world)
            shoot_icon_theme(browser, out, world)
            shoot_gitlab_lines(browser, out, world)
            shoot_claims(browser, out, world)
            shoot_proof_board(browser, out, world)
            shoot_handoff_board(browser, out, world)
            shoot_mentions(browser, out, world)
            shoot_mention_highlight(browser, out, world)
            shoot_broadcast_popover(browser, out, world)
            shoot_broadcast_mention(browser, out, world)
            shoot_humans_popover(browser, out, world)
            shoot_humans_mention(browser, out, world)
            shoot_claude_avatar(browser, out, world)
            shoot_offline(browser, out, world)
            # last of this world's shots: its two rooms' unread backlog would otherwise show up
            # (unread is recomputed per page load) in shoot_offline's sidebar too
            shoot_room_badges(browser, out, world)
            shoot_focus_mode(browser, out, world)
            shoot_wake_settings(browser, out, world)
            shoot_room_rename(browser, out, world)
            shoot_settings_tabs(browser, out, world)
            shoot_agent_icons(browser, out, world)
            shoot_settings_agents(browser, out, world)
            shoot_connect_codex(browser, out, world)
            shoot_oauth_consent(browser, out, world, consent)
            shoot_confirmed_check(browser, out, world)  # rooms of its own, so last in this world
            shoot_approval(browser, out, world)
            shoot_hosted(browser, out, hosted)
            shoot_common_password(browser, out, weak)
            shoot_mention_people(browser, out, people)
        finally:
            browser.close()
    finally:
        consent.stop()
        people.stop()
        weak.stop()
        hosted.stop()
        world.stop()
        if args.keep:
            print(
                f"kept: {world.home} {world.home2} {hosted.home} {weak.home} {people.home}",
                file=sys.stderr,
            )


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
