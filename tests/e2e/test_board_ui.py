"""People draw and agents update the opaque room board, with no CSP errors (§56.8, #657)."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from test_web_ui import PHONE, UI, no_horizontal_scroll, ui, world  # noqa: F401
from ui_world import UIWorld, wait_js

pytestmark = pytest.mark.e2e


def open_board(page: Page) -> Any:
    page.get_by_role("button", name="Whiteboard", exact=True).click()
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
        return [(e.x + e.width / 2 + s.scrollX) * z + s.offsetLeft, (e.y + e.height / 2 + s.scrollY) * z + s.offsetTop, z]; }""",
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
    moved_by = board_frame.evaluate(
        "(id) => window.sbBoard.api.getSceneElements().find(e => e.id === id).y", second
    ) - 300
    assert moved_by > 40, "the drag moved the target"
    assert abs(after["end"][1] - before["end"][1] - moved_by) < 8, (before["end"], after["end"])
    for p, q in zip(after["points"], after["points"][1:], strict=False):
        assert abs(p[0] - q[0]) < 1 or abs(p[1] - q[1]) < 1, after["points"]
