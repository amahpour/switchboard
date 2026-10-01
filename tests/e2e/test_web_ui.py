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


# The dot (.remote-state::before) of the row or card head at sel, which is the seeded fpga-pi's
# (the only remote, never enabled: disabled), and of a clone of it in each of the other states.
REMOTE_DOTS = """([sel, states]) => {
  const dot = e => {
    const s = getComputedStyle(e.querySelector('.remote-state'), '::before');
    return {bg: s.backgroundColor, ring: s.boxShadow};
  };
  const real = document.querySelector(sel);
  const out = {disabled: dot(real)};
  for (const st of states) {
    const c = real.cloneNode(true);
    c.classList.replace('st-disabled', 'st-' + st);
    real.after(c);
    out[st] = dot(c);
    c.remove();
  }
  return out;
}"""

# each state's dot colour token; a disabled remote's dot is a hollow ring instead
DOT_COLOUR = {"up": "green", "connecting": "busy", "down": "danger", "error": "danger", "blocked": "danger"}


def test_remote_dots_show_their_state(ui: UI) -> None:
    """A remote's dot takes its state's colour, in the sidebar and in the remotes sheet: green
    when up, amber when connecting, red when down or blocked, a hollow ring when disabled. The
    sidebar's base rule used to outweigh the state rules, so every dot there was grey."""
    page = ui.open(room=None)
    tok = {k: rgb(page.evaluate(f"getComputedStyle(document.documentElement).getPropertyValue('--{k}')"))
           for k in ("green", "busy", "danger", "muted")}

    def check(sel: str) -> None:
        expect(page.locator(sel)).to_have_class(cls("st-disabled"))
        dots = page.evaluate(REMOTE_DOTS, [sel, list(DOT_COLOUR)])
        assert dots.pop("disabled") == {"bg": "rgba(0, 0, 0, 0)", "ring": f"{tok['muted']} 0px 0px 0px 1.5px inset"}, sel
        assert dots == {st: {"bg": tok[k], "ring": "none"} for st, k in DOT_COLOUR.items()}, sel

    check("#remotes .remote")  # the sidebar row
    expect(page.locator("#add-machine")).to_be_hidden()  # a desktop broker takes no machines that dial in
    expect(page.locator("#machines .remote")).to_have_count(0)
    page.click("#remotes .remote")
    expect(page.locator("#remotes-panel")).to_be_visible()
    check("#remotes-body .remote-head")  # the sheet's card


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


# A sidebar row per case, cloned from the seeded fpga-pi's with a name, a state and the text
# chipText() gives it: the name's and the state's [shown, full] widths, how far the state's
# right edge sits inside the row's padding (negative: it spills out) and its text-overflow.
REMOTE_ROWS = """(cases) => {
  const real = document.querySelector('#remotes .remote');
  return cases.map(([name, st, text]) => {
    const c = real.cloneNode(true);
    c.className = 'remote st-' + st;
    c.querySelector('.remote-name').textContent = name;
    c.querySelector('.remote-state').textContent = text;
    real.after(c);
    const n = c.querySelector('.remote-name'), s = c.querySelector('.remote-state');
    const inside = c.getBoundingClientRect().right - parseFloat(getComputedStyle(c).paddingRight)
                   - s.getBoundingClientRect().right;
    const out = {name: [n.clientWidth, n.scrollWidth], state: [s.clientWidth, s.scrollWidth], inside: inside,
                 overflow: getComputedStyle(s).textOverflow};
    c.remove();
    return out;
  });
}"""

LONG_STATES = [("scope-pi", "down", "down: timeout (retry in 20 s)"),
               ("build-vm", "blocked", "blocked: forced command failed"),
               ("dev-box-west", "disabled", "needs enable (config changed)")]


@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_a_long_remote_state_leaves_the_name_readable(ui: UI, size: str) -> None:
    """A sidebar row gives its name and its state half its width each, and whatever one doesn't
    need to the other. A long state (down with a countdown, blocked, a changed config) used to
    take the whole row and squeeze the name to nothing; now it ends in an ellipsis."""
    page = ui.open(room=None, **(PHONE if size == "phone" else {}))
    expect(page.locator("#remotes .remote")).to_have_count(1)
    rows = page.evaluate(REMOTE_ROWS, [*LONG_STATES, ("gpu-box", "up", "up · 14 ms"),
                                       ("fpga-bench-lab-02", "down", "down: timeout (retry in 20 s)")])
    *long, short, both = rows
    for (name, _, text), r in zip(LONG_STATES, long):
        assert r["name"][0] >= r["name"][1] - 1, f"{size}: {name} is cut to {r['name']} px next to {text!r}"
    assert all(r["inside"] >= -0.5 and r["overflow"] == "ellipsis" for r in rows), rows  # nothing spills out
    assert short["name"][0] >= short["name"][1] - 1 and short["state"][0] >= short["state"][1] - 1, short
    assert abs(short["inside"]) < 0.5, short  # a short state keeps to the right edge
    assert abs(both["name"][0] - both["state"][0]) <= 2, both  # a long name and a long state: half each


