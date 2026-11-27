"""A review board's ready line in a real browser (#985, DESIGN.md §37.21): hidden in a room with no
coordinator, shown under the board's header once a person names one (typing the command in the composer, as
they do), naming what is missing, and "Ready at <sha>" once the coordinator's CI report, the settled cards and
the reviewers' verdicts are all at the latest commit; hidden again when the coordinator is removed. A phone
does not scroll sideways. A console error, a page error or a CSP violation fails the test
(``test_web_ui.UI``). Marker ``e2e``."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from test_web_ui import PHONE, UI, no_horizontal_scroll, open_room, review_room, ui, world  # noqa: F401

pytestmark = pytest.mark.e2e

FIX = "9a1b2c3d4e5f"


def command(page: Page, text: str) -> None:
    page.fill("#input", text)
    page.press("#input", "Enter")


def test_the_ready_line_is_hidden_without_a_coordinator_and_names_what_is_missing_with_one(ui: UI) -> None:  # noqa: F811
    """The seeded board has no line until a person names a coordinator; then it names the CI report and the
    cards still open, and the line is plain text under the header. Removing the coordinator hides it again.
    Fails if the line shows in a room with no coordinator, does not follow the command, or names a card
    that is settled."""
    page = review_room(ui, "e2e-ready-hidden")
    page.click("#board-toggle")
    expect(page.locator("#board .board-card")).not_to_have_count(0)
    expect(page.locator("#board-ready")).to_have_count(0)
    command(page, "/coordinator claude-1 none")
    line = page.locator("#board-ready")
    expect(line).to_contain_text("Not ready: CI not reported passing at 9a1b2c3d4e5f by claude-1")
    expect(line).to_contain_text("F3 is contested")
    expect(line).to_contain_text("Q1 is open")
    expect(line).not_to_contain_text("F2")  # confirmed: settled
    expect(line).not_to_have_class(re.compile(r"\bok\b"))
    command(page, "/coordinator off")
    expect(page.locator("#board-ready")).to_have_count(0)
    no_horizontal_scroll(page)


def test_the_ready_line_reads_ready_at_the_commit_and_shows_the_verdicts_on_a_phone(ui: UI) -> None:  # noqa: F811
    """With the coordinator's CI report at the fix's commit, every card settled and the reviewer's verdict
    there, the line reads "Ready at <sha>" in the ok style, and the verdicts read beside it. Fails if a part
    is not counted, the line stays "Not ready", or the longer header makes the phone scroll sideways."""
    slug, room = "e2e-ready-line", "#e2e-ready-line"
    ui.world.create_room(room)
    ui.world.add_agents(room, ("claude-1", "codex-1"))
    call = ui.world.agent_call
    call(room, "codex-1", "review", action="open", url="https://example.com/shop/pull/7", head="4f2c9e1a7b30")
    call(
        room,
        "codex-1",
        "review",
        action="raise",
        title="Discount is taken after tax",
        where="shop/cart.py:40",
    )
    call(room, "claude-1", "review", action="concede", item="F1")
    call(room, "claude-1", "review", action="fix", item="F1", commit=FIX, proof="test_cart")
    page = ui.open(room=None, **PHONE)
    open_room(page, slug)
    page.click("#board-toggle")
    command(page, "/coordinator claude-1 none")
    expect(page.locator("#board-ready")).to_contain_text("F1 awaits its check")
    call(room, "codex-1", "review", action="confirm", item="F1")
    call(room, "codex-1", "review", action="verdict", head=FIX)
    expect(page.locator("#board-verdicts")).to_have_text("Verdicts: codex-1 at 9a1b2c3")
    expect(page.locator("#board-ready")).to_contain_text("CI not reported passing")
    call(room, "claude-1", "review", action="report", ci="passing", head=FIX + "0" * 28)
    line = page.locator("#board-ready")
    expect(line).to_have_text(f"Ready at {FIX}")
    expect(line).to_have_class(re.compile(r"\bok\b"))
    no_horizontal_scroll(page)
