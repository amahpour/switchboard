"""A call as its own transcript card in the room (#771, DESIGN.md §52): the page groups a stretch of call
lines into one card, a chat message ends it, an agent's ``@call`` line and a snapshot sit inside it, and
the last card says when the call ended. The rows are the same messages with the same ids; only the page's
drawing changes.

Lines are planted straight into the store with the timestamps a test names (the page reads history from
the API), and live lines go through the service, as the broker posts them. Any console error, page error
or CSP violation fails the test, through the watcher in ``test_web_ui.UI``. Marker ``e2e``."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from playwright.sync_api import Page
from test_meetings_ui import (
    PHONE,
    UI,
    FakeJoinly,
    InProcBroker,
    _open,
    _plant_agents,
    _post,
    _start_call,
    expect,
    pytest,
)
from test_meetings_ui import calls as calls  # noqa: F401  (a fixture)
from test_meetings_ui import ui as ui  # noqa: F401  (a fixture)
from test_meetings_ui import voice_call as voice_call  # noqa: F401  (a fixture)
from test_web_ui import no_horizontal_scroll

pytestmark = pytest.mark.e2e

CLAUDE = "rgb(185, 87, 58)"  # --h-claude-bg, the colour an agent's spoken row takes


def _base() -> float:
    """Yesterday at noon, local time: every planted line is on one day, so no day divider splits a card."""
    noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    return noon.timestamp()


def _plant(
    broker: InProcBroker,
    room: str,
    rows: list[tuple[str, str, str, float]],
    *,
    call: str | None = "zoom",
) -> dict[str, int]:
    """A room with ``rows`` of (kind, name, text, seconds after the base) and, when ``call`` names a platform,
    a live call. Kinds: ``meeting`` (a call line), ``human`` and ``agent`` (chat). Returns each row's id by
    its text."""
    assert _post(broker, "/api/rooms", {"name": room}).status_code == 200
    ids: dict[str, int] = {}
    base = _base()  # read once: a run that crosses midnight must not move later rows to another day

    def plant() -> None:
        store = broker.state.store
        r = store.get_room(room)
        assert r is not None
        if call:
            row = store.insert_meeting(r.id, started_by="alice", platform=call)
            store.set_meeting_live(row.id)
        for kind, name, text, at in rows:
            m = store.insert_message(
                r.id,
                sender_name=name,
                sender_kind=kind,
                via={"human": "web", "agent": "mcp"}.get(kind, "meeting"),
                text=text,
                sender_harness="claude" if kind == "agent" else None,
            )
            store.con.execute("UPDATE messages SET ts=? WHERE id=?", (base + at, m.id))
            ids[text] = m.id
        broker.state.service.publish_settings(r.id)

    broker.on_loop(plant)
    return ids


class _Held:
    """A clock that reads one moment, a second later at each look, whatever the wall clock says."""

    def __init__(self, at: float) -> None:
        self.at = at

    def now(self) -> float:
        self.at += 1.0
        return self.at


def test_consecutive_call_lines_make_one_card_and_a_chat_message_ends_it(
    ui: UI,
    calls: InProcBroker,  # noqa: F811
) -> None:
    """Two call lines in a row are one card, a person's chat message ends it, and the next call line starts
    another: a card, a chat row and a card, in that order. The live call heads only the last card (its
    platform, "live", Voice, the lines so far); the earlier one is just its lines. A row is compact: the
    speaker, the text and the time, with no avatar and no host, and its id (#N) appears on hover while the
    row still carries it as data-id for links. Fails if the lines stay separate chat rows, the chat
    message does not break the card, a card that is not the call's latest is marked live, or a row shows
    an avatar."""
    ids = _plant(
        calls,
        "#cards",
        [
            ("meeting", "bob", "first thing said", 0),
            ("meeting", "alice", "second thing said", 60),
            ("human", "alice", "noting that down", 90),
            ("meeting", "bob", "third thing said", 120),
            ("meeting", "bob", "fourth thing said", 240),
        ],
    )
    page = _open(ui, calls, room="cards")
    cards = page.locator("#log > .call-card")
    expect(cards).to_have_count(2)
    expect(page.locator("#log > .call-card, #log > .line.k-chat")).to_have_count(3)
    kinds = page.locator("#log > .call-card, #log > .line.k-chat").evaluate_all(
        "els => els.map(e => e.classList.contains('call-card') ? 'card'"
        " : e.classList.contains('k-chat') ? 'chat' : '?')"
    )
    assert kinds == ["card", "chat", "card"], kinds

    first, last = cards.nth(0), cards.nth(1)
    expect(first.locator(".call-row")).to_have_count(2)
    expect(last.locator(".call-row")).to_have_count(2)
    expect(first.locator(".call-count")).to_have_text("2 lines")
    expect(first.locator(".call-live")).to_have_count(0)
    expect(first.locator(".call-end")).to_have_count(0)
    expect(last.locator(".call-title")).to_have_text("Zoom call")
    expect(last.locator(".call-live")).to_have_text("live")
    expect(last.locator(".call-voice")).to_have_text("Voice off")
    expect(last.locator(".call-count")).to_have_text("2 lines")
    expect(last).to_have_class(re.compile("is-live"))
    expect(page.locator("#log .call-card.is-live")).to_have_count(1)

    row = first.locator(".call-row").first
    expect(row).to_have_attribute("data-id", str(ids["first thing said"]))
    expect(row.locator(".call-who")).to_have_text("bob")
    expect(row).to_contain_text("first thing said")
    expect(row.locator(".avatar")).to_have_count(0)
    expect(row.locator("time")).to_be_visible()
    expect(row.locator(".call-id")).to_have_text(f"#{ids['first thing said']}")
    expect(row.locator(".call-id")).to_have_css("opacity", "0")
    row.hover()
    expect(row.locator(".call-id")).to_have_css("opacity", "1")
    expect(page.locator(".call-tag, .via-host")).to_have_count(0)  # the old per-row tag and host are gone
    chat = page.locator("#log > .line.k-chat")
    expect(chat).to_have_attribute("data-id", str(ids["noting that down"]))
    expect(chat.locator(".avatar")).to_have_count(1)  # a chat message is still a chat message


def test_live_call_lines_join_the_last_card_until_a_chat_message_ends_it(
    request: pytest.FixtureRequest,
    ui: UI,
    calls: InProcBroker,  # noqa: F811
) -> None:
    """Lines that arrive while the page is open: the second joins the first's card and the count follows, a
    chat message that arrives ends it, and the next line is a card of its own, the only one that is
    live. Fails if a new line starts a card of its own while one is open, or a line after a chat
    message joins the old card."""
    _plant(calls, "#livecards", [])
    page = _open(ui, calls, room="livecards")
    # Every line posted below is stamped by the store's clock. Held at one moment, a run that crosses midnight
    # can't put a day divider (and so a new card) between two of them.
    real = calls.state.store.clock
    request.addfinalizer(lambda: calls.on_loop(lambda: setattr(calls.state.store, "clock", real)))
    calls.on_loop(lambda: setattr(calls.state.store, "clock", _Held(_base() + 3600)))

    def say(text: str) -> None:
        def post() -> None:
            room = calls.state.store.get_room("#livecards")
            assert room is not None
            calls.state.service.post_meeting_line(room.id, sender_name="bob", text=text, mentions=[])

        calls.on_loop(post)

    say("one said")
    say("two said")
    cards = page.locator("#log > .call-card")
    expect(cards).to_have_count(1)
    expect(cards.first.locator(".call-row")).to_have_count(2)
    expect(cards.first.locator(".call-count")).to_have_text("2 lines")
    assert _post(calls, "/api/rooms/livecards/say", {"text": "back to the room"}).status_code == 200
    expect(page.locator("#log > .line.k-chat")).to_have_count(1)
    say("three said")
    expect(cards).to_have_count(2)
    expect(cards.nth(1).locator(".call-row")).to_have_count(1)
    expect(cards.first.locator(".call-live")).to_have_count(0)
    expect(page.locator("#log .call-card.is-live")).to_have_count(1)
    expect(cards.nth(1)).to_have_class(re.compile("is-live"))


def test_the_last_card_says_when_the_call_ended_and_for_how_long(
    ui: UI,
    calls: InProcBroker,  # noqa: F811
) -> None:
    """Ending the call (Leave call) takes "live" and the Voice mark off the last card and closes it with
    "The call ended. 2 lines, 2 minutes." (the length of its own lines). A page opened afterwards shows the
    same closing line, and an earlier card, cut by a chat message, has none. Fails if the card stays live, the
    closing line is missing or counts the wrong lines, or the earlier card is closed too."""
    _plant(
        calls,
        "#ending",
        [
            ("meeting", "bob", "early one", 0),
            ("human", "alice", "a word in the room", 30),
            ("meeting", "bob", "late one", 120),
            ("meeting", "alice", "later one", 240),
        ],
    )
    page = _open(ui, calls, room="ending")
    last = page.locator("#log > .call-card").last
    expect(last.locator(".call-live")).to_have_text("live")
    expect(last.locator(".call-end")).to_have_count(0)
    page.click("#meeting-leave")
    expect(page.locator("#banner-meeting")).to_be_hidden()
    expect(last.locator(".call-live")).to_have_count(0)
    expect(last.locator(".call-voice")).to_have_count(0)
    expect(last.locator(".call-end")).to_have_text("The call ended. 2 lines, 2 minutes.")
    expect(page.locator("#log .call-end")).to_have_count(1)
    expect(page.locator("#log > .call-card").first.locator(".call-end")).to_have_count(0)
    # the ended card is named by the call it was
    expect(last.locator(".call-title")).to_have_text("Zoom call")

    again = _open(ui, calls, room="ending")
    expect(again.locator("#log .call-end")).to_have_text("The call ended. 2 lines, 2 minutes.")
    expect(again.locator("#log .call-live")).to_have_count(0)
    expect(again.locator("#log .call-card").last.locator(".call-title")).to_have_text(
        "Call"
    )  # not known to a fresh page


def test_an_agents_at_call_line_is_a_spoken_row_in_its_colour_and_stays_in_the_chat(
    ui: UI,
    calls: InProcBroker,  # noqa: F811
) -> None:
    """An agent's ``@call`` message after a call line shows in the card as a row marked "spoken", in the
    agent's colour (light and dark), without the "@call" and cut to what is spoken; the whole
    message is still in the chat below the card, and it is not counted as a transcript line. An
    ordinary agent message after the card gets no row. A live ``@call`` while Voice is off makes no
    spoken row, since nothing was said aloud. Fails if the row is missing or in no colour, the chat
    message is gone, the line count includes it, or a message without ``@call`` has a spoken row."""
    ids = _plant(
        calls,
        "#spoken",
        [
            ("meeting", "bob", "claude one, how is the build", 0),
            (
                "agent",
                "claude-1",
                "@call It is green. Details are in the chat; the second run took four minutes.",
                30,
            ),
            ("meeting", "bob", "thanks", 60),
            ("agent", "claude-1", "plain message without the call word", 90),
        ],
    )
    _plant_agents(calls, "#spoken", {"claude-1": "prompting"})
    page = _open(ui, calls, room="spoken")
    spoken = page.locator("#log .call-card .call-spoken")
    expect(spoken).to_have_count(1)
    expect(spoken).to_have_attribute(
        "data-id", str(ids["@call It is green. Details are in the chat; the second run took four minutes."])
    )
    expect(spoken.locator(".call-spoken-tag")).to_have_text("spoken")
    expect(spoken.locator(".call-who")).to_have_text("claude-1")
    expect(spoken.locator(".call-said")).to_have_text(
        "It is green. Details are in the chat; the second run took four minutes."
    )
    expect(spoken.locator(".call-who")).to_have_css("color", CLAUDE)
    expect(spoken).to_have_css("box-shadow", re.compile(re.escape(CLAUDE)))
    # the card holds the two transcript lines, not the agent's reply; the chat keeps every word
    expect(page.locator("#log .call-card").first.locator(".call-count")).to_have_text("1 line")
    chat_rows = page.locator("#log > .line.k-chat")
    expect(chat_rows).to_have_count(2)
    expect(chat_rows.first).to_contain_text("@call It is green")
    expect(page.locator("#log .call-card .call-spoken")).to_have_count(1)  # the plain message got none
    page.emulate_media(color_scheme="dark")
    expect(spoken.locator(".call-who")).to_have_css("color", CLAUDE)

    # live, with Voice off: the agent's @call is chat only
    def live_call() -> None:
        room = calls.state.store.get_room("#spoken")
        assert room is not None
        member = calls.state.store.find_member(room.id, "claude-1")
        calls.state.service._post(
            room,
            sender_name="claude-1",
            sender_kind="agent",
            sender_harness="claude",
            sender_membership_id=member.membership_id,
            via="mcp",
            text="@call a second thing, but Voice is off",
            mentions=[],
        )

    calls.on_loop(live_call)
    expect(page.locator("#log")).to_contain_text("a second thing, but Voice is off")
    expect(page.locator("#log .call-spoken")).to_have_count(1)


def test_with_voice_on_a_live_at_call_line_joins_the_card_as_it_is_posted(
    ui: UI,
    voice_call: tuple[InProcBroker, FakeJoinly],  # noqa: F811
) -> None:
    """Turning Voice on in the bar makes an agent's next live ``@call`` line a spoken row in the call's
    card (a new card when no call line is just above), headed "Voice on"; the message is also in the
    chat. A snapshot of the shared screen posted during the call sits in the card as its file card.
    Fails if the row waits for a reload, the header still says Voice off, or the snapshot is a chat
    row outside the card."""
    broker, _joinly = voice_call
    _start_call(broker, _joinly)
    _plant_agents(broker, "#voice", {"claude-1": "prompting"})
    page = _open(ui, broker, room="voice")
    page.click("#meeting-voice")
    expect(page.locator("#meeting-voice")).to_have_attribute("aria-checked", "true")

    def agent_call() -> None:
        room = broker.state.store.get_room("#voice")
        assert room is not None
        member = broker.state.store.find_member(room.id, "claude-1")
        broker.state.service._post(
            room,
            sender_name="claude-1",
            sender_kind="agent",
            sender_harness="claude",
            sender_membership_id=member.membership_id,
            via="mcp",
            text="@call The deploy is done.",
            mentions=[],
        )

    broker.on_loop(agent_call)
    spoken = page.locator("#log .call-card .call-spoken")
    expect(spoken).to_have_count(1)
    expect(spoken.locator(".call-said")).to_have_text("The deploy is done.")
    expect(page.locator("#log .call-card .call-voice")).to_have_text("Voice on")
    expect(page.locator("#log > .line.k-chat")).to_contain_text("@call The deploy is done.")

    page.click("#meeting-snapshot")
    expect(page.locator("#log .call-card .md-artifact a.md-file")).to_be_visible()
    expect(page.locator("#log > .line.k-chat .md-artifact")).to_have_count(0)


def test_a_search_hit_on_a_call_line_opens_at_that_line_inside_its_card(
    ui: UI,
    calls: InProcBroker,  # noqa: F811
) -> None:
    """Opening a search result that is a call line shows the room around it with the line marked and in view,
    inside its card, and the card is not drawn as live or ended (a window is not the room's end). Fails if the
    line is not in a card, is not the marked one, or the window claims the call is live."""
    ids = _plant(
        calls,
        "#findme",
        [
            ("meeting", "bob", "the zebra migration needs a rollback plan", 0),
            ("meeting", "alice", "agreed on the plan", 30),
        ],
    )
    page = _open(ui, calls, room="cards")
    box = page.locator("#search-input")
    box.click()
    box.fill("zebra migration")
    hit = page.locator("#search-results .search-hit")
    expect(hit).to_have_count(1)
    hit.first.click()
    expect(page.locator("#room-title")).to_have_text("findme")
    target = page.locator(
        f'#log .call-card .line[data-id="{ids["the zebra migration needs a rollback plan"]}"]'
    )
    expect(target).to_have_class(re.compile("search-target"))
    expect(target).to_be_in_viewport()
    expect(page.locator("#log .call-card.is-live, #log .call-end")).to_have_count(0)
    page.click("#around-latest")
    expect(page.locator("#log .call-card.is-live")).to_have_count(1)


def test_cards_fit_a_phone_and_read_in_both_themes(
    ui: UI,
    calls: InProcBroker,  # noqa: F811
) -> None:
    """On a 390 px phone a card and its rows stay inside the screen with the text on its own line under the
    speaker and time, nothing scrolls sideways, and a long unbroken word wraps instead of pushing the card
    out. The card's header and rows are readable in light and dark (a different colour each). Fails if the
    page scrolls sideways, the text is squeezed into a few letters, or the card has no theme."""
    _plant(
        calls,
        "#phone",
        [
            ("meeting", "bob", "x" * 140, 0),
            ("meeting", "alice", "a short line that has to share the row", 30),
        ],
        call="meet",
    )
    page = _open(ui, calls, room="phone", **PHONE)
    card = page.locator("#log > .call-card")
    expect(card).to_have_count(1)
    expect(card.locator(".call-title")).to_have_text("Meet call")
    width = "el => el.getBoundingClientRect().width"
    card_w = card.evaluate(width)
    viewport_w = page.evaluate("document.documentElement.clientWidth")
    assert card_w <= viewport_w, (card_w, viewport_w)
    row = card.locator(".call-row").nth(1)
    assert row.locator(".msg-text").evaluate(width) > row.evaluate(width) * 0.8
    no_horizontal_scroll(page)
    inside = page.evaluate(
        """() => Array.from(document.querySelectorAll('#log .call-card *')).every(
              e => e.getBoundingClientRect().right <= document.documentElement.clientWidth + 1)"""
    )
    assert inside

    def look(p: Page) -> tuple[str, str]:
        return (
            p.locator("#log .call-card").evaluate("el => getComputedStyle(el).backgroundColor"),
            p.locator("#log .call-head").evaluate("el => getComputedStyle(el).backgroundColor"),
        )

    light = look(page)
    page.emulate_media(color_scheme="dark")
    expect(card.locator(".call-title")).to_be_visible()
    dark = look(page)
    assert light[0] != dark[0] and light[1] != dark[1], (light, dark)