def test_a_remote_rows_title_has_its_whole_state(ui: UI) -> None:
    """The row's title has the state in full, since the row may end it in an ellipsis, and it
    ticks with the retry countdown as the row's text does. /api/remotes answers with fpga-pi
    down (the seeded world's is never enabled); no state change sends a remotes frame here."""
    ctx = ui.context()
    down = {"name": "fpga-pi", "state": "down", "reason": "timeout", "retry_in_s": 30, "rtt_ms": None}
    ctx.route("**/api/remotes", lambda route: route.fulfill(json={"remotes": [down], "config_error": None}))
    page = ctx.new_page()
    page.goto(ui.world.broker.login_url())
    expect(page.locator("#st-conn")).to_have_text("Connected")
    row = page.locator("#remotes .remote")
    expect(row).to_have_class(cls("st-down"))

    def title_and_text() -> tuple[str, str]:
        t, s = row.evaluate("r => [r.title, r.querySelector('.remote-state').textContent]")
        assert re.fullmatch(r"down: timeout \(retry in \d+ s\)", s), s
        assert t == f"Remote machine fpga-pi ({s}): open the remotes panel", (t, s)
        return t, s

    _, first = title_and_text()
    expect(row.locator(".remote-state")).not_to_have_text(first)  # the countdown ticks every second
    title_and_text()


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


# ------------------------------------------------------------------ focus (review findings)
ACTIVE_FOCUS_KEY = "() => document.activeElement ? (document.activeElement.dataset.focus || document.activeElement.id" \
                   " || document.activeElement.tagName) : null"


def member_row(page: Page, name: str) -> Any:
    return page.locator(f'#buddy-list .member[data-name="{name}"]')


def test_inspector_re_renders_keep_focus(ui: UI) -> None:
    """A members frame (here: another tab holding codex-1) re-renders the Inspector. Focus stays
    on the heading after the detail fetch, on a catch-up menu item and on the red Kick button,
    and the menu's arrow keys still work afterwards (they went dead with focus on <body>)."""
    page = ui.open()
    held = member_row(page, "codex-1")

    def members_frame(text: str) -> None:
        ui.world.command("build", text)
        if text.startswith("/hold"):
            expect(held).to_contain_text("held")
        else:
            expect(held).not_to_contain_text("held")

    try:
        with page.expect_response(lambda r: r.url.endswith("/api/rooms/build/members/claude-1")):
            member_row(page, "claude-1").click()
        expect(page.locator("#insp-body code.sess")).to_have_count(1)  # the detail landed and re-rendered
        expect(page.locator("#insp-name")).to_be_focused()

        # the catch-up menu: the second item keeps focus through a re-render, and ArrowDown moves on
        page.click("#insp-catchup")
        items = page.locator("#catchup-menu .menu-item")
        expect(items.first).to_be_focused()
        page.keyboard.press("ArrowDown")
        expect(items.nth(1)).to_be_focused()
        members_frame("/hold codex-1")
        expect(page.locator("#catchup-menu")).to_be_visible()
        expect(items.nth(1)).to_be_focused()
        page.keyboard.press("ArrowDown")
        expect(items.nth(2)).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#insp-catchup")).to_be_focused()

        # the kick confirm: Tab to the red Kick button, re-render, it is still focused; Esc backs out
        page.click("#insp-kick")
        expect(page.locator("#kick-confirm")).to_be_visible()
        page.keyboard.press("Tab")
        do_kick = page.locator("#kick-confirm .btn-danger")
        expect(do_kick).to_be_focused()
        members_frame("/release codex-1")
        expect(do_kick).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#kick-confirm")).to_be_hidden()
        expect(page.locator("#insp-kick")).to_be_focused()
        expect(member_row(page, "claude-1")).to_have_count(1)  # nothing was kicked
    finally:
        ui.world.command("build", "/release codex-1")


