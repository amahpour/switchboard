"""The web UI in a real browser (issue #19): Playwright end-to-end tests. Marker ``e2e``, opt-in.

    uv run playwright install chromium       # once per machine (CI: --with-deps)
    uv run pytest -m e2e tests/e2e           # add --headed or --slowmo 250 to watch

The node tests (tests/unit/test_web_app_behavior.py, test_web_markdown.py) run app.js and md.js
against a fake DOM; these run the shipped page in Chromium against a real broker, so layout,
CSS, the CSP, focus and the browser's own event order are what is under test.

The world (``tests/ui_world.py``, shared with docs/media/ui_shots.py) is built once per module:
an in-process test-mode broker in a temp home, seeded with three rooms (``#docs`` closed), a
never-enabled remote and four scripted agents with a Markdown-rich conversation in ``#build``.
Every test gets fresh browser contexts from the ``ui`` fixture, each signed in through its own
one-time login link. Tests that change state (a hold, a close) undo it or use a room of their own.

Every page is watched: a console error, an uncaught page error or a CSP violation (reported by
a ``securitypolicyviolation`` listener the context injects) fails the test, listed per page.
On any failure the fixture saves a Playwright trace (open it with ``uv run playwright show-trace
<zip>``) and a screenshot of every open page under ``e2e-artifacts/<test>/`` (or
``$SWITCHBOARD_E2E_ARTIFACTS``), the folder CI uploads.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, ConsoleMessage, Page, expect

from conftest import PHASE_REPORTS, TEST_HUMAN, sanitize_env
from ui_world import CI_URL, JS_SCHEME, PROFILE, UIWorld

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(os.environ.get("SWITCHBOARD_E2E_ARTIFACTS") or ROOT / "e2e-artifacts")

DESKTOP: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
PHONE: dict[str, Any] = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3,
                         "is_mobile": True, "has_touch": True}
WAIT_MS = 15_000

# Injected into every page before its own scripts: a CSP violation becomes a console error,
# which the console watcher below records (Chromium logs most of them anyway; this is sure).
CSP_PROBE = """
document.addEventListener('securitypolicyviolation', function (e) {
  console.error('CSP violation: ' + e.violatedDirective + ' blocked ' + (e.blockedURI || 'inline') +
                ' at ' + (e.sourceFile || '?') + ':' + (e.lineNumber || 0));
});
"""

expect.set_options(timeout=WAIT_MS)


def cls(name: str) -> re.Pattern[str]:
    """A class-list matcher for ``to_have_class``: ``name`` as a whole word."""
    return re.compile(rf"(^|\s){re.escape(name)}(\s|$)")


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def world(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[UIWorld]:
    """The seeded broker, once per module. It asks for ``playwright`` (the driver, a session
    fixture) only so the driver starts first, under the real HOME, where
    ``playwright install`` put the browsers; the environment is then isolated for the broker
    and the agents (a module-wide version of the autouse ``clean_env``)."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        w = UIWorld().start()
        try:
            yield w
        finally:
            w.stop()


