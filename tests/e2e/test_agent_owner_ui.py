"""Whose an agent is, in the page (#496, DESIGN.md §29.10), on a hosted broker with alice (the admin).

Clicking an agent names its owner and its device in the Inspector, on a desktop and on a phone, light
and dark. Members names the owner under each agent's name only when the switchboard has more than one
person: with one, every agent is that person's and the line would only be noise. An agent whose owner
the broker can't work out shows no owner, and no row ever shows an email.

The agents are scripted test agents moved onto a device (``adopt``, as ``tests/ui_world.py`` does for
its own), because a session over HTTPS is the one thing a scripted agent is not; the broker side that
names the owner is driven with real Codex sessions in ``tests/integration/test_agent_owner.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from fakes.fake_agent import FakeAgent
from playwright.sync_api import Browser, Page, expect
from test_private_rooms_ui import person_cookie
from test_web_ui import PHASE_REPORTS, PHONE, UI

from switchboard import db
from switchboard.broker.auth import WebOrigin
from switchboard.models import session_key

pytestmark = pytest.mark.e2e

ADMIN_PW = "correct horse battery"
ROOM = "#build"


class Hosted:
    """A hosted broker set up through its API: alice, the admin, and the room ``#build``. Test agents
    join through ``agent`` and are put on a device with ``adopt``; people are added with ``person``."""

    def __init__(self, broker: InProcBroker, cookie: str) -> None:
        self.broker = broker
        self.cookie = cookie  # alice's session
        self.origin = broker.state.web_origin.origin
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="e2e-owner-agents", daemon=True)
        self.thread.start()
        self.agents: list[FakeAgent] = []

    def agent(self, name: str, device: str, person: int | None, *, token: bool = True) -> None:
        """``name`` joins ``#build`` and runs on ``device``, whose agent token is ``person``'s (None: the
        owner's), or nobody's with ``token=False``: a device nothing vouches for."""
        fake = FakeAgent(self.broker.home, f"owner-ui-{name}", person_id=person)
        self.agents.append(fake)
        asyncio.run_coroutine_threadsafe(fake.start(), self.loop).result(60)
        joined = asyncio.run_coroutine_threadsafe(fake.join(ROOM, name), self.loop).result(60)
        assert joined["ok"], joined
        self.broker.on_loop(self._adopt, name, device, person, token)

    def _adopt(self, name: str, device: str, person: int | None, token: bool) -> None:
        """Fixtures only (the scripted agent has no device, §49.3): move ``name`` onto ``device``."""
        store = self.broker.state.store
        if token:
            digest = hashlib.sha256(device.encode()).hexdigest()
            assert store.agent_token_add(person, device, digest)
        room = store.get_room(ROOM)
        assert room is not None
        member = next(m for m in store.members(room.id) if m.name == name)
        with db.tx(store.con):
            store.con.execute(
                "UPDATE participants SET host=?, session_key=? WHERE id=?",
                (device, session_key("test", device, name), member.participant_id),
            )
        self.broker.state.hub.members_changed(ROOM)

    def person(self, name: str) -> int:
        """Add ``name`` (with an email, which the page must never show) and return their id."""
        h = {"Origin": self.origin, "X-Switchboard": "1"}
        cookies = {"switchboard_session": self.cookie}
        with httpx.Client(base_url=self.origin, headers=h, cookies=cookies) as c:
            added = c.post("/api/people", json={"name": name, "email": f"{name}@example.com"})
        assert added.status_code == 200, added.text
        return int(added.json()["person"]["id"])

    def remove(self, person: int) -> None:
        h = {"Origin": self.origin, "X-Switchboard": "1"}
        with httpx.Client(base_url=self.origin, headers=h, cookies={"switchboard_session": self.cookie}) as c:
            assert c.post(f"/api/people/{person}/remove", json={}).status_code == 200

    def close(self) -> None:
        for fake in self.agents:
            asyncio.run_coroutine_threadsafe(fake.close(), self.loop).result(60)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


@pytest.fixture
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Hosted]:
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
            assert c.post("/api/rooms", json={"name": ROOM}).status_code == 200
            cookie = c.cookies.get("switchboard_session")
        assert cookie
        world = Hosted(b, cookie)
        try:
            yield world
        finally:
            world.close()
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


def open_page(ui: UI, world: Hosted, cookie: str | None = None, **options: Any) -> Page:
    """The room in a page signed in as ``cookie`` (alice's by default), connected."""
    ctx = ui.context(**options)
    ctx.add_cookies([{"name": "switchboard_session", "value": cookie or world.cookie, "url": world.origin}])
    page = ctx.new_page()
    page.goto(world.origin + "/")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    page.locator(f'#tabs .room[data-room="{ROOM}"]').click()
    return page


def members_open(page: Page, phone: bool) -> None:
    """On a phone Members is a sheet behind the header's pill; the list is on screen otherwise."""
    if phone:
        page.click("#buddy-toggle")
    expect(page.locator("#buddy-list")).to_be_visible()


def row(page: Page, name: str) -> Any:
    return page.locator(f'#buddy-list .member[data-name="{name}"]')


def luminance(rgb: str) -> float:
    r, g, b = (int(x) / 255 for x in re.findall(r"\d+", rgb)[:3])
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(fg: str, bg: str) -> float:
    a, b = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


@pytest.mark.parametrize(
    "options",
    [{"color_scheme": "light"}, {"color_scheme": "dark"}, {**PHONE, "color_scheme": "light"}],
    ids=["light", "dark", "phone"],
)
def test_clicking_an_agent_names_its_owner_and_its_device(
    ui: UI, hosted: Hosted, options: dict[str, Any]
) -> None:
    """#496: the Inspector says whose the agent is and which device it runs on, by display name, with
    one person on the switchboard (Members says nothing then), in the light and dark schemes and on a
    phone, where the line stays inside the sheet and is readable. Fails if the Inspector has no owner
    line, names the device but not the person, shows an email, or if the text can't be read in a scheme."""
    hosted.agent("claude-1", "alices-pi", None)
    page = open_page(ui, hosted, **options)
    phone = "is_mobile" in options
    members_open(page, phone)
    expect(row(page, "claude-1")).to_be_visible()
    expect(page.locator("#buddy-list .m-owner")).to_have_count(0)  # one person: nothing to tell apart
    with page.expect_response(lambda r: "/members/claude-1" in r.url and r.request.method == "GET"):
        row(page, "claude-1").click()
    owner = page.locator("#insp-owner")
    expect(owner).to_have_text("Owner: alice · device alices-pi")
    expect(owner).to_be_visible()
    assert "@" not in (owner.text_content() or "")
    inside = owner.evaluate(
        "el => { const r = el.getBoundingClientRect(), p = el.closest('#inspector').getBoundingClientRect();"
        " return r.left >= p.left && r.right <= p.right; }"
    )
    assert inside, "the owner line runs out of the Inspector"
    colors = owner.evaluate(
        """el => {
          let n = el, bg = 'rgba(0, 0, 0, 0)';  // the first opaque background behind the text
          while (n && /rgba\\(.*, 0\\)$/.test(bg)) { bg = getComputedStyle(n).backgroundColor; n = n.parentElement; }
          return {fg: getComputedStyle(el).color, strong: getComputedStyle(el.querySelector('strong')).color, bg};
        }"""
    )
    assert contrast(colors["fg"], colors["bg"]) >= 4.5, colors
    assert contrast(colors["strong"], colors["bg"]) >= 4.5, colors
    if phone:
        width = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert width <= 0


@pytest.mark.parametrize("phone", [False, True], ids=["desktop", "phone"])
def test_members_name_the_owner_only_with_more_than_one_person(ui: UI, hosted: Hosted, phone: bool) -> None:
    """#496: Members has an owner line under each agent's name once the switchboard has a second person,
    and not before: with one person it would only say "alice" under every agent. It follows the people
    as they come and go, on an open page, and never shows an email (bob's is ``bob@example.com``).
    Fails if the line shows with one person, doesn't appear when bob arrives, names the wrong person,
    shows an email, or stays after bob is removed."""
    hosted.agent("claude-1", "alices-pi", None)
    page = open_page(ui, hosted, **(PHONE if phone else {}))
    members_open(page, phone)
    expect(row(page, "claude-1")).to_be_visible()
    expect(page.locator("#buddy-list .m-owner")).to_have_count(0)
    bob = hosted.person("bob")
    expect(row(page, "claude-1").locator(".m-owner")).to_have_text("Owner: alice")  # bob's arrival shows it
    hosted.agent("codex-1", "bobs-laptop", bob)
    expect(row(page, "codex-1").locator(".m-owner")).to_have_text("Owner: bob")
    listed = page.locator("#buddy-list").text_content() or ""
    assert "@example.com" not in listed and "bob@" not in listed, listed
    # under the agent's name: its owner comes before its status line
    order = row(page, "codex-1").locator(".m-main").evaluate(
        "el => Array.from(el.children).map(c => c.className.split(' ')[0])"
    )
    assert order.index("m-name-line") < order.index("m-owner") < order.index("m-status"), order
    if phone:
        width = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert width <= 0
    hosted.remove(bob)  # a person goes, and with their agent the second person's reason to say whose is whose
    expect(page.locator("#buddy-list .m-owner")).to_have_count(0)
    expect(row(page, "claude-1")).to_be_visible()


def test_an_agent_whose_owner_cant_be_worked_out_shows_none(ui: UI, hosted: Hosted) -> None:
    """#496, fail closed: an agent on a device no live token names has no owner in Members (with two
    people there) or in the Inspector, and nothing in its place; its device is still shown. Fails if
    either invents an owner, or leaves a bare "Owner:" label."""
    bob = hosted.person("bob")
    hosted.agent("claude-1", "alices-pi", None)
    hosted.agent("codex-1", "bobs-laptop", bob)
    hosted.agent("devin-1", "orphan-pi", None, token=False)
    page = open_page(ui, hosted)
    expect(row(page, "codex-1").locator(".m-owner")).to_have_text("Owner: bob")
    expect(row(page, "claude-1").locator(".m-owner")).to_have_text("Owner: alice")
    expect(row(page, "devin-1")).to_be_visible()
    expect(row(page, "devin-1").locator(".m-owner")).to_have_count(0)
    with page.expect_response(lambda r: "/members/devin-1" in r.url and r.request.method == "GET"):
        row(page, "devin-1").click()
    expect(page.locator("#insp-name")).to_contain_text("devin-1")
    expect(page.locator("#insp-owner")).to_have_count(0)
    expect(page.locator("#inspector .insp-id")).to_contain_text("orphan-pi")  # the device chip is still there
    assert "Owner" not in (page.locator("#inspector .insp-id").text_content() or "")