def test_focus_moves_to_a_neighbour_when_the_inspected_agent_goes(ui: UI) -> None:
    """A kick from the Inspector focuses the row that takes the kicked one's place; a kick from
    elsewhere while the Inspector has focus does the same; with no agent left, the Members
    heading gets it. Focus never drops to <body>."""
    ui.world.create_room("#e2e-focus")
    ui.world.add_agents("#e2e-focus", ("ag-1", "ag-2", "ag-3"))
    page = ui.open(room="e2e-focus")
    expect(page.locator("#buddy-list .member")).to_have_count(3)

    # 1. Kick from the Inspector's confirm (by keyboard): ag-3 takes ag-2's place
    member_row(page, "ag-2").click()
    expect(page.locator("#insp-name")).to_contain_text("ag-2")
    page.click("#insp-kick")
    page.keyboard.press("Tab")
    expect(page.locator("#kick-confirm .btn-danger")).to_be_focused()
    page.keyboard.press("Enter")
    expect(member_row(page, "ag-2")).to_have_count(0)  # the members frame landed
    expect(page.locator("#pane")).not_to_have_class(cls("inspecting"))
    expect(member_row(page, "ag-3")).to_be_focused()

    # 2. ag-3 (last in the list) is kicked from elsewhere while focus is on its Hold button
    member_row(page, "ag-3").click()
    expect(page.locator("#insp-name")).to_contain_text("ag-3")
    page.locator("#insp-hold").focus()
    ui.world.command("e2e-focus", "/kick ag-3")
    expect(page.locator("#pane")).not_to_have_class(cls("inspecting"))
    expect(member_row(page, "ag-1")).to_be_focused()

    # 3. the last agent goes: the Members heading
    member_row(page, "ag-1").click()
    page.locator("#insp-hold").focus()
    ui.world.command("e2e-focus", "/kick ag-1")
    expect(page.locator("#buddy-list .member")).to_have_count(0)
    expect(page.locator("#members-title")).to_be_focused()


def test_phone_members_sheet_takes_focus_and_gives_it_back(ui: UI) -> None:
    """At phone width the members sheet is a modal dialog: opening it focuses the first row and
    makes the page behind it inert; the scrim returns focus to the pill."""
    page = ui.open(**PHONE)
    page.locator("#buddy-toggle").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#app")).to_have_class(cls("sheet-open"))
    expect(member_row(page, "claude-1")).to_be_focused()
    assert page.evaluate("[document.getElementById('main').inert, document.getElementById('sidebar').inert]") == [True, True]
    page.keyboard.press("Tab")
    assert page.evaluate("document.getElementById('pane').contains(document.activeElement)"), \
        page.evaluate(ACTIVE_FOCUS_KEY)
    page.locator("#scrim").click(position={"x": 195, "y": 60})  # above the sheet
    expect(page.locator("#app")).not_to_have_class(cls("sheet-open"))
    expect(page.locator("#buddy-toggle")).to_be_focused()
    assert page.evaluate("document.getElementById('main').inert") is False


def test_phone_closed_sheet_gives_focus_to_the_rooms_toggle(ui: UI) -> None:
    """The Closed button sits in the rooms drawer, which closes (and hides) when the sheet opens,
    so closing the sheet focuses the Rooms toggle instead of losing focus."""
    page = ui.open(**PHONE)
    page.click("#rooms-toggle")
    page.click("#closed-rooms")
    expect(page.locator("#closed-panel")).to_be_visible()
    expect(page.locator("#closed-close")).to_be_focused()
    expect(page.locator("#sidebar")).to_be_hidden()  # visibility:hidden once the slide is over
    page.keyboard.press("Escape")
    expect(page.locator("#closed-panel")).to_be_hidden()
    expect(page.locator("#rooms-toggle")).to_be_focused()


def test_a_catchup_entry_keeps_the_draft_and_esc_brings_it_back(ui: UI) -> None:
    page = ui.open()
    box = page.locator("#input")
    box.fill("a half-written note")
    member_row(page, "claude-1").click()
    page.click("#insp-catchup")
    page.locator("#catchup-menu .menu-item", has_text="The whole room").click()
    expect(box).to_have_value("/catchup claude-1")
    expect(box).to_be_focused()
    expect(page.locator("#log")).to_contain_text("Your draft is kept")
    page.keyboard.press("Escape")
    expect(box).to_have_value("a half-written note")
    box.fill("")  # nothing is sent