class UI:
    """Browser contexts for one test, each watched for problems and traced."""

    def __init__(self, browser: Browser, world: UIWorld) -> None:
        self.browser = browser
        self.world = world
        self.contexts: list[BrowserContext] = []
        self.problems: list[str] = []

    def _watch(self, page: Page, where: str) -> None:
        def on_console(msg: ConsoleMessage) -> None:
            if msg.type == "error":
                loc = msg.location or {}
                self.problems.append(f"{where}: console error: {msg.text} ({loc.get('url', '?')}:{loc.get('lineNumber', '?')})")

        page.on("console", on_console)
        page.on("pageerror", lambda err: self.problems.append(f"{where}: page error: {err}"))

    def context(self, **args: Any) -> BrowserContext:
        """A new context (desktop size by default), traced, with every page in it watched."""
        ctx = self.browser.new_context(**{"timezone_id": "UTC", "locale": "en-US", **DESKTOP, **args})
        ctx.set_default_timeout(WAIT_MS)
        ctx.tracing.start(screenshots=True, snapshots=True, sources=False)
        ctx.add_init_script(CSP_PROBE)
        n = len(self.contexts)
        pages = [0]

        def on_page(p: Page) -> None:
            self._watch(p, f"context {n} page {pages[0]}")
            pages[0] += 1

        ctx.on("page", on_page)
        self.contexts.append(ctx)
        return ctx

    def open(self, room: str | None = "build", **args: Any) -> Page:
        """A signed-in page (a fresh one-time login link), connected, with ``room`` open."""
        page = self.context(**args).new_page()
        page.goto(self.world.broker.login_url())  # 303 to "/", which sets the session cookie
        expect(page.locator("#st-conn")).to_have_text("Connected")
        if room:
            open_room(page, room)
        return page

    @staticmethod
    def artifacts_dir(test_id: str) -> Path:
        return ARTIFACTS / re.sub(r"[^A-Za-z0-9_.-]+", "-", test_id).strip("-")

    def finish(self, failed: bool, test_id: str) -> None:
        """Stop tracing and close every context; on a failure keep a trace and screenshots."""
        out = self.artifacts_dir(test_id)
        for i, ctx in enumerate(self.contexts):
            try:
                if failed:
                    out.mkdir(parents=True, exist_ok=True)
                    for j, p in enumerate(ctx.pages):
                        try:
                            p.screenshot(path=str(out / f"context{i}-page{j}.png"), full_page=True)
                        except Exception as e:  # a crashed page: the trace still has its frames
                            (out / f"context{i}-page{j}.txt").write_text(f"no screenshot: {e}\n")
                    ctx.tracing.stop(path=str(out / f"context{i}-trace.zip"))
                else:
                    ctx.tracing.stop()
            finally:
                ctx.close()
        if failed:
            print(f"\ne2e artifacts: {out}")


@pytest.fixture
def ui(browser: Browser, world: UIWorld, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, world)
    # a previous run's artifacts for this test would read as this run's
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


# ------------------------------------------------------------------ helpers
def open_room(page: Page, room: str) -> None:
    """Click a room in the sidebar (opening the drawer first at phone width) and wait for its
    header (and, for #build, its seeded log and members)."""
    tab = page.locator(f'#tabs .room[data-room="#{room}"]')
    if not tab.is_visible():
        page.click("#rooms-toggle")
    tab.click()
    expect(page.locator("#room-title")).to_have_text(room)
    if room == "build":
        expect(page.locator("#buddy-list .member")).to_have_count(4)
        page.wait_for_function("document.querySelectorAll('#log .line.k-chat').length >= 8")


def chat_row(page: Page, text: str) -> Any:
    """The log row (a chat line) whose text contains ``text``."""
    return page.locator("#log .line.k-chat", has_text=text).last


def rgb(hex_color: str) -> str:
    """'#1c1c1f' -> 'rgb(28, 28, 31)', how getComputedStyle reports an opaque colour."""
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgb({r}, {g}, {b})"


def no_horizontal_scroll(page: Page) -> None:
    widths = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth,"
                           " document.body.scrollWidth, document.body.clientWidth]")
    assert widths[0] <= widths[1] and widths[2] <= widths[3], f"horizontal scroll: {widths}"


# ------------------------------------------------------------------ tests
def test_problem_watch_catches_a_csp_violation(ui: UI) -> None:
    """The guard the other tests rely on is live: an inline script the page's CSP refuses is
    reported (then cleared, so this test passes)."""
    page = ui.open(room=None)
    page.evaluate("() => { const s = document.createElement('script'); s.textContent = 'window.__x = 1';"
                  " document.body.append(s); }")
    page.wait_for_timeout(200)  # the violation event is queued, not synchronous
    assert page.evaluate("window.__x") is None, "the CSP let an inline script run"
    assert any("CSP violation" in p or "Content Security Policy" in p for p in ui.problems), ui.problems
    ui.problems.clear()


