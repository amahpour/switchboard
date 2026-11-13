"""A flow drawn by the broker, editable, and a read-only storyboard, in the same opaque board (§56.9)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect
from test_board_ui import open_board
from test_web_ui import PHONE, UI, no_horizontal_scroll, ui, world  # noqa: F401
from ui_world import UIWorld, wait_js

pytestmark = pytest.mark.e2e


def test_a_diagram_drawn_by_the_broker_shows_in_a_page_opened_afterwards_and_hostile_text_stays_text(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
):  # noqa: F811
    """The broker converts with no page open; a page opened afterwards draws the stored shapes (not an
    image) with neither live links nor HTML handlers, clear of an earlier drawing, and a real key edit to
    a converted label goes through the normal person change path. Fails if the conversion still needed
    the pane, or the stored elbow arrows and frame broke the board."""
    world.create_room("#flow")
    world.add_agents("#flow", ("drawer",))
    b = world.broker
    existing = b.on_loop(
        lambda: b.state.room_board.agent(
            b.state.store.get_room("#flow"),
            "alice",
            {"action": "box", "text": "Existing", "x": 400, "y": 100},
            None,
        )
    )
    source = (
        'flowchart LR\nsubgraph team [Team]\nA["<img src=x onerror=alert(1)>"] --> B[Review]\nend\n'
        'B -->|ship it| C{Done?}\nclick A href "https://example.com"'
    )
    made = world.agent_call("#flow", "drawer", "board", action="draw_mermaid", text=source)
    assert made.get("changed"), made
    page = ui.open("flow")
    frame = open_board(page)
    elements = frame.locator("body").evaluate("window.sbBoard.api.getSceneElements()")
    imported = [e for e in elements if e["id"] in made["ids"]]
    assert len(imported) == len(made["ids"])
    assert any("<img src=x onerror=alert(1)>" in e.get("originalText", e.get("text", "")) for e in imported)
    assert imported and all(e["type"] != "image" and e.get("link") is None for e in imported)
    assert [e["name"] for e in imported if e["type"] == "frame"] == ["Team"]
    arrows = [e for e in imported if e["type"] == "arrow"]
    assert arrows and all(e.get("elbowed") for e in arrows)
    original = next(e for e in elements if e["id"] == existing["ids"][0])
    assert min(e["x"] for e in imported) >= original["x"] + original["width"] + 80
    assert frame.locator("a[href],img,object,embed,iframe").count() == 0
    text = next(e for e in imported if e["type"] == "text" and "Review" in e["text"])
    frame.locator("body").evaluate(
        """(_el, id) => { const a=window.sbBoard.api;
      a.updateScene({appState:{selectedElementIds:{[id]:true}}}); }""",
        text["id"],
    )
    frame.locator(".excalidraw").press("ArrowRight")
    wait_js(
        page.frame(url=re.compile(r"/board$")),
        "([id,x]) => window.sbBoard.api.getSceneElements().find(e=>e.id===id).x > x",
        arg=[text["id"], text["x"]],
    )
    expect(page.locator("#whiteboard-status")).to_contain_text("Live")
    no_horizontal_scroll(page)


@pytest.mark.parametrize("phone", [False, True])
def test_play_steps_through_six_frames_in_reading_order_with_captions_and_never_writes(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
    phone: bool,
):  # noqa: F811
    """Playback fills the viewport, isolates each frame, and restores the editable scene without a write."""
    room = "#story-" + ("phone" if phone else "desktop")
    world.create_room(room)
    b = world.broker
    for i in reversed(range(6)):
        b.on_loop(
            lambda i=i: b.state.room_board.agent(
                b.state.store.get_room(room),
                "alice",
                {
                    "action": "frame",
                    "text": f"Step {i + 1}",
                    "x": (i % 3) * 1000,
                    "y": (i // 3) * 800 + (50 if i == 0 else 40),
                },
                None,
            )
        )
    before = b.on_loop(lambda: b.state.store.board_load(b.state.store.get_room(room).id))
    page = ui.open(room.lstrip("#"), **(PHONE if phone else {}))
    open_board(page)
    page.get_by_role("button", name="Play frames").click()
    wait_js(page, "() => document.fullscreenElement?.id === 'whiteboard-panel'")
    for i in range(6):
        expect(page.locator("#whiteboard-caption")).to_have_text(f"{i + 1} / 6 · Step {i + 1}")
        wait_js(
            page.frame(url=re.compile(r"/board$")),
            "() => window.sbBoard.api.getAppState().viewModeEnabled === true",
        )
        wait_js(
            page.frame(url=re.compile(r"/board$")),
            """name => { const fs=window.sbBoard.api.getSceneElements().filter(e=>e.type==='frame');
            return fs.length === 1 && fs[0].name === name; }""",
            arg=f"Step {i + 1}",
        )
        if i != 5:
            page.get_by_role("button", name="Next frame").click()
    expect(page.get_by_role("button", name="Next frame")).to_be_disabled()
    box = page.locator("#whiteboard-panel").bounding_box()
    assert box["x"] == box["y"] == 0
    no_horizontal_scroll(page)
    page.get_by_role("button", name="Exit play").click()
    expect(page.get_by_role("button", name="Play frames")).to_be_focused()
    after = b.on_loop(lambda: b.state.store.board_load(b.state.store.get_room(room).id))
    assert after == before


FIT = """() => { const api = window.sbBoard.api, a = api.getAppState();
  const f = api.getSceneElements().find(e => e.type === 'frame');
  if (!f || !a.viewModeEnabled) return 0;
  const wr = f.width * a.zoom.value / a.width, hr = f.height * a.zoom.value / a.height;
  return Math.max(wr, hr); }"""


@pytest.mark.parametrize("phone", [False, True])
def test_play_fits_a_small_frame_to_the_view_and_keeps_it_fitted(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
    phone: bool,
):  # noqa: F811
    """#686: Play frames showed a frame of a few hundred pixels at 100% in the middle of a full-screen, mostly
    empty board, because Excalidraw's `fitToContent` never zooms in past 100%. A storyboard step should fill
    the view like a slide does: the frame's box reaches most of the viewport in one direction (the fit asks
    for 0.85), and it stays so when the window changes size (a phone turned sideways, a window dragged).
    The frame is made 200x120, a size a person can draw. Fails if the frame stays near its own size, or does
    not refit after the viewport changes."""
    room = "#fit-" + ("phone" if phone else "desktop")
    world.create_room(room)
    b = world.broker
    b.on_loop(
        lambda: b.state.room_board.agent(
            b.state.store.get_room(room),
            "alice",
            {"action": "frame", "text": "Small step", "x": 0, "y": 0},
            None,
        )
    )
    page = ui.open(room.lstrip("#"), **(PHONE if phone else {}))
    open_board(page)
    board = page.frame(url=re.compile(r"/board$"))
    # make the frame small, as a person who drew one that size would: the agent tool's frame is 600x360
    board.evaluate(
        """() => { const api = window.sbBoard.api;
          api.updateScene({ elements: api.getSceneElementsIncludingDeleted().map(
            e => e.type === 'frame' ? { ...e, width: 200, height: 120, version: e.version + 1 } : e) });
          window.sbBoard.flush(); }"""
    )
    wait_js(page, "() => /1 shape/.test(document.getElementById('whiteboard-status').textContent)")
    page.get_by_role("button", name="Play frames").click()
    wait_js(page, "() => document.fullscreenElement?.id === 'whiteboard-panel'")
    wait_js(board, "() => window.sbBoard.api.getAppState().viewModeEnabled === true")
    wait_js(board, f"() => ({FIT})() >= 0.7")
    zoom = board.evaluate("window.sbBoard.api.getAppState().zoom.value")
    assert zoom > 1.2, zoom  # the frame is 200 px wide: it is drawn larger than life, not at 100%
    # a phone turned sideways, a window dragged: the page can't be resized while it is full screen, so the
    # browser is told its screen has another size, which is what the page sees either way
    width, height = (844, 390) if phone else (900, 560)
    page.context.new_cdp_session(page).send(
        "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": phone},
    )
    wait_js(board, f"() => ({FIT})() >= 0.7 && window.sbBoard.api.getAppState().zoom.value !== {zoom}")
    page.get_by_role("button", name="Exit play").click()
    expect(page.get_by_role("button", name="Play frames")).to_be_focused()


def test_a_dragged_shape_pulls_its_elbow_arrow_along_and_the_binding_keeps_its_fixed_point(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
):  # noqa: F811
    """An elbow arrow the broker drew is bound by `fixedPoint`, so when a person drags a shape in the pane the
    arrow's end follows it, and what is stored afterwards still has the binding, `fixedPoint` and elbows.
    Fails if the pane's rules dropped `fixedPoint` (the arrow would stay behind or lose its binding)."""
    import time

    world.create_room("#drag")
    world.add_agents("#drag", ("drawer",))
    b = world.broker
    made = world.agent_call(
        "#drag", "drawer", "board", action="draw_mermaid", text="flowchart LR\nA[One] --> B[Two]"
    )
    assert made.get("changed"), made
    page = ui.open("drag")
    frame = open_board(page)
    scene = "window.sbBoard.api.getSceneElements()"
    els = frame.locator("body").evaluate(scene)
    arrow = next(e for e in els if e["type"] == "arrow")
    two = next(e for e in els if e["id"] == arrow["endBinding"]["elementId"])
    assert arrow["elbowed"] and arrow["endBinding"]["fixedPoint"] and arrow["startBinding"]["fixedPoint"]
    end_before = arrow["y"] + arrow["points"][-1][1]
    state = frame.locator("body").evaluate(
        """() => { const s = window.sbBoard.api.getAppState();
        return {z: s.zoom.value, sx: s.scrollX, sy: s.scrollY, ox: s.offsetLeft, oy: s.offsetTop}; }"""
    )
    box = page.locator("#whiteboard-host iframe").bounding_box()
    cx = box["x"] + state["ox"] + (two["x"] + two["width"] / 2 + state["sx"]) * state["z"]
    cy = box["y"] + state["oy"] + (two["y"] + two["height"] / 2 + state["sy"]) * state["z"]
    drop = 150 * state["z"]
    page.mouse.move(cx, cy)
    page.mouse.click(cx, cy)  # select it, then drag it
    page.mouse.down()
    page.mouse.move(cx, cy + drop / 2, steps=5)
    page.mouse.move(cx, cy + drop, steps=5)
    page.mouse.up()
    wait_js(
        page.frame(url=re.compile(r"/board$")),
        "([id, y]) => window.sbBoard.api.getSceneElements().find(e => e.id === id).y > y + 100",
        arg=[two["id"], two["y"]],
    )
    moved = next(e for e in frame.locator("body").evaluate(scene) if e["id"] == arrow["id"])
    assert abs((moved["y"] + moved["points"][-1][1]) - end_before - 150) < 8, (
        "the arrow's end followed its shape"
    )
    deadline = time.monotonic() + 10
    while True:  # the page sends its change on pointer-up; wait for the broker to hold it
        stored = {
            e["id"]: e
            for e in b.on_loop(lambda: b.state.store.board_load(b.state.store.get_room("#drag").id))[1]
        }
        if stored[arrow["id"]]["version"] > 1 or time.monotonic() > deadline:
            break
    kept = stored[arrow["id"]]
    assert kept["elbowed"] is True and kept["version"] > 1
    for key in ("startBinding", "endBinding"):
        assert kept[key]["elementId"] in stored
        fx, fy = kept[key]["fixedPoint"]
        assert 0 <= fx <= 1 and 0 <= fy <= 1
