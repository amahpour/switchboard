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

The 15 outputs: desktop-{light,dark}, markdown-{light,dark}, palette-light, mention-light,
closed-light, remotes-light, inspector-light, inspector-remote-light, inspector-dark-parked,
phone-light, phone-dark-sheet, welcome-light and login-light (all .png).

Every wait has a deadline (Playwright's auto-waiting ``expect`` and ``wait_for_function``); a
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
PHONE: dict[str, Any] = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3,
                         "is_mobile": True, "has_touch": True}
TALL = 2400  # a desktop viewport tall enough for the whole #build conversation, cropped to it

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
    page.evaluate("document.fonts ? document.fonts.ready.then(() => true) : true")
    page.wait_for_function(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r("
        "document.getAnimations().every(a => a.playState !== 'running')))))",
        timeout=10_000)


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
    page.wait_for_function(f"document.querySelectorAll('#log .line.k-chat').length >= {n_chat}", timeout=WAIT_MS)
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

    with page.expect_response(lambda r: f"/members/{name}" in r.url and r.request.method == "GET",
                              timeout=WAIT_MS):
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
        " height: z.bottom + 12 - Math.max(0, a.top - 12)}; })()")


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


# -------------------------------------------------------------------- main
def run(args: argparse.Namespace, pw: Any) -> None:
    from ui_world import UIWorld

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    world = UIWorld(keep=args.keep)
    try:
        world.start()
        env = {**os.environ, "TZ": "UTC", "HOME": REAL_HOME or os.environ["HOME"]}
        browser = pw.chromium.launch(executable_path=args.chrome or None, env=env)
        try:
            shoot(browser, out, world)
        finally:
            browser.close()
    finally:
        world.stop()
        if args.keep:
            print(f"kept: {world.home} {world.home2}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--out", default=str(OUT), help="output directory (default docs/media/ui)")
    ap.add_argument("--chrome", help="a Chrome or Chromium binary to use instead of Playwright's own Chromium")
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
