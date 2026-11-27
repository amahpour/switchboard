"""People draw and agents update the opaque room board, with no CSP errors (§56.8, #657)."""

from __future__ import annotations

import base64
import time
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from test_web_ui import PHONE, UI, no_horizontal_scroll, tap_more, ui, world  # noqa: F401
from ui_world import UIWorld, wait_js

pytestmark = pytest.mark.e2e


def open_board(page: Page) -> Any:
    icon = page.get_by_role("button", name="Whiteboard", exact=True)
    if icon.is_visible():
        icon.click()
    else:  # a phone: the header's icon is a row of the More sheet
        tap_more(page, "Whiteboard")
    expect(page.locator("#whiteboard-status")).to_contain_text("Live")
    frame = page.frame_locator("#whiteboard-host iframe")
    expect(frame.locator("canvas").first).to_be_visible()
    return frame


@pytest.mark.parametrize("mode", ["light", "dark", "phone"])
def test_person_draws_and_an_agent_shape_appears_live_in_an_opaque_frame(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
    mode: str,  # noqa: F811
) -> None:
    """Real pointer drawing is stored, and a subsequent tool note appears without reopening the frame."""
    room = f"board-{mode}"
    world.create_room(room)
    world.add_agents("#" + room, ("drawer",))
    page = ui.open(
        room, color_scheme="dark" if mode == "dark" else "light", **(PHONE if mode == "phone" else {})
    )
    cookies = []
    page.on(
        "request",
        lambda request: (
            cookies.append(request.all_headers().get("cookie", ""))
            if request.url.endswith("/board")
            else None
        ),
    )
    frame = open_board(page)
    other = open_board(ui.open(room)) if mode == "light" else None
    assert cookies == [""], "the credentialless document navigation must not carry the app cookie"
    node = page.locator("#whiteboard-host iframe")
    expect(node).to_have_attribute("sandbox", "allow-scripts")
    assert "allow-same-origin" not in (node.get_attribute("sandbox") or "")
    # This is Chromium's actual origin boundary, not a string check of the sandbox attribute.
    assert frame.locator("body").evaluate("""() => {
      try { parent.document.body; return false; } catch (e) { return e.name === 'SecurityError'; }
    }""")
    assert frame.locator("body").evaluate("""() => {
      try { return document.cookie === ''; } catch (e) { return e.name === 'SecurityError'; }
    }""")
    frame.locator("label").filter(has=frame.locator('[data-testid="toolbar-rectangle"]')).click()
    box = node.bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] * 0.35, box["y"] + box["height"] * 0.4
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 90, y + 70, steps=8)
    page.mouse.up()
    expect(page.locator("#whiteboard-status")).to_contain_text("1 shape")
    made = world.agent_call(
        "#" + room,
        "drawer",
        "board",
        action="note",
        text="<img onerror=alert(1)> is text 日本語",
        x=240,
        y=80,
    )
    assert made["ok"], made
    if other is not None:
        expect(other.locator("#board-count")).to_have_text("3 shapes")
    expect(frame.locator("#board-count")).to_have_text("3 shapes")
    texts = frame.locator("body").evaluate(
        "() => window.sbBoard.api.getSceneElements().filter(e => e.type === 'text').map(e => e.originalText)"
    )
    assert texts == ["<img onerror=alert(1)> is text 日本語"]
    assert frame.locator("img, a[href], iframe, object, embed").count() == 0
    frame.locator("body").evaluate("""() => {
        const a=window.sbBoard.api, t=a.getSceneElements().find(e=>e.type==='text');
        a.updateScene({appState:{selectedElementIds:{[t.id]:true}}});
    }""")
    expect(frame.get_by_role("button", name="Add link", include_hidden=True)).to_be_hidden()
    expect(
        frame.locator("fieldset").filter(has=frame.locator('[data-testid="font-family-normal"]'))
    ).to_be_hidden()
    agent = world._extra_named[("#" + room, "drawer")]
    picture = world._scripted(agent.raw("board", {"room": "#" + room, "action": "read", "text": "image"}))
    assert not picture.is_error and picture.content[0].type == "image"
    assert picture.content[0].mime_type == "image/png"
    # A forged message from the parent window cannot stand in for the frame.
    page.evaluate("window.postMessage({t:'sb-board', action:'change', elements:[]}, '*')")
    no_horizontal_scroll(page)
    page.get_by_role("button", name="Back to chat").click()
    expect(page.locator("#whiteboard-host iframe")).to_have_count(0)
    expect(page.get_by_role("button", name="Whiteboard", exact=True)).to_be_focused()