def test_empty_room_copy_keeps_its_icon(ui: UI) -> None:
    """The join hint's Copy button changes only its label: it used to lose its icon for good."""
    ui.world.create_room("#e2e-copy")  # a room of its own: nobody in it and nothing said
    page = ui.open(room="e2e-copy", permissions=["clipboard-read", "clipboard-write"])
    copy = page.locator("#copy-join")
    expect(copy).to_be_visible()
    copy.click()
    expect(copy.locator("span")).to_have_text("Copied")
    expect(copy.locator("svg")).to_have_count(1)
    assert page.evaluate("navigator.clipboard.readText()") == page.locator("#join-line").inner_text()
    expect(copy.locator("span")).to_have_text("Copy")  # after COPIED_MS
    expect(copy.locator("svg")).to_have_count(1)


# ------------------------------------------------------------------ Mermaid diagrams (issue #57)
FENCE = "```"
FLOW = "flowchart LR\n  H[alice posts] --> B(broker)\n  B -->|idle| W[wake the agent]\n  B -->|busy| Q[queue]"
SEQ = "sequenceDiagram\n  alice->>claude-1: review the parser\n  claude-1-->>alice: two nits"
SHADOW_SVG = "(b) => { const f = b.querySelector('.md-diagram'); return !!(f && f.shadowRoot && f.shadowRoot.querySelector('svg')); }"
MERMAID_SCRIPTS = "document.querySelectorAll('script[src=\"/static/vendor/mermaid/mermaid.min.js\"]').length"


def diagram_room(ui: UI, name: str, *texts: str, **ctx: Any) -> Page:
    """A room of its own with ``texts`` posted by the human, open in a fresh page."""
    ui.world.create_room(f"#{name}")
    for t in texts:
        ui.world.say(name, t)
    page = ui.open(room=name, **ctx)
    expect(page.locator("#log .line.k-chat .md-pre")).to_have_count(len(texts))
    return page


def show(page: Page, n: int = 0) -> Any:
    """Click the n-th "Show diagram" and wait until it has drawn (or failed); the block's box."""
    box = page.locator("#log .md-pre").nth(n)
    button = box.locator("button.md-show-diagram")
    expect(button).to_have_text("Show diagram")
    button.click()
    expect(button).not_to_have_text(re.compile("Drawing"))
    expect(button).to_be_enabled()
    return box


def test_a_mermaid_block_shows_its_diagram_on_click_and_its_code_again(ui: UI) -> None:
    """A ```mermaid block shows as code with "Show diagram". Mermaid loads only then, once; the
    drawing lands in the block's shadow root (no style of it reaches the page) and "Show code"
    brings the code back. Copy copies the source either way (DESIGN.md §33)."""
    page = diagram_room(ui, "e2e-diagram", f"{FENCE}mermaid\n{FLOW}\n{FENCE}", f"{FENCE}mermaid\n{SEQ}\n{FENCE}",
                        permissions=["clipboard-read", "clipboard-write"])
    assert page.evaluate(MERMAID_SCRIPTS) == 0 and page.evaluate("typeof window.mermaid") == "undefined"
    styles_before = page.evaluate("document.querySelectorAll('style').length")
    box = show(page)
    expect(box.locator("button.md-show-diagram")).to_have_text("Show code")
    expect(box).to_have_class(cls("md-showing-diagram"))
    expect(box.locator(".md-diagram")).to_be_visible()
    expect(box.locator(".md-pre-body")).to_be_hidden()
    assert box.evaluate(SHADOW_SVG)
    assert box.evaluate("(b) => b.querySelector('.md-diagram').shadowRoot.querySelector('svg style') !== null")
    assert page.evaluate("document.querySelectorAll('style').length") == styles_before  # none in the page
    assert page.evaluate("document.querySelectorAll('.md-diagram-stage').length") == 0
    box.locator("button.md-copy", has_text="Copy").click()
    assert page.evaluate("navigator.clipboard.readText()") == FLOW
    box.locator("button.md-show-diagram").click()  # back to the code
    expect(box.locator("button.md-show-diagram")).to_have_text("Show diagram")
    expect(box.locator(".md-pre-body")).to_be_visible()
    expect(box.locator(".md-diagram")).to_be_hidden()
    second = show(page, 1)
    assert second.evaluate(SHADOW_SVG)
    assert page.evaluate(MERMAID_SCRIPTS) == 1  # loaded once, for both