def test_app_loads_connected_with_rooms_members_and_chips(ui: UI) -> None:
    page = ui.context().new_page()
    resp = page.goto(ui.world.broker.login_url())
    assert resp is not None and resp.ok
    csp = resp.headers.get("content-security-policy")  # the page's own (after the login redirect)
    assert csp and "script-src 'self'" in csp, f"no CSP on the page: {resp.headers}"
    expect(page.locator("#st-conn")).to_have_text("Connected")
    expect(page.locator("#me-name")).to_have_text(TEST_HUMAN)

    # rooms: the two open ones; #docs is closed and counted under Closed
    expect(page.locator('#tabs .room[data-room="#build"]')).to_be_visible()
    expect(page.locator('#tabs .room[data-room="#fpga-bench"]')).to_be_visible()
    expect(page.locator('#tabs .room[data-room="#docs"]')).to_have_count(0)
    expect(page.locator("#closed-label")).to_have_text(re.compile(r"^Closed \(\d+\)$"))

    open_room(page, "build")
    for name in PROFILE:
        expect(page.locator(f'#buddy-list .member[data-name="{name}"]')).to_be_visible()
    expect(page.locator("#agents-title")).to_have_text("Agents (4)")
    expect(page.locator("#buddy-me")).to_contain_text(TEST_HUMAN)
    # the header chips: wakes left / per hour, and agent messages in a row / the hop limit
    expect(page.locator("#st-budget")).to_have_text(re.compile(r"Budget\s*\d+/\d+"))
    expect(page.locator("#st-hops")).to_have_text(re.compile(r"Hops\s*\d+/30"))
    expect(page.locator("#st-state")).to_have_text(re.compile(r"Running"))
    # codex-1 runs with approvals off: the chip and the band say so
    expect(page.locator("#st-approvals")).to_contain_text("codex-1")
    expect(page.locator("#banner-bridge")).to_be_visible()
    expect(page.locator("#banner-test")).to_be_hidden()


def test_markdown_renders_real_elements(ui: UI) -> None:
    page = ui.open()
    log = page.locator("#log")
    # each construct is a real element, not text with asterisks or pipes in it
    for sel, text in (("strong", "Doing:"), ("em", "pick a free port"), ("code.md-code", "parse_port"),
                      ("pre", "def parse_port"), ("ol li", "Reject non-digits"),
                      ("ul li", "Leading zeros"), ("blockquote", "add input validation"),
                      ("table th", "input"), ("table td", "ValueError"), ("[role=heading]", "Open questions")):
        expect(log.locator(sel, has_text=text).first).to_be_visible()
    expect(log.locator("hr.md-hr").first).to_be_attached()
    # a fence keeps its lines
    assert "raise ValueError" in log.locator("pre", has_text="def parse_port").first.inner_text()