def test_agent_arrows_bind_to_existing_shapes_without_reconverting_the_targets(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
) -> None:
    """Regression for the spike: existing shape dimensions and both arrow bindings survive a later connect."""
    room = "board-arrow"
    world.create_room(room)
    page = ui.open(room)
    frame = open_board(page)
    b = world.broker

    def draw(action: str, **params: Any) -> dict[str, Any]:
        return b.on_loop(
            lambda: b.state.room_board.agent(
                b.state.store.get_room("#" + room), "drawer", {"action": action, **params}, None
            )
        )

    first = draw("box", text="Start", x=0, y=0)["ids"][0]
    second = draw("box", text="Done", x=400, y=0)["ids"][0]
    expect(frame.locator("#board-count")).to_have_text("4 shapes")
    before = frame.locator("body").evaluate(
        """() => window.sbBoard.api.getSceneElements().filter(e => e.type === 'rectangle')
        .map(e => [e.id,e.x,e.y,e.width,e.height])"""
    )
    arrow = draw("connect", id=first, to=second)["ids"][0]
    expect(frame.locator("#board-count")).to_have_text("5 shapes")
    after = frame.locator("body").evaluate(
        """() => window.sbBoard.api.getSceneElements().filter(e => e.type === 'rectangle')
        .map(e => [e.id,e.x,e.y,e.width,e.height])"""
    )
    assert after == before
    ends = frame.locator("body").evaluate(
        """(_node, id) => { const a=window.sbBoard.api.getSceneElements().find(e => e.id === id);
        return [a.startBinding.elementId,a.endBinding.elementId]; }""",
        arrow,
    )
    assert ends == [first, second]

    end = frame.locator("body").evaluate(
        """(_node,id) => { const a=window.sbBoard.api.getSceneElements().find(e => e.id===id);
        return a.x+a.points[1][0]; }""",
        arrow,
    )
    target = next(row for row in before if row[0] == second)
    assert abs(end - (target[1] - 8)) < 1, "the arrow must end at the target edge plus its binding gap"


def test_bound_agent_text_wraps_to_its_container_without_changing_the_stored_scene(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
) -> None:
    """Restore must measure and wrap bound labels; trusting the broker's rough text box clips words."""
    world.create_room("board-label")
    b = world.broker
    phrase = "Ready for review by the whole team before release"
    b.on_loop(
        lambda: b.state.room_board.agent(
            b.state.store.get_room("#board-label"), "drawer", {"action": "note", "text": phrase}, None
        )
    )
    before = b.on_loop(lambda: b.state.store.board_load(b.state.store.get_room("#board-label").id))
    page = ui.open("board-label")
    frame = open_board(page)
    frame.locator("body").evaluate("document.fonts.ready")
    board_frame = next(f for f in page.frames if f.url.endswith("/board"))
    wait_js(
        board_frame,
        "() => window.sbBoard.api.getSceneElements().some(e=>e.type==='text' && e.text.includes('\\n'))",
    )
    elements = frame.locator("body").evaluate("window.sbBoard.api.getSceneElements()")
    text = next(e for e in elements if e["type"] == "text")
    container = next(e for e in elements if e["id"] == text["containerId"])
    assert "\n" in text["text"], "the rough one-line text box must be measured and wrapped"
    assert abs(text["y"] + text["height"] / 2 - container["y"] - container["height"] / 2) < 1
    assert " ".join(text["text"].split()) == phrase and text["originalText"] == phrase
    assert container["width"] == before[1][0]["width"] and container["height"] == before[1][0]["height"]
    assert b.on_loop(lambda: b.state.store.board_load(b.state.store.get_room("#board-label").id)) == before


