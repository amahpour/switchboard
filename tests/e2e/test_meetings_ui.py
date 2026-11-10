"""A live call in the room (#573): the bar, Leave call, and a transcript line that is not a chat row.

The first test's broker is seeded with a live meeting and one transcript line. No joinly runs: the
page reads the room's settings and its history. The second (#593) starts a call through the real
command against a scripted joinly, so the bar's voice switch and Snapshot button reach it. A console
error, a page error or a CSP violation fails the test, through the watcher in ``test_web_ui.UI``.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from fakes.fake_joinly import FakeJoinly
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHASE_REPORTS, PHONE, UI

from switchboard.config import Config, MeetingCfg
from switchboard.meeting import keys

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def calls(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    """A desktop broker in a temp home. ``playwright`` starts first, under the real HOME."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        broker = InProcBroker(home).start()
        try:
            yield broker
        finally:
            broker.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture(scope="module")
def voice_call(
    playwright: Any, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[InProcBroker, FakeJoinly]]:
    """A desktop broker that knows a scripted joinly, so ``/meeting <link>`` really joins (#593)."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        joinly = FakeJoinly().start()
        cfg = Config(human_name="alice").replace(meeting=MeetingCfg(joinly_url=joinly.url))
        broker = InProcBroker(home, cfg).start()
        try:
            yield broker, joinly
        finally:
            broker.stop()
            joinly.stop()
            shutil.rmtree(home, ignore_errors=True)


KEY = "sbc_" + hashlib.sha256(b"an example key").hexdigest()  # only the hash of it is ever kept
HASH = keys.key_hash(KEY)


@pytest.fixture(scope="module")
def hosting(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    """A desktop broker for the calls hosted on a person's machine (#623). Its own, so ending the call here
    does not touch the other tests' calls."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        broker = InProcBroker(home).start()
        try:
            yield broker
        finally:
            broker.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def ui(browser: Browser, calls: InProcBroker, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, None)  # type: ignore[arg-type]
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def _seed(broker: InProcBroker) -> None:
    headers = broker.write_headers()
    client = broker.web_client()
    try:
        made = client.post("/api/rooms", json={"name": "#calls"}, headers=headers)
        assert made.status_code == 200, made.text
        said = client.post("/api/rooms/calls/say", json={"text": "a note from the room"}, headers=headers)
        assert said.status_code == 200, said.text
    finally:
        client.close()

    def plant() -> None:
        room = broker.state.store.get_room("#calls")
        assert room is not None
        row = broker.state.store.insert_meeting(room.id, started_by="alice", platform="zoom")
        broker.state.store.set_meeting_live(row.id)
        broker.state.store.insert_message(
            room.id,
            sender_name="bob",
            sender_kind="meeting",
            via="meeting",
            text="Codex, how much is gas in Los Angeles?",
        )

    broker.on_loop(plant)


def _open(ui: UI, broker: InProcBroker, *, room: str = "calls", **options: Any) -> Page:
    page = ui.context(**options).new_page()
    page.goto(broker.login_url())
    expect(page.locator("#st-conn")).to_have_text("Connected")
    tab = page.locator(f'#tabs .room[data-room="#{room}"]')
    if not tab.is_visible():
        page.click("#rooms-toggle")
    tab.click()
    expect(page.locator("#room-title")).to_have_text(room)
    return page


def test_a_live_call_shows_a_bar_a_leave_button_and_a_smaller_transcript_line(
    ui: UI, calls: InProcBroker
) -> None:
    """The bar names the platform, Leave call ends it, and a transcript line is tagged and smaller.

    Light, dark and a phone width all show the bar. The line carries a call tag and the speaker,
    and it is set smaller than a person's message.
    """
    _seed(calls)
    page = _open(ui, calls)
    bar = page.locator("#banner-meeting")
    expect(bar).to_be_visible()
    expect(bar).to_contain_text("Transcribing a call (Zoom)")
    leave = page.locator("#meeting-leave")
    expect(leave).to_have_text("Leave call")
    expect(leave).to_be_visible()

    line = page.locator("#log .line.k-meeting")
    expect(line).to_be_visible()
    expect(line.locator(".call-tag")).to_have_text("call")
    expect(line.locator(".nick-meeting")).to_have_text("bob")
    expect(line).to_contain_text("how much is gas")
    expect(line.locator(".avatar")).to_have_count(0)
    chat = page.locator("#log .line.k-chat:not(.k-meeting)")
    expect(chat).to_be_visible()
    meet_size = line.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
    chat_size = chat.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
    assert meet_size < chat_size

    light = bar.evaluate("el => getComputedStyle(el).backgroundColor")
    page.emulate_media(color_scheme="dark")
    expect(bar).to_contain_text("Transcribing a call (Zoom)")
    dark = bar.evaluate("el => getComputedStyle(el).backgroundColor")
    assert light != dark

    phone = _open(ui, calls, **PHONE)
    phone_bar = phone.locator("#banner-meeting")
    expect(phone_bar).to_be_visible()
    expect(phone_bar).to_contain_text("Transcribing a call (Zoom)")
    expect(phone.locator("#meeting-leave")).to_be_visible()
    expect(phone.locator("#log .line.k-meeting .call-tag")).to_have_text("call")
    # The line's text uses the row's width on a phone, not the 28 px avatar column a chat
    # row reserves (the first build wrapped one word per line there).
    width = "el => el.getBoundingClientRect().width"
    body_w = phone.locator("#log .line.k-meeting .msg-body").evaluate(width)
    row_w = phone.locator("#log .line.k-meeting").evaluate(width)
    assert body_w > row_w * 0.7, (body_w, row_w)
    fits = phone.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )
    assert fits

    leave.click()
    expect(page.locator("#input")).to_be_focused()
    expect(bar).to_be_hidden()