def test_markdown_links_and_raw_html_stay_inert(ui: UI) -> None:
    page = ui.open()
    devin = chat_row(page, "Picking this up from")

    # [CI run](javascript:...) is a blocked label, never a link
    blocked = devin.locator(".md-blocked", has_text="CI run")
    expect(blocked).to_be_visible()
    expect(devin.locator(".md-blocked-pill")).to_have_text("link blocked")
    assert blocked.evaluate("e => e.tagName") == "SPAN"
    assert blocked.get_attribute("href") is None
    assert devin.locator("a").count() == 0
    assert page.evaluate(f"[...document.querySelectorAll('[href]')].filter(e => e.getAttribute('href')"
                         f".toLowerCase().includes({JS_SCHEME!r})).length") == 0

    # raw HTML is literal text: no script or b element anywhere in the log
    expect(devin).to_contain_text("<script>alert(1)</script> stays text, as does <b>this</b>.")
    assert page.locator("#log script").count() == 0
    assert page.locator("#log b").count() == 0

    # an https link: new tab, no opener, no referrer, nofollow, and the real URL shown after it
    link = page.locator(f'#log a.md-link[href="{CI_URL}"]')
    expect(link).to_have_text("CI run")
    assert link.get_attribute("rel") == "noopener noreferrer nofollow"
    assert link.get_attribute("target") == "_blank"
    assert link.get_attribute("referrerpolicy") == "no-referrer"
    expect(chat_row(page, "Bitstream flashed").locator(".md-url")).to_have_text(CI_URL)

    # a link back to this switchboard (switchboard.localhost) is inert too, from the human as well
    local = f"http://switchboard.localhost:{ui.world.broker.port}/#build"
    ui.world.say("fpga-bench", f"Open [the room]({local}) please.")
    open_room(page, "fpga-bench")
    row = chat_row(page, "please.")
    expect(row.locator(".md-blocked", has_text="the room")).to_be_visible()
    expect(row.locator(".md-blocked-pill")).to_have_text("link blocked")
    assert row.locator("a").count() == 0
    assert page.locator('#log a[href*="localhost"]').count() == 0


@pytest.mark.parametrize("scheme,other", [("light", "dark"), ("dark", "light")])
def test_color_scheme_follows_the_system(ui: UI, scheme: str, other: str) -> None:
    page = ui.open(color_scheme=scheme)

    def body_and_token() -> tuple[str, str]:
        return page.evaluate("[getComputedStyle(document.body).backgroundColor,"
                             " getComputedStyle(document.documentElement).getPropertyValue('--bg')]")

    bg, token = body_and_token()
    assert bg == rgb(token), (bg, token)
    assert bg == {"light": "rgb(255, 255, 255)", "dark": "rgb(28, 28, 31)"}[scheme]
    page.emulate_media(color_scheme=other)
    bg2, token2 = body_and_token()
    assert bg2 == rgb(token2) and bg2 != bg, (bg, bg2)


def test_phone_layout_drawer_and_members_sheet(ui: UI) -> None:
    page = ui.open(**PHONE)
    app = page.locator("#app")
    no_horizontal_scroll(page)

    # the sidebar is a drawer, hidden until the Rooms toggle; Esc closes it
    expect(page.locator("#sidebar")).to_be_hidden()
    page.click("#rooms-toggle")
    expect(app).to_have_class(cls("nav-open"))
    expect(page.locator("#sidebar")).to_be_visible()
    expect(page.locator("#rooms-toggle")).to_have_attribute("aria-expanded", "true")
    no_horizontal_scroll(page)
    page.keyboard.press("Escape")
    expect(app).not_to_have_class(cls("nav-open"))
    expect(page.locator("#sidebar")).to_be_hidden()

    # the members sheet: the pill opens it over a scrim; Esc closes it, and so does the scrim
    page.click("#buddy-toggle")
    expect(app).to_have_class(cls("sheet-open"))
    expect(page.locator("#scrim")).to_be_visible()
    expect(page.locator("#pane")).to_be_in_viewport()
    expect(page.locator("#pane")).to_have_attribute("role", "dialog")
    expect(page.locator('#buddy-list .member[data-name="bench"]')).to_be_visible()
    no_horizontal_scroll(page)
    page.keyboard.press("Escape")
    expect(app).not_to_have_class(cls("sheet-open"))
    expect(page.locator("#scrim")).to_be_hidden()
    expect(page.locator("#buddy-toggle")).to_be_focused()

    page.click("#buddy-toggle")
    expect(app).to_have_class(cls("sheet-open"))
    page.locator("#scrim").click(position={"x": 195, "y": 60})  # above the sheet
    expect(app).not_to_have_class(cls("sheet-open"))
    expect(page.locator("#scrim")).to_be_hidden()
    no_horizontal_scroll(page)