def frame_pixel(ui: UI, host: Any) -> tuple[int, int, int]:  # noqa: F811
    """The middle pixel of the board frame as painted, read back in a blank page (the e2e setup has no
    imaging library)."""
    decoder = ui.context().new_page()
    r, g, b = decoder.evaluate(
        """async b64 => {
          const img = new Image(); img.src = 'data:image/png;base64,' + b64; await img.decode();
          const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;
          const x = c.getContext('2d'); x.drawImage(img, 0, 0);
          return [...x.getImageData(img.width >> 1, img.height >> 1, 1, 1).data].slice(0, 3);
        }""",
        base64.b64encode(host.screenshot(animations="disabled")).decode(),
    )
    decoder.close()
    return r, g, b


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_board_frame_stays_hidden_until_it_has_drawn_in_the_apps_theme(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
    scheme: str,
) -> None:
    """#684: opening Whiteboard in the dark theme showed a white frame for a moment. The board mounts in
    Excalidraw's light theme and is told the app's theme after it reports ready, so the first frames are
    light. The frame now stays hidden, over the app's own background, until the board has rendered in the
    app's theme. The test holds the board's scripts back so the frame is still opening, and checks that the
    frame is hidden and the painted pixel is the app's background (dark in the dark theme, white in the
    light one); then it lets the scripts go and checks that the frame is shown, with a board that is
    dark in the dark theme. Fails if the frame is visible while it is opening."""
    room = f"board-paint-{scheme}"
    world.create_room(room)
    page = ui.open(room, color_scheme=scheme)
    held: list[Any] = []
    # never answered until the end: mermaid.min.js blocks the shell's first paint and board.js draws the board
    page.route("**/board-assets/*.js", lambda route: held.append(route))
    with page.expect_request("**/board-assets/mermaid.min.js"):  # the shell is in, its scripts are held
        page.get_by_role("button", name="Whiteboard", exact=True).click()
    host = page.locator("#whiteboard-host iframe")
    expect(page.locator("#whiteboard-status")).to_have_text("Opening whiteboard…")
    expect(host).to_be_hidden()  # not painted: the host's background shows
    r, g, b = frame_pixel(ui, page.locator("#whiteboard-host"))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    assert (luminance < 60) if scheme == "dark" else (luminance > 230), ("opening", scheme, (r, g, b))
    for route in held:
        route.continue_()
    page.unroute("**/board-assets/*.js")
    expect(page.locator("#whiteboard-status")).to_contain_text("Live")
    expect(host).to_be_visible()
    r, g, b = frame_pixel(ui, host)
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    assert (luminance < 60) if scheme == "dark" else (luminance > 230), ("drawn", scheme, (r, g, b))


def test_an_agent_elbow_arrow_follows_a_shape_a_person_drags(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
) -> None:
    """``connect`` with ``arrow-type=elbow`` routes with #730's router and binds with ``fixedPoint``
    ends, so Excalidraw keeps the arrow attached: dragging the target with the mouse must carry the
    arrow's end with it, and the arrow stays an elbow with right angles. Without the ``fixedPoint`` the
    pane drops the binding and the arrow stays behind."""
    room = "board-elbow"
    world.create_room(room)
    page = ui.open(room)
    frame = open_board(page)
    b = world.broker

    def draw(action: str, **params: Any) -> dict[str, Any]:
        return b.on_loop(
            lambda: b.state.room_board.agent(
                b.state.store.get_room("#" + room), "drawer", {"action": action, **params}, None
            )
        )

    first = draw("box", text="Start", x=0, y=300)["ids"][0]  # below the toolbar, which covers the top
    second = draw("box", text="Done", x=500, y=300)["ids"][0]
    arrow = draw("connect", id=first, to=second, style="arrow-type=elbow")["ids"][0]
    expect(frame.locator("#board-count")).to_have_text("5 shapes")
    read = """(_n, id) => { const api = window.sbBoard.api, a = api.getSceneElements().find(e => e.id === id);
        return {elbowed: a.elbowed, bound: !!(a.startBinding && a.endBinding),
                end: [a.x + a.points[a.points.length - 1][0], a.y + a.points[a.points.length - 1][1]],
                points: a.points}; }"""
    before = frame.locator("body").evaluate(read, arrow)
    assert before["elbowed"] is True and before["bound"] is True, before
    centre = frame.locator("body").evaluate(
        """(_n, id) => { const api = window.sbBoard.api, s = api.getAppState(),
        e = api.getSceneElements().find(x => x.id === id), z = s.zoom.value;
        return [(e.x + e.width / 2 + s.scrollX) * z + s.offsetLeft,
                (e.y + e.height / 2 + s.scrollY) * z + s.offsetTop, z]; }""",
        second,
    )
    node = page.locator("#whiteboard-host iframe").bounding_box()
    assert node is not None
    x, y = node["x"] + centre[0], node["y"] + centre[1]
    page.mouse.move(x, y)
    page.screenshot(path="/tmp/x/dbg.png")
    page.mouse.down()
    page.mouse.move(x, y + 150 * centre[2], steps=10)
    page.mouse.up()
    # The click selects the target; the arrow keys then move it the way a person nudging it would.
    page.keyboard.press("Shift+ArrowDown", delay=20)
    for _ in range(14):
        page.keyboard.press("Shift+ArrowDown", delay=20)
    board_frame = next(f for f in page.frames if f.url.endswith("/board"))
    wait_js(
        board_frame,
        "() => window.sbBoard.api.getSceneElements().some(e => e.type === 'rectangle' && e.y > 340)",
    )
    after = frame.locator("body").evaluate(read, arrow)
    assert after["bound"] is True and after["elbowed"] is True, after
    moved_by = (
        board_frame.evaluate("(id) => window.sbBoard.api.getSceneElements().find(e => e.id === id).y", second)
        - 300
    )
    assert moved_by > 40, "the drag moved the target"
    assert abs(after["end"][1] - before["end"][1] - moved_by) < 8, (before["end"], after["end"])
    for p, q in zip(after["points"], after["points"][1:], strict=False):
        assert abs(p[0] - q[0]) < 1 or abs(p[1] - q[1]) < 1, after["points"]