def _start_call(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """A room named voice and a Meet call in it, through the same command a person types."""
    headers = broker.write_headers()
    client = broker.web_client()
    try:
        made = client.post("/api/rooms", json={"name": "#voice"}, headers=headers)
        assert made.status_code == 200, made.text
        sent = client.post(
            "/api/rooms/voice/command",
            json={"text": "/meeting https://meet.google.com/abc-defg-hij"},
            headers=headers,
        )
        assert sent.status_code == 200, sent.text
    finally:
        client.close()


def _track(page: Page) -> str:
    return page.locator("#meeting-voice .switch-track").evaluate("el => getComputedStyle(el).backgroundColor")


def test_the_call_bar_has_a_voice_switch_and_a_snapshot_button(
    ui: UI, voice_call: tuple[InProcBroker, FakeJoinly]
) -> None:
    """The bar holds a Voice switch (off at the start), Snapshot and Leave call. The switch turns
    voice on and off through the same command a person types, says so in the bar and the room, and
    works from the keyboard. Snapshot posts the shared screen as a card. All three fit a phone, and
    the switch's colour differs on and off in light and dark. Fails if the switch starts on, its state is
    not in ``aria-checked`` and the bar's words, a button is cut off at phone width, or Snapshot posts
    nothing."""
    broker, joinly = voice_call
    _start_call(broker, joinly)
    page = _open(ui, broker, room="voice")
    bar = page.locator("#banner-meeting")
    expect(bar).to_contain_text("Transcribing a call (Meet)")
    switch = page.locator("#meeting-voice")
    snapshot = page.locator("#meeting-snapshot")
    expect(switch).to_have_attribute("role", "switch")
    expect(switch).to_have_attribute("aria-checked", "false")
    expect(switch).to_have_text("Voice")
    expect(bar).not_to_contain_text("agents can speak")
    expect(snapshot).to_be_enabled()
    off_light = _track(page)

    switch.click()
    expect(switch).to_have_attribute("aria-checked", "true")
    expect(bar).to_contain_text("agents can speak")
    expect(page.locator("#log")).to_contain_text("alice turned voice on")
    # The track eases to its new colour over 120 ms (style.css). A read made before the transition's first
    # frame finds the colour it left, which made this test fail one run in forty: wait until it has moved.
    expect(page.locator("#meeting-voice .switch-track")).not_to_have_css("background-color", off_light)
    on_light = _track(page)
    assert on_light != off_light

    page.emulate_media(color_scheme="dark")
    expect(switch).to_be_visible()
    on_dark = _track(page)
    assert on_dark and on_dark != "rgba(0, 0, 0, 0)"

    phone = _open(ui, broker, room="voice", **PHONE)
    for button in ("#meeting-voice", "#meeting-snapshot", "#meeting-leave"):
        expect(phone.locator(button)).to_be_visible()
    inside = phone.evaluate(
        """() => ['meeting-voice', 'meeting-snapshot', 'meeting-leave'].every(id => {
          const r = document.getElementById(id).getBoundingClientRect();
          return r.left >= 0 && r.right <= document.documentElement.clientWidth;
        }) && document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"""
    )
    assert inside
    expect(phone.locator("#meeting-voice")).to_have_attribute("aria-checked", "true")

    switch.focus()
    page.keyboard.press("Space")
    expect(switch).to_have_attribute("aria-checked", "false")
    expect(bar).not_to_contain_text("agents can speak")
    expect(page.locator("#log")).to_contain_text("alice turned voice off")

    snapshot.click()
    expect(page.locator("#log .md-artifact a.md-file")).to_be_visible()
    expect(page.locator("#log .md-artifact .md-artifact-name")).to_contain_text("screen-")
    assert joinly.snapshots == 1


def _seed_hosted(broker: InProcBroker) -> None:
    """A room with a live call hosted on alice's laptop and one line that machine pushed: the rows the
    broker keeps for a call whose relay is in it, planted as the other tests plant theirs."""
    headers = broker.write_headers()
    client = broker.web_client()
    try:
        made = client.post("/api/rooms", json={"name": "#hosted"}, headers=headers)
        assert made.status_code == 200, made.text
    finally:
        client.close()

    def plant() -> None:
        store = broker.state.store
        room = store.get_room("#hosted")
        assert room is not None
        token = store.agent_token_add(None, "alice-laptop", hashlib.sha256(b"a token").hexdigest())
        assert token is not None
        row = store.insert_hosted_meeting(
            room.id,
            started_by="alice",
            platform="zoom",
            key_hash=HASH,
            token_id=token.id,
            owner_person_id=None,
            host="alice-laptop",
            model="large-v3-turbo",
        )
        store.set_meeting_live(row.id)
        store.insert_message(
            room.id,
            sender_name="bob",
            sender_kind="meeting",
            via="meeting",
            text="Claude one, can you check the upload limit?",
            sender_host="alice-laptop",
            meeting_id=row.id,
        )

    broker.on_loop(plant)


def test_a_hosted_call_names_the_machine_and_its_key_and_end_call_refuses_the_next_push(
    ui: UI, hosting: InProcBroker
) -> None:
    """The bar for a call hosted on a person's machine says whose machine and the key's fingerprint, and its
    button is End call (a hosted call's key ends with it). A line the machine pushed says "via" that machine
    beside the speaker. Light, dark and a phone all show both, with nothing cut off or scrolling sideways.
    End call ends the call, and the next push with its key is refused. Fails if the host or the fingerprint
    is not in the bar, the line has no machine, the snapshot button is on for a bot that sends none, a phone
    scrolls sideways, or the key still works after End call."""
    _seed_hosted(hosting)
    fingerprint = keys.fingerprint(HASH)
    page = _open(ui, hosting, room="hosted")
    bar = page.locator("#banner-meeting")
    expect(bar).to_be_visible()
    expect(page.locator("#banner-meeting-text")).to_have_text(
        f"Transcribing a call (Zoom) · on alice-laptop · key {fingerprint}"
    )
    end = page.locator("#meeting-leave")
    expect(end).to_have_text("End call")
    expect(page.locator("#meeting-snapshot")).to_be_disabled()
    expect(page.locator("#meeting-voice")).to_have_attribute("aria-checked", "false")

    line = page.locator("#log .line.k-meeting")
    expect(line.locator(".nick-meeting")).to_have_text("bob")
    expect(line.locator(".via-host")).to_have_text("via alice-laptop")
    expect(line).to_contain_text("upload limit")
    colour = line.locator(".via-host").evaluate("el => getComputedStyle(el).color")
    page.emulate_media(color_scheme="dark")
    expect(bar).to_contain_text(f"key {fingerprint}")
    expect(line.locator(".via-host")).to_be_visible()
    assert line.locator(".via-host").evaluate("el => getComputedStyle(el).color") != colour

    phone = _open(ui, hosting, room="hosted", **PHONE)
    expect(phone.locator("#banner-meeting")).to_contain_text(f"on alice-laptop · key {fingerprint}")
    expect(phone.locator("#log .line.k-meeting .via-host")).to_have_text("via alice-laptop")
    inside = phone.evaluate(
        """() => ['meeting-voice', 'meeting-snapshot', 'meeting-leave'].every(id => {
          const r = document.getElementById(id).getBoundingClientRect();
          return r.left >= 0 && r.right <= document.documentElement.clientWidth;
        }) && document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"""
    )
    assert inside

    relay = httpx.Client(base_url=hosting.base, headers={"authorization": f"Bearer {KEY}"})
    try:
        assert relay.post("/mcp/meeting/push", json={"segments": []}).status_code == 200
        end.click()
        expect(page.locator("#input")).to_be_focused()
        expect(bar).to_be_hidden()
        refused = relay.post("/mcp/meeting/push", json={"segments": []})
        assert (refused.status_code, refused.json()["error"]) == (410, "ended")
    finally:
        relay.close()
    expect(line.locator(".via-host")).to_have_text("via alice-laptop")  # what it pushed keeps its label


def _post(broker: InProcBroker, path: str, body: dict[str, str]) -> httpx.Response:
    client = broker.web_client()
    try:
        return client.post(path, json=body, headers=broker.write_headers())
    finally:
        client.close()


def _plant_agents(broker: InProcBroker, room: str, modes: dict[str, str]) -> None:
    """Agents in a room, as the other call tests plant them: a participant and a membership each, with the
    approval mode the participant reports, then the Members frame published."""
    from switchboard.models import session_key

    def plant() -> None:
        st = broker.state
        row = st.store.get_room(room)
        for name, mode in modes.items():
            p = st.store.upsert_participant(
                "claude", session_key("claude", "alice-laptop", f"{room}-{name}"), host="alice-laptop"
            )
            st.store.update_participant(p.id, approval_mode=mode)
            st.store.create_membership(row.id, p.id, name)
        st.hub.members_changed(room)

    broker.on_loop(plant)


def test_the_call_bar_turns_an_agents_listening_on_and_off(ui: UI, calls: InProcBroker) -> None:
    """Each agent has a Listening switch in the call bar. It is named for its agent, reachable by Tab, and
    sends the person's own /meeting listen command: on shows "listening" in Members and a Listening chip in
    the Inspector (with the approvals warning for an agent whose approvals are off), and off takes them away.
    Nothing is cut off on a phone. Fails if the switch is missing, ignores the keyboard, leaves the marker
    after off, shows the bar's row with no call, or the warning is missing for approvals off."""
    broker = calls
    for name in ("#hearing", "#nocall"):
        assert _post(broker, "/api/rooms", {"name": name}).status_code == 200
    _plant_agents(broker, "#hearing", {"claude-1": "prompting", "codex-1": "bypass"})
    _plant_agents(broker, "#nocall", {"claude-1": "prompting"})

    page = _open(ui, broker, room="nocall")
    expect(page.locator("#banner-meeting")).to_be_hidden()
    expect(page.locator("#meeting-listen")).to_be_hidden()

    def start_call() -> None:
        room = broker.state.store.get_room("#hearing")
        assert room is not None
        row = broker.state.store.insert_meeting(room.id, started_by="alice", platform="meet")
        broker.state.store.set_meeting_live(row.id)
        broker.state.service.publish_settings(room.id)

    broker.on_loop(start_call)
    page = _open(ui, broker, room="hearing")
    claude = page.get_by_role("switch", name="Listening: claude-1")
    codex = page.get_by_role("switch", name="Listening: codex-1")
    expect(claude).to_have_attribute("aria-checked", "false")
    expect(codex).to_have_attribute("aria-checked", "false")
    expect(page.locator(".m-status", has_text="listening")).to_have_count(0)

    claude.click()
    expect(claude).to_have_attribute("aria-checked", "true")
    expect(page.locator("#log")).to_contain_text("alice started claude-1 listening to the call")
    row = page.locator("#buddy-list .member", has=page.locator(".m-name", has_text="claude-1"))
    expect(row.locator(".m-status")).to_contain_text("listening")
    expect(
        page.locator("#buddy-list .member", has=page.locator(".m-name", has_text="codex-1")).locator(
            ".m-status"
        )
    ).not_to_contain_text("listening")

    # the keyboard: Tab reaches the switch, a ring shows, Space flips it; its words are in the page's own
    claude.focus()
    page.keyboard.press("Tab")
    expect(codex).to_be_focused()
    outline = codex.evaluate("el => getComputedStyle(el).outlineStyle")
    assert outline == "solid", outline
    page.keyboard.press("Space")
    expect(codex).to_have_attribute("aria-checked", "true")
    expect(page.locator("#log")).to_contain_text("approvals off")
    expect(codex).to_be_focused()  # the frame that follows keeps the focus where the person is

    # the Inspector: the chip, and the warning for approvals off
    page.locator("#buddy-list .member", has=page.locator(".m-name", has_text="codex-1")).locator(
        ".m-name"
    ).click()
    expect(page.locator("#insp-name")).to_contain_text("codex-1")
    expect(page.locator(".insp-chips .chip-listening")).to_have_text("Listening")
    expect(page.locator(".insp-attn")).to_contain_text("Listening with approvals off")

    # off takes the marker away, in Members and in the Inspector
    codex.click()
    expect(codex).to_have_attribute("aria-checked", "false")
    expect(page.locator(".insp-chips .chip-listening")).to_have_count(0)
    expect(page.locator(".insp-attn")).not_to_contain_text("Listening with approvals")
    claude.click()
    expect(claude).to_have_attribute("aria-checked", "false")
    expect(page.locator(".m-status", has_text="listening")).to_have_count(0)

    # a phone: every switch is inside the screen, with a name that says which agent
    phone = _open(ui, broker, room="hearing", **PHONE)
    for who in ("claude-1", "codex-1"):
        expect(phone.get_by_role("switch", name=f"Listening: {who}")).to_be_visible()
    inside = phone.evaluate(
        """() => [...document.querySelectorAll('.meeting-listen-btn')].every(b => {
          const r = b.getBoundingClientRect();
          return r.left >= 0 && r.right <= document.documentElement.clientWidth;
        }) && document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"""
    )
    assert inside