def test_inspector_shows_the_member_detail(ui: UI) -> None:
    page = ui.open()
    with page.expect_response(lambda r: r.url.endswith("/api/rooms/build/members/bench")) as got:
        page.click('#buddy-list .member[data-name="bench"]')
    detail = got.value.json()
    _, tier, _, _, host, sid = PROFILE["bench"]
    assert detail["member"]["tier"] == tier and detail["member"]["host"] == host
    assert detail["session"]["id"] == sid
    assert isinstance(detail["queued"], list) and isinstance(detail["timeline"], list)

    body = page.locator("#insp-body")
    expect(page.locator("#pane")).to_have_class(cls("inspecting"))
    expect(page.locator("#insp-name")).to_have_text("bench@fpga-pi")
    expect(body.locator(".insp-chips")).to_contain_text(tier)
    expect(body.locator(".insp-chips")).to_contain_text(host)
    expect(body.locator("code.sess")).to_have_attribute("title", sid)
    expect(body.locator(".insp-facts")).to_contain_text("Queued")
    expect(body.locator(".insp-timeline")).to_contain_text("Delivery")
    expect(page.locator("#insp-pos")).to_have_text(re.compile(r"Agent \d of 4"))

    # back returns to Members
    page.click("#insp-back")
    expect(page.locator("#pane")).not_to_have_class(cls("inspecting"))
    expect(page.locator("#members-view")).not_to_have_class(cls("offscreen"))
    expect(page.locator('#buddy-list .member[data-name="bench"]')).to_be_visible()

    # a member with no session id gets the reason instead (codex-1: no Codex thread id)
    with page.expect_response(lambda r: r.url.endswith("/api/rooms/build/members/codex-1")) as got:
        page.click('#buddy-list .member[data-name="codex-1"]')
    why = got.value.json()["session"]
    assert not why["id"] and why["why"]
    expect(body.locator(".insp-facts")).to_contain_text(why["why"])
    expect(body.locator(".insp-attn")).to_contain_text("Approvals off")


def test_inspector_hold_posts_the_command_and_shows_held(ui: UI) -> None:
    page = ui.open()
    page.click('#buddy-list .member[data-name="codex-1"]')
    expect(page.locator("#insp-name")).to_contain_text("codex-1")
    hold = page.locator("#insp-hold")
    expect(hold).to_have_text("Hold")
    try:
        with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/rooms/build/command")) as req:
            hold.click()
        assert req.value.post_data_json == {"text": "/hold codex-1"}
        expect(page.locator("#insp-body .chip-held")).to_have_text("Held")
        expect(hold).to_have_text("Release")
        expect(hold).to_have_attribute("aria-pressed", "true")
        expect(page.locator('#buddy-list .member[data-name="codex-1"]')).to_contain_text("held")

        # and Release undoes it through the same path
        with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/command")) as req:
            hold.click()
        assert req.value.post_data_json == {"text": "/release codex-1"}
        expect(page.locator("#insp-body .chip-held")).to_have_count(0)
        expect(hold).to_have_text("Hold")
    finally:
        # leave the shared world as it was, whatever happened above (an extra release is a no-op reply)
        assert ui.world.web is not None
        ui.world.web.post("/api/rooms/build/command", json={"text": "/release codex-1"},
                          headers=ui.world.broker.write_headers())