def test_a_secret_a_person_types_is_cut_out_and_the_drawing_tab_shows_the_committed_text(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
) -> None:
    """Real typing in the real frame, through both client cleaners and the socket (#893). The text crosses
    character 200, where the browser used to cut it before the broker saw it, so the broker could not
    recognize what was left; and the tab that drew keeps its own copy until the broker answers, and the
    answer lost a tie, so the author kept seeing the secret and an image read of the board would show it.
    Fails if the stored text holds the token, or if the drawing tab's scene (the source of an agent's image
    read) still holds it after the broker's reply."""
    room = "board-secret"
    world.create_room(room)
    page = ui.open(room)
    frame = open_board(page)
    board_frame = next(f for f in page.frames if f.url.endswith("/board"))
    token = "ghp" + "_" + "aB3" * 12
    typed = "x" * 170 + " " + token
    frame.locator("label").filter(has=frame.locator('[data-testid="toolbar-text"]')).click()
    box = page.locator("#whiteboard-host iframe").bounding_box()
    assert box is not None
    page.mouse.click(box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.4)
    page.keyboard.type(typed)
    page.keyboard.press("Escape")
    b = world.broker

    def stored() -> str:
        return str(b.on_loop(lambda: b.state.store.board_load(b.state.store.get_room("#" + room).id)))

    wait_js(
        board_frame,
        "() => window.sbBoard.api.getSceneElements()"
        ".some(e => e.type === 'text' && e.originalText.includes('[redacted'))",
    )
    assert token not in stored(), stored()[:3000]
    assert "[redacted: github token]" in stored()
    scene = board_frame.evaluate("JSON.stringify(window.sbBoard.api.getSceneElementsIncludingDeleted())")
    assert token not in scene and "aB3aB3" not in scene


def test_text_closed_with_escape_after_the_broker_answered_is_sent_again(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
) -> None:
    """A person types a secret, stops, and presses Escape only after the broker has answered (#933). The
    pause lets the frame send the open text, and the broker's redacted copy replaces it while the editor is
    still open. Escape then commits the editor's own text, a new version holding the secret, inside
    Excalidraw's key handler and so before the frame's keydown listener had marked the board dirty: the
    change was never sent, and the author's tab kept the secret. That was the flake of the test above,
    when a slow runner left a pause before its Escape. Fails if the broker never stores a newer version of
    the text after Escape, redacted, or the tab does not show it. Without the fix a late onChange of
    Excalidraw's sometimes sends the change anyway; the static check of the capture listener in
    `test_web_static_lint.py` is the one that fails every time."""
    room = "board-secret-escape"
    world.create_room(room)
    page = ui.open(room)
    frame = open_board(page)
    board_frame = next(f for f in page.frames if f.url.endswith("/board"))
    token = "ghp" + "_" + "aB3" * 12
    frame.locator("label").filter(has=frame.locator('[data-testid="toolbar-text"]')).click()
    box = page.locator("#whiteboard-host iframe").bounding_box()
    assert box is not None
    page.mouse.click(box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.4)
    # One input event, so the frame sends the open text once and no older copy of it is still on its way back.
    page.keyboard.insert_text("note " + token)
    # the frame sends the open text once typing stops, and the broker's redacted copy lands under the editor
    wait_js(
        board_frame,
        "() => window.sbBoard.api.getAppState().editingTextElement !== null && window.sbBoard.api"
        ".getSceneElements().some(e => e.type === 'text' && e.originalText.includes('[redacted'))",
    )
    b = world.broker

    def stored_text() -> dict[str, Any]:
        _, elements = b.on_loop(lambda: b.state.store.board_load(b.state.store.get_room("#" + room).id))
        texts = [e for e in elements if e.get("type") == "text"]
        assert len(texts) == 1, elements
        return texts[0]

    # the redacted copy the tab holds came from here, so Escape's commit is what a newer version must be
    before = stored_text()
    assert token not in str(before) and "[redacted: github token]" in before["originalText"], before
    page.keyboard.press("Escape")
    # until the broker stores the change Escape made: a poll of the store, bounded, not a wait for "by now"
    deadline = time.monotonic() + 15
    after = stored_text()
    while after["version"] <= before["version"] and time.monotonic() < deadline:
        time.sleep(0.05)
        after = stored_text()
    assert after["id"] == before["id"] and after["version"] > before["version"], (before, after)
    assert token not in str(after) and "[redacted: github token]" in after["originalText"], after
    # and the tab that drew shows that version, closed, not the editor's own text
    wait_js(
        board_frame,
        "([id, v]) => window.sbBoard.api.getAppState().editingTextElement === null && window.sbBoard.api"
        ".getSceneElements().some(e => e.id === id && e.version >= v"
        " && e.originalText.includes('[redacted'))",
        [after["id"], after["version"]],
    )
    scene = board_frame.evaluate("JSON.stringify(window.sbBoard.api.getSceneElementsIncludingDeleted())")
    assert token not in scene and "aB3aB3" not in scene