def test_a_diagram_fills_the_screen_and_returns_drawn_after_escape(ui: UI) -> None:
    """The Full screen button uses the diagram box, and Esc returns to the drawn message."""
    page = diagram_room(ui, "e2e-diagram-fullscreen", f"{FENCE}mermaid\n{FLOW}\n{FENCE}")
    box = show(page)
    full = box.get_by_role("button", name="Full screen")
    expect(full).to_be_visible()
    full.click()
    assert box.evaluate("(b) => document.fullscreenElement === b.querySelector('.md-diagram')")
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.fullscreenElement === null")
    expect(box).to_have_class(cls("md-showing-diagram"))
    assert box.evaluate(SHADOW_SVG)
    expect(full).to_be_visible()
    box.locator("button.md-show-diagram").click()
    expect(full).to_be_hidden()


def test_a_diagram_follows_the_light_or_dark_scheme(ui: UI) -> None:
    """A drawing uses the page's scheme, and one on show is redrawn when the scheme changes."""
    page = diagram_room(ui, "e2e-diagram-dark", f"{FENCE}mermaid\n{FLOW}\n{FENCE}", color_scheme="dark")
    box = show(page)
    fill = "(b) => getComputedStyle(b.querySelector('.md-diagram').shadowRoot.querySelector('.node rect')).fill"
    dark = box.evaluate(fill)
    page.emulate_media(color_scheme="light")
    page.wait_for_function(f"(prev) => ({fill})(document.querySelector('#log .md-pre')) !== prev", arg=dark)
    light = box.evaluate(fill)
    assert dark != light, (dark, light)
    expect(box).to_have_class(cls("md-showing-diagram"))


def test_a_hostile_diagram_cannot_reach_the_page(ui: UI) -> None:
    """Diagram source is message text: it can't loosen Mermaid's settings from its own config,
    run script, load anything, link anywhere or restyle the page. The fixture's watch fails the
    test on any console error or CSP violation."""
    hostile = (
        '%%{init: {"securityLevel": "loose", "htmlLabels": true, "flowchart": {"htmlLabels": true},'
        ' "theme": "forest", "themeCSS": "body { display: none }", "dompurifyConfig": {"ADD_TAGS": ["iframe"]}}}%%\n'
        "flowchart TD\n"
        '  A["<img src=x onerror=window.__pwned=1> <b>bold</b>"] --> B[next]\n'
        '  click A "/logout" _self\n'
        '  click B call alert(1)\n'
        "  style A fill:#f00,stroke:#333")
    page = diagram_room(ui, "e2e-diagram-hostile", f"{FENCE}mermaid\n{hostile}\n{FENCE}")
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    box = show(page)
    assert box.evaluate(SHADOW_SVG)
    root = "(b) => b.querySelector('.md-diagram').shadowRoot"
    found = box.evaluate(f"(b) => {{ const r = ({root})(b); return {{"
                         " unsafe: r.querySelectorAll('img, image, foreignObject, iframe, script, object, embed').length,"
                         " links: r.querySelectorAll('[href], [*|href]').length,"
                         " text: [...r.querySelectorAll('text')].map(t => t.textContent).join(' '),"
                         " css: [...r.querySelectorAll('style')].map(s => s.textContent).join('') }; }")
    assert found["unsafe"] == 0 and found["links"] == 0, found
    assert '<img src="x"' in found["text"] and "onerror" not in found["text"]  # a label is text
    assert "display: none" not in found["css"] and "#cde498" not in found["css"]  # no themeCSS, not forest
    # clicking either node does nothing: no callback was bound (an alert() would have opened, and
    # been recorded, before the click returned) and there is no href to follow (above)
    for node in ("A", "B"):
        clicked = box.evaluate(f"(b) => {{ const n = ({root})(b).querySelector('g.node[id*=\"-{node}-\"]');"
                               " return !!n && n.dispatchEvent(new MouseEvent('click', {bubbles: true})); }")
        assert clicked, node
    assert dialogs == [] and page.evaluate("window.__pwned") is None
    assert page.evaluate("fetch('/api/me').then(r => r.status)") == 200
    expect(page.locator("#log")).to_be_visible()


def test_a_broken_diagram_says_why_and_keeps_its_code(ui: UI) -> None:
    page = diagram_room(ui, "e2e-diagram-broken", f"{FENCE}mermaid\nflowchart LR\n  A -->\n{FENCE}")
    box = show(page)
    expect(box.locator("button.md-show-diagram")).to_have_text("Show diagram")
    expect(box.locator(".md-diagram-error")).to_contain_text("Can't draw this diagram: Parse error on line 3")
    expect(box.locator(".md-pre-body")).to_be_visible()
    expect(box.locator(".md-diagram")).to_be_hidden()
    assert not box.evaluate(SHADOW_SVG)  # no "syntax error" drawing either
