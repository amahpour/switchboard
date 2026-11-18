"""The Zoom participant list in the patched joinly ``server.py`` (#824, DESIGN.md section 52.7).

The block runs inside joinly, which is not installed here, so it is cut out of the file by its markers and
run with a fake Playwright page: the button (a visible match, a hidden one that needs a click without
visibility), the "More" menu, the Alt+U shortcut, a panel that is already open, and the one diagnostic line
when nothing works."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
START = "# --- switchboard Zoom participant list (#824"
END = "# --- end of the Zoom participant list"


class Participant:
    def __init__(self, name: str, infos: list[str]) -> None:
        self.name, self.infos = name, infos


def namespace() -> dict[str, Any]:
    text = (ROOT / "deploy" / "joinly" / "server.py").read_text()
    block = text[text.index(START) : text.index(END)].replace("\n_install_zoom_participants()\n", "\n")
    sys.modules["joinly"] = types.ModuleType("joinly")
    fake_types: Any = types.SimpleNamespace(MeetingParticipant=Participant)
    sys.modules["joinly.types"] = fake_types
    ns: dict[str, Any] = {"logger": logging.getLogger("joinly.test"), "Any": Any, "re": re}
    exec(compile(block, "server.py", "exec"), ns)  # noqa: S102
    return ns


class El:
    """One element: a visibility, a box, a click that may open the panel, and (for a row) its label."""

    def __init__(
        self, world: "World", visible: bool = True, opens: str | None = None, label: str = ""
    ) -> None:
        self.world, self.visible, self.opens, self.label, self.clicks, self.js = (
            world,
            visible,
            opens,
            label,
            0,
            0,
        )

    async def is_visible(self) -> bool:
        return self.visible

    async def bounding_box(self) -> dict[str, float] | None:
        return {"x": 1.0, "y": 2.0, "width": 30.0, "height": 40.0} if self.visible else None

    async def scroll_into_view_if_needed(self, timeout: int = 0) -> None:
        return None

    async def click(self, timeout: int = 0) -> None:
        self.clicks += 1
        self.world.hit(self)

    async def evaluate(self, js: str) -> None:
        self.js += 1
        self.world.hit(self)

    async def get_attribute(self, name: str) -> str | None:
        return self.label


class Loc:
    def __init__(self, items: list[El], world: "World | None" = None) -> None:
        self.items, self.world = items, world

    async def count(self) -> int:
        return len(self.items)

    def nth(self, i: int) -> El:
        return self.items[i]

    @property
    def first(self) -> "Loc":
        return Loc(self.items[:1], self.world)

    async def is_visible(self) -> bool:
        return bool(self.items) and await self.items[0].is_visible()

    async def all(self) -> list[El]:
        return self.items

    def locator(self, css: str) -> "Loc":
        assert self.world is not None
        return Loc(self.world.rows, self.world)


class World:
    """A page: the buttons by role and a panel that exists once something opened it."""

    def __init__(self) -> None:
        self.buttons: list[El] = []
        self.more: list[El] = []
        self.entries: list[El] = []
        self.panel = El(self, visible=False)
        self.rows = [El(self, label="Alice Example, host"), El(self, label="Bob Example"), El(self, label="")]
        self.keys: list[str] = []
        self.opened_by: str | None = None
        self.key_opens = False
        self.dom_elements: list[El] = []  # elements only the page-side click can reach
        self.dom: list[tuple[str, str]] = []
        self.page = Page(self)

    def hit(self, el: El) -> None:
        if el.opens:
            self.panel.visible = not self.panel.visible if el.opens == "toggle" else True
            self.opened_by = el.opens


class Keyboard:
    def __init__(self, world: World) -> None:
        self.world = world

    async def press(self, key: str) -> None:
        self.world.keys.append(key)
        if self.world.key_opens and key == "Alt+U":
            self.world.panel.visible = not self.world.panel.visible


class Mouse:
    async def click(self, x: int, y: int) -> None:
        return None


class Page:
    def __init__(self, world: World) -> None:
        self.world, self.keyboard, self.mouse = world, Keyboard(world), Mouse()

    def locator(self, css: str) -> Loc:
        return Loc([self.world.panel], self.world)

    def get_by_role(self, role: str, name: Any) -> Loc:
        w = self.world
        if role == "button" and name.search("participants list"):
            return Loc(w.buttons, w)
        if role == "button" and name.search("More meeting controls"):
            return Loc(w.more, w)
        if role in ("menuitem", "button") and name.search("participants"):
            return Loc(w.entries if role == "menuitem" else [], w)
        return Loc([], w)

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def evaluate(self, js: str, arg: Any) -> bool:
        """The page-side DOM click: finds an element by its label, whether or not a role locator sees it."""
        self.world.dom.append(tuple(arg))
        src, sel = arg
        hit = [e for e in self.world.dom_elements if re.search(src, e.dom_label, re.IGNORECASE)]
        if not hit:
            return False
        self.world.hit(hit[0])
        return True


class Controller:
    async def _activate_controls(self, page: Any) -> None:
        return None

    async def get_participants(self, page: Any) -> list[Any]:
        raise AssertionError("joinly's own get_participants must be replaced")


def run(w: World) -> list[Participant]:
    ns = namespace()
    return asyncio.run(ns["_zoom_get_participants"](Controller(), w.page))


def names(people: list[Participant]) -> list[tuple[str, list[str]]]:
    return [(p.name, p.infos) for p in people]


EXPECTED = [("Alice Example", [" host"]), ("Bob Example", [])]


def test_a_panel_that_is_already_open_is_read_without_touching_anything() -> None:
    """Rows come from the panel, a row with no label is left out, nothing is clicked or typed. Fails if it
    opens a panel that is open (which would close it)."""
    w = World()
    w.panel.visible = True
    w.buttons = [El(w, opens="toggle")]
    assert names(run(w)) == EXPECTED
    assert w.buttons[0].clicks == 0 and w.keys == []


def test_a_visible_button_opens_the_panel_and_it_is_closed_again() -> None:
    """The ordinary path, with two matches (one hidden): the visible one is clicked, the list read, and the
    button clicked again to put the panel back. Fails if a second match breaks it or the panel is left."""
    w = World()
    w.buttons = [El(w, visible=False), El(w, opens="toggle")]
    assert names(run(w)) == EXPECTED
    assert w.buttons[1].clicks == 2 and w.buttons[0].clicks == 0 and not w.panel.visible


def test_a_button_that_is_on_the_page_but_not_visible_is_clicked_without_visibility() -> None:
    """The live failure of #824: the button is in the page (a hidden control bar) and is_visible() says no.
    The fallback click runs in the page and opens it. Fails if a hidden button ends in the old error."""
    w = World()
    w.buttons = [El(w, visible=False, opens="button")]
    assert names(run(w)) == EXPECTED
    assert w.buttons[0].js >= 1 and w.buttons[0].clicks == 0


def test_an_element_the_role_locator_cannot_see_is_found_by_its_label_in_the_page() -> None:
    """A role locator skips hidden elements, and the live button was one: nothing matches by role, but a
    page-side click on the button whose aria-label says "participants list" opens the panel. Fails if only
    role locators are tried (the #824 error again), or the page click gets anything but the label pattern."""
    w = World()
    hidden = El(w, visible=False, opens="dom")
    hidden.dom_label = "open the participants list pane, 2 participants"
    w.dom_elements = [hidden]
    assert names(run(w)) == EXPECTED
    assert w.opened_by == "dom" and w.dom[0][1] == "button" and "participants list" in w.dom[0][0]


def test_the_more_menu_is_tried_when_the_button_is_not_on_the_bar() -> None:
    """With no participants button on the bar, "More meeting controls" is clicked and the menu entry opens the
    panel. Fails if the menu route is missing."""
    w = World()
    w.more = [El(w)]
    w.entries = [El(w, opens="more")]
    assert names(run(w)) == EXPECTED
    assert w.more[0].clicks == 1 and w.entries[0].clicks == 1


def test_alt_u_is_the_last_way_and_is_pressed_again_to_close() -> None:
    """With neither a button nor a menu entry, Alt+U opens the panel when Zoom's web client honours it, and is
    pressed again to put it back. Fails if the shortcut is never tried."""
    w = World()
    w.key_opens = True
    assert names(run(w)) == EXPECTED
    assert w.keys == ["Alt+U", "Alt+U"] and not w.panel.visible


def test_when_nothing_opens_it_raises_the_old_error_and_logs_one_line_of_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing works: the same RuntimeError as before (captions already handle it), and ONE warning per page
    with counts, visibility and boxes, never a label or an address, however often it is asked. Fails if it
    logs a name, logs twice for one page, or raises something else."""
    w = World()
    w.buttons = [El(w, visible=False), El(w, visible=False, label="Secret Name")]
    ns = namespace()
    with caplog.at_level(logging.WARNING, logger="joinly.test"):
        for _ in range(2):
            with pytest.raises(RuntimeError, match="Participants button not found or not visible."):
                asyncio.run(ns["_zoom_get_participants"](Controller(), w.page))
    lines = [r.getMessage() for r in caplog.records if "did not open" in r.getMessage()]
    assert len(lines) == 1
    assert "button 2 [h none; h none]" in lines[0] and "more 0 []" in lines[0]
    assert "Secret" not in lines[0] and "http" not in lines[0]


def test_install_replaces_the_controllers_method_once() -> None:
    """The mounted zoom.py is not changed: server.py swaps the method on import, once. Fails if a second
    install wraps again, or a joinly without the controller raises."""
    ns = namespace()
    cls = Controller

    def load(name: str) -> Any:
        if name != "joinly.providers.browser.platforms.zoom":
            raise ImportError(name)
        return types.SimpleNamespace(ZoomBrowserPlatformController=cls)

    assert ns["_install_zoom_participants"](load) is True
    first = cls.get_participants
    assert ns["_install_zoom_participants"](load) is True and cls.get_participants is first
    assert ns["_install_zoom_participants"](lambda n: (_ for _ in ()).throw(ImportError(n))) is False