# Added to the context before the board frame loads: its message listeners are wrapped as they register, so
# that every `elements` message is held in the order it arrives, and `__deliver` hands the first one to them
# as the parent would have (the same data and source). Nothing else about the frame changes.
HOLD_BOARD_COPIES = """(() => { if (location.pathname !== '/board') return;
          const add = window.addEventListener.bind(window);
          window.__held = []; window.__listeners = [];
          window.addEventListener = function (type, fn, opts) {
            if (type !== 'message' || typeof fn !== 'function') return add(type, fn, opts);
            window.__listeners.push(fn);
            return add(type, ev => {
              const m = ev.data;
              if (ev.source === parent && m && m.t === 'sb-board' && m.action === 'elements') {
                window.__held.push({ data: m, source: ev.source });
              } else fn(ev);
            }, opts);
          };
          window.__deliver = () => {
            const ev = window.__held.shift();
            window.__listeners.forEach(fn => fn(ev));
          };
          window.__texts = h => h.data.elements.filter(e => e.type === "text").map(e => e.originalText);
        })();"""


def test_a_board_copy_that_was_in_flight_does_not_take_back_text_the_frame_already_sent(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
) -> None:
    """A person types, the frame sends the open text and the broker stores it; they type more and the frame
    sends that too, before the broker's copy of the first text has reached the frame (#937). Copies arrive
    in the order the broker made them, so the copy of the first text then reaches the frame after the frame
    has sent more, and it put the open text back to the first words, losing what was typed in between.
    Every copy here is held in the frame and delivered in its original order by the test, so nothing depends
    on load or on a sleep: the copy of the shorter text is delivered while the copy of the longer one is
    still on its way. Fails if the open text goes back to the shorter one, if the longer copy's arrival does
    not then clear the hold, or if a note an agent adds afterwards (a newer copy) is not applied."""
    room = "board-in-flight-copy"
    world.create_room(room)
    world.add_agents("#" + room, ("drawer",))
    page = ui.open(room)
    page.context.add_init_script(HOLD_BOARD_COPIES)
    frame = open_board(page)
    board_frame = next(f for f in page.frames if f.url.endswith("/board"))
    b = world.broker

    def stored_text() -> str:
        _, elements = b.on_loop(lambda: b.state.store.board_load(b.state.store.get_room("#" + room).id))
        return " ".join(str(e.get("originalText")) for e in elements if e.get("type") == "text")

    def wait_stored(words: str) -> None:
        deadline = time.monotonic() + 20
        while stored_text() != words and time.monotonic() < deadline:
            time.sleep(0.05)
        assert stored_text() == words

    frame.locator("label").filter(has=frame.locator('[data-testid="toolbar-text"]')).click()
    box = page.locator("#whiteboard-host iframe").bounding_box()
    assert box is not None
    page.mouse.click(box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.4)
    page.keyboard.insert_text("alpha")
    wait_stored("alpha")
    page.keyboard.insert_text(" beta gamma")
    wait_stored("alpha beta gamma")
    # the broker's copies of both texts are now on their way to the frame, held in order
    wait_js(board_frame, "() => window.__held.some(m => window.__texts(m).includes('alpha beta gamma'))")
    older = board_frame.evaluate(
        "() => window.__held.filter(m => !window.__texts(m).includes('alpha beta gamma')).length"
    )
    assert older >= 1, "the copy of the first text never came"
    # the older copies reach the frame first; it has already sent the longer text
    for _ in range(older):
        board_frame.evaluate("() => window.__deliver()")
    open_text = (
        "() => window.sbBoard.api.getSceneElements().filter(e => e.type === 'text').map(e => e.originalText)"
    )
    assert board_frame.evaluate(open_text) == ["alpha beta gamma"], board_frame.evaluate(open_text)
    assert board_frame.evaluate("() => window.sbBoard.api.getAppState().editingTextElement !== null")
    # the copy of the longer text then arrives and the frame shows it, with nothing held back any more
    while board_frame.evaluate("() => window.__held.length"):
        board_frame.evaluate("() => window.__deliver()")
    assert board_frame.evaluate(open_text) == ["alpha beta gamma"]
    # a newer copy from someone else is applied at once
    made = world.agent_call("#" + room, "drawer", "board", action="note", text="newer", x=400, y=300)
    assert made["ok"], made
    wait_js(board_frame, "() => window.__held.length > 0")
    while board_frame.evaluate("() => window.__held.length"):
        board_frame.evaluate("() => window.__deliver()")
    assert sorted(board_frame.evaluate(open_text)) == ["alpha beta gamma", "newer"]


