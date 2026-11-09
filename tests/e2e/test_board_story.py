"""Editable fresh flows and a read-only storyboard, in the same opaque board (§56.9)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect
from test_board_ui import open_board
from test_web_ui import PHONE, UI, no_horizontal_scroll, ui, world  # noqa: F401
from ui_world import UIWorld, wait_js

pytestmark = pytest.mark.e2e


def test_mermaid_makes_editable_shapes_clear_of_existing_drawing_and_hostile_text_stays_text(
    ui: UI,  # noqa: F811
    world: UIWorld,  # noqa: F811
):  # noqa: F811
    """The real converter returns shapes, not an image, with neither live links nor HTML handlers."""
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
    page = ui.open("flow")
    frame = open_board(page)
    source = (
        'flowchart LR\nA["<img src=x onerror=alert(1)>"] --> B[Review]\nclick A href "https://example.com"'
    )
    made = world.agent_call("#flow", "drawer", "board", action="draw_mermaid", text=source)
    assert made.get("changed"), made
    elements = frame.locator("body").evaluate("window.sbBoard.api.getSceneElements()")
    imported = [e for e in elements if e["id"] in made["ids"]]
    assert any("<img src=x onerror=alert(1)>" in e.get("text", "") for e in imported)
    assert imported and all(e["type"] != "image" and e.get("link") is None for e in imported)
    original = next(e for e in elements if e["id"] == existing["ids"][0])
    assert min(e["x"] for e in imported) >= original["x"] + original["width"] + 80
    assert frame.locator("a[href],img,object,embed,iframe").count() == 0
    text = next(e for e in imported if e["type"] == "text" and "Review" in e["text"])
    # A real key edit to the converted label goes through the normal person change path.
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
