"""People draw and agents update the opaque room board, with no CSP errors (§56.8, #657)."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect
from test_web_ui import PHONE, UI, no_horizontal_scroll, ui, world  # noqa: F401
from ui_world import UIWorld

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
    phrase = "Start with a shared goal"
    b.on_loop(
        lambda: b.state.room_board.agent(
            b.state.store.get_room("#board-label"), "drawer", {"action": "note", "text": phrase}, None
        )
    )
    before = b.on_loop(lambda: b.state.store.board_load(b.state.store.get_room("#board-label").id))
    page = ui.open("board-label")
    frame = open_board(page)
    frame.locator("body").evaluate("document.fonts.ready")
    elements = frame.locator("body").evaluate("window.sbBoard.api.getSceneElements()")
    text = next(e for e in elements if e["type"] == "text")
    container = next(e for e in elements if e["id"] == text["containerId"])
    assert "\n" in text["text"], "the rough one-line text box must be measured and wrapped"
    assert " ".join(text["text"].split()) == phrase and text["originalText"] == phrase
    assert container["width"] == before[1][0]["width"] and container["height"] == before[1][0]["height"]
    assert b.on_loop(lambda: b.state.store.board_load(b.state.store.get_room("#board-label").id)) == before