def test_a_refused_shape_goes_when_the_whole_board_answers_it_though_an_older_copy_arrived_first(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
) -> None:
    """The frame holds back what it sent from a copy of the board that does not show it yet (#937), and a
    shape the broker refuses never shows in any copy. The broker answers a refused write with the whole
    board (#893), and that must end the hold: a frame that kept holding would show the refused shape for
    good. An agent's note makes a copy the app posted before it had seen the shape; it reaches the frame
    after the frame sent the shape, while the answer is still on its way. Fails if the shape is gone before
    the whole board arrives (the hold does nothing), or still shown after it (the hold was never cleared)."""
    room = "board-refused-held"
    world.create_room(room)
    world.add_agents("#" + room, ("drawer",))
    page = ui.open(room)
    page.context.add_init_script(HOLD_BOARD_COPIES)
    open_board(page)
    board_frame = next(f for f in page.frames if f.url.endswith("/board"))
    made = world.agent_call("#" + room, "drawer", "board", action="note", text="older", x=100, y=100)
    assert made["ok"], made
    wait_js(board_frame, "() => window.__held.some(h => window.__texts(h).includes('older'))")
    # a shape the broker refuses (its marker would push its text past the limit), sent as drawing does
    refused = "x" * 170 + " password=" + "hunter2" * 2
    board_frame.evaluate(
        """(text) => { const a = window.sbBoard.api;
          a.updateScene({ elements: [...a.getSceneElementsIncludingDeleted(), {
            id: 'refused1', type: 'rectangle', x: 40, y: 300, width: 160, height: 90, version: 1,
            versionNonce: 7, isDeleted: false, seed: 1, angle: 0, strokeColor: '#000000',
            backgroundColor: '#ff0000', fillStyle: 'solid', strokeWidth: 2, roughness: 1, opacity: 100,
            groupIds: [], frameId: null, roundness: null, boundElements: null, updated: 1, link: null,
            locked: false, text }] });
          document.dispatchEvent(new KeyboardEvent('keydown', { key: 'a' }));
          window.sbBoard.flush(); }""",
        refused,
    )
    # the broker's answer, the whole board, is held behind the note's copy, which has not seen the shape
    wait_js(board_frame, "() => window.__held.some(h => h.data.full === true)")
    shown = "() => window.sbBoard.api.getSceneElements().some(e => e.id === 'refused1')"
    while not board_frame.evaluate("() => window.__held[0].data.full === true"):
        assert not any(
            e["id"] == "refused1" for e in board_frame.evaluate("() => window.__held[0].data.elements")
        )
        board_frame.evaluate("() => window.__deliver()")
    assert board_frame.evaluate(shown), "an older copy took the shape away before the broker answered"
    board_frame.evaluate("() => window.__deliver()")
    assert not board_frame.evaluate(shown), "the broker refused the shape and its answer did not remove it"