def test_slash_palette_and_mentions(ui: UI) -> None:
    page = ui.open()
    box = page.locator("#input")
    box.focus()

    # "/" opens the palette with the first command highlighted; arrows move; Enter fills it
    page.keyboard.type("/")
    palette = page.locator("#palette")
    expect(palette).to_be_visible()
    expect(page.locator("#pal-pause")).to_have_attribute("aria-selected", "true")
    expect(box).to_have_attribute("aria-expanded", "true")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    expect(page.locator("#pal-budget")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#pal-pause")).to_have_attribute("aria-selected", "false")
    page.keyboard.press("ArrowUp")
    expect(page.locator("#pal-resume")).to_have_attribute("aria-selected", "true")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(box).to_have_value("/budget ")  # a command with arguments waits for them
    expect(palette).to_be_hidden()

    # "@co" lists the matching agents; Enter inserts the name and a space
    box.fill("")
    page.keyboard.type("ask @co")
    mentions = page.locator("#mentions")
    expect(mentions).to_be_visible()
    expect(page.locator("#men-codex-1")).to_have_attribute("aria-selected", "true")
    expect(mentions.locator("[role=option]")).to_have_count(1)
    page.keyboard.press("Enter")
    expect(box).to_have_value("ask @codex-1 ")
    expect(mentions).to_be_hidden()

    # Esc closes an open popover and keeps the text
    page.keyboard.type("/")  # not at the start of the message: no palette
    expect(palette).to_be_hidden()
    box.fill("/he")
    box.dispatch_event("input")
    expect(palette).to_be_visible()
    page.keyboard.press("Escape")
    expect(palette).to_be_hidden()
    expect(box).to_have_value("/he")
    box.fill("")  # nothing is sent


def test_close_a_room_then_reopen_it(ui: UI) -> None:
    ui.world.create_room("#e2e-close")
    page = ui.open(room=None)
    tab = page.locator('#tabs .room[data-room="#e2e-close"]')
    expect(tab).to_be_visible()
    open_room(page, "e2e-close")
    before = int(re.findall(r"\d+", page.locator("#closed-label").inner_text())[0])

    # /close asks first (the native confirm); accepting it closes the room and drops its tab
    asked: list[str] = []

    def accept(dialog: Any) -> None:
        asked.append(dialog.message)
        dialog.accept()

    page.once("dialog", accept)
    page.locator("#input").focus()
    page.keyboard.type("/close")
    page.keyboard.press("Enter")
    expect(tab).to_have_count(0)
    assert asked and asked[0].startswith("Close #e2e-close?"), asked
    expect(page.locator("#closed-label")).to_have_text(f"Closed ({before + 1})")

    # the Closed rooms sheet lists it; Reopen brings it back under its name
    page.click("#closed-rooms")
    expect(page.locator("#closed-panel")).to_be_visible()
    card = page.locator("#closed-body .closed-card").filter(
        has=page.locator(".closed-name", has_text=re.compile(r"^e2e-close$")))
    expect(card).to_have_count(1)
    card.get_by_role("button", name="Reopen").click()
    expect(tab).to_be_visible()
    expect(page.locator("#closed-label")).to_have_text(f"Closed ({before})")
    page.keyboard.press("Escape")
    expect(page.locator("#closed-panel")).to_be_hidden()


FOCUS_RING = """() => {
  const e = document.activeElement;
  if (!e || e === document.body) return null;
  const s = getComputedStyle(e);
  const outline = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0;
  // the message box shows its ring on the composer around it (.composer-box:focus-within)
  const box = e.closest('.composer-box');
  const halo = !!box && getComputedStyle(box).boxShadow !== 'none';
  return {id: e.id, cls: String(e.className), name: e.dataset.name || '',
          visible: e.matches(':focus-visible'), ring: outline || halo};
}"""


def test_keyboard_tab_walk_shows_a_focus_ring(ui: UI) -> None:
    page = ui.open()
    page.evaluate("document.activeElement && document.activeElement.blur()")
    seen: dict[str, dict[str, Any]] = {}
    for _ in range(80):
        page.keyboard.press("Tab")
        f = page.evaluate(FOCUS_RING)
        if not f:
            continue
        key = "input" if f["id"] == "input" else "pane-toggle" if f["id"] == "pane-toggle" else (
            "member" if "member" in f["cls"].split() and f["name"] else "")
        if key and key not in seen:
            seen[key] = f
        if len(seen) == 3:
            break
    assert set(seen) == {"input", "pane-toggle", "member"}, f"the Tab walk reached only {sorted(seen)}"
    for key, f in seen.items():
        assert f["visible"] and f["ring"], f"{key} has no visible focus ring: {f}"