def test_a_picture_for_an_agent_shows_only_the_committed_board(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
) -> None:
    """The image read is made from the scene the broker has committed, never the live drawing (#893). A shape
    with a secret is put on the frame's canvas and not yet flushed: the picture must be byte for byte the
    one taken before, and the export message must hold no token. Fails if the picture changes or the export
    carries the secret."""
    room = "board-export"
    world.create_room(room)
    world.add_agents("#" + room, ("drawer",))
    made = world.agent_call("#" + room, "drawer", "board", action="note", text="committed")
    assert made["ok"], made
    page = ui.open(room)
    open_board(page)
    board_frame = next(f for f in page.frames if f.url.endswith("/board"))
    wait_js(board_frame, "() => window.sbBoard.api.getSceneElements().length >= 2")
    board_frame.evaluate("""() => {
      window.exported = [];
      window.addEventListener('message', ev => {
        if (ev.data && ev.data.action === 'export') window.exported.push(JSON.stringify(ev.data));
      });
    }""")
    agent = world._extra_named[("#" + room, "drawer")]

    def picture() -> str:
        got = world._scripted(agent.raw("board", {"room": "#" + room, "action": "read", "text": "image"}))
        assert not got.is_error and got.content[0].type == "image"
        return str(got.content[0].data)

    before = picture()
    token = "ghp" + "_" + "aB3" * 12

    board_frame.evaluate(
        """() => { window.putEl = (eid, text, y) => {
          const a = window.sbBoard.api;
          a.updateScene({ elements: [...a.getSceneElementsIncludingDeleted(), {
            id: eid, type: 'rectangle', x: 40, y, width: 160, height: 90, version: 1, versionNonce: 7,
            isDeleted: false, seed: 1, angle: 0, strokeColor: '#000000', backgroundColor: '#ff0000',
            fillStyle: 'solid', strokeWidth: 2, roughness: 1, opacity: 100, groupIds: [], frameId: null,
            roundness: null, boundElements: null, updated: 1, link: null, locked: false, text }] });
        }; }"""
    )

    def put(eid: str, text: str, y: int = 300) -> None:
        board_frame.evaluate("([eid, text, y]) => window.putEl(eid, text, y)", [eid, text, y])

    put("unsent1", token)  # on the canvas, never flushed: dirty is not set, so the frame does not send it
    assert token in board_frame.evaluate("JSON.stringify(window.sbBoard.api.getSceneElements())")
    assert picture() == before, "the picture drew the live drawing"
    exported = board_frame.evaluate("window.exported")
    assert exported and all(token not in e for e in exported)
    # a shape the broker refuses: its text fits, and its marker would push it past the limit
    refused = "x" * 170 + " password=" + "hunter2" * 2
    put("refused1", refused)
    assert board_frame.evaluate("window.sbBoard.api.getSceneElements().some(e => e.id === 'refused1')")
    # as drawing does: dirty, then a flush; then the person starts a different shape and the pointer stays
    # down, so the frame is dirty (and does not flush) when the broker's answer arrives. The refused shape was
    # sent and has not changed since, so it goes with the broker's board; the new one was never sent: stays.
    board_frame.evaluate(
        "() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'a' })); window.sbBoard.flush();"
        " window.putEl('keep1', 'still drawing', 450);"
        " document.dispatchEvent(new PointerEvent('pointerdown')); }"
    )
    wait_js(board_frame, "() => !window.sbBoard.api.getSceneElements().some(e => e.id === 'refused1')")
    ids = board_frame.evaluate("window.sbBoard.api.getSceneElements().map(e => e.id)")
    assert "keep1" in ids and "unsent1" in ids


def test_text_stored_unwrapped_is_wrapped_again_by_the_page_and_a_wrapped_secret_shows_as_a_marker(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
) -> None:
    """The broker keeps one string, the read ``originalText``, and ignores the page's wrapped ``text`` (#893).
    That is only sound if Excalidraw wraps a loaded text again from the words it is given: a fixed-width text
    stored on one line must come out wrapped in the scene, and a token the page had wrapped across two lines
    must be stored and drawn as the marker. Fails if the scene's text has no line break, or holds a piece of
    the token."""
    room = "board-rewrap"
    world.create_room(room)
    b = world.broker
    long = "a long label that has to wrap inside a narrow fixed width text box " * 2
    token = "ghp" + "_" + "aB3" * 12

    def text(eid: str, y: int, words: str, wrapped: str) -> dict[str, Any]:
        return {
            "id": eid, "type": "text", "x": 20, "y": y, "width": 120, "height": 25, "version": 1,
            "versionNonce": 3, "isDeleted": False, "text": wrapped, "originalText": words, "fontSize": 16,
            "fontFamily": 1, "autoResize": False, "textAlign": "left", "verticalAlign": "top",
            "containerId": None, "lineHeight": 1.25,
        }  # fmt: skip

    b.on_loop(
        lambda: b.state.room_board.from_page(
            b.state.store.get_room("#" + room),
            "alice",
            [
                text("w1", 20, long, long),
                text("w2", 300, f"key {token} end", f"key {token[:20]}\n{token[20:]} end"),
            ],
        )
    )
    stored = str(b.on_loop(lambda: b.state.store.board_load(b.state.store.get_room("#" + room).id)))
    assert token not in stored and token[20:] not in stored and "[redacted: github token]" in stored
    page = ui.open(room)
    open_board(page)
    board_frame = next(f for f in page.frames if f.url.endswith("/board"))
    wait_js(
        board_frame,
        "() => window.sbBoard.api.getSceneElements().some(e => e.id === 'w1' && e.text.includes('\\n'))",
    )
    scene = board_frame.evaluate("JSON.stringify(window.sbBoard.api.getSceneElements())")
    assert token[:20] not in scene and token[20:] not in scene and "redacted" in scene


def test_one_old_stored_shape_does_not_keep_the_whole_board_from_opening(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
) -> None:
    """An earlier broker stored a text with a line break and no ``originalText``, which a write now refuses
    (#893). The board it loads is read as it was stored: the page shows every shape, and records the board's
    seq, which a picture for an agent waits for. Fails if the whole board is dropped (no shapes, and the
    picture read never answers)."""
    room = "board-legacy"
    world.create_room(room)
    world.add_agents("#" + room, ("drawer",))
    b = world.broker
    base = {"x": 20, "width": 120, "height": 40, "version": 1, "versionNonce": 3, "isDeleted": False}
    old_text = {**base, "id": "old1", "type": "text", "y": 20, "text": "two\nlines", "fontSize": 16}
    old_text.update(fontFamily=1, textAlign="left", verticalAlign="top", containerId=None, lineHeight=1.25)
    box = {**base, "id": "box1", "type": "rectangle", "y": 120, "link": None}
    row = b.on_loop(lambda: b.state.store.get_room("#" + room))
    b.on_loop(lambda: b.state.store.board_write(row.id, 1, [old_text, box], "alice", ["old1", "box1"]))
    page = ui.open(room)
    open_board(page)
    board_frame = next(f for f in page.frames if f.url.endswith("/board"))
    wait_js(board_frame, "() => window.sbBoard.api.getSceneElements().length === 2")
    agent = world._extra_named[("#" + room, "drawer")]
    got = world._scripted(agent.raw("board", {"room": "#" + room, "action": "read", "text": "image"}))
    assert not got.is_error and got.content[0].type == "image"


def test_a_board_near_the_limits_opens_with_its_text_from_the_built_bundle(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
) -> None:
    """A load keeps ``text`` and ``originalText`` exactly as stored, because an earlier broker and browser
    sized them independently and a board near the limits would otherwise open empty (DESIGN §56). The frame
    asks the rules for that with ``batch(elements, true)``; the bundle once shipped without the ``true`` (a
    hand-edited build that no longer matched ``scripts/board/frame.jsx``, #958). Fails if the committed
    ``board.js`` cuts a stored text to the write limit on load: here a 1500-character text beside a shorter
    ``originalText``, which the page must show whole."""
    room = "board-near-limits"
    world.create_room(room)
    b = world.broker
    base = {"x": 20, "width": 400, "height": 40, "version": 1, "versionNonce": 3, "isDeleted": False}
    long_text = {
        **base,
        "id": "long1",
        "type": "text",
        "y": 20,
        "text": "a" * 1500,
        "originalText": "a" * 1400,
    }
    long_text.update(fontSize=16, fontFamily=1, textAlign="left", verticalAlign="top", containerId=None)
    long_text.update(lineHeight=1.25)
    row = b.on_loop(lambda: b.state.store.get_room("#" + room))
    b.on_loop(lambda: b.state.store.board_write(row.id, 1, [long_text], "alice", ["long1"]))
    page = ui.open(room)
    open_board(page)
    board_frame = next(f for f in page.frames if f.url.endswith("/board"))
    wait_js(board_frame, "() => window.sbBoard.api.getSceneElements().length === 1")
    shown = board_frame.evaluate(
        "() => window.sbBoard.api.getSceneElements().map(e => [e.text.length, e.originalText.length])"
    )
    assert shown == [[1500, 1400]], shown
