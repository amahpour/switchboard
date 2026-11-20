"""The one output-side defense kept for #890: the line a move posts for another agent (``Boards._say``) is
scrubbed, though the upgrade cleaned the stored rows and a board's input is checked. The line is where stored
card text is joined and cut into a message that wakes an agent, so it is cheap to keep it clean even if a row
is ever written another way. The row here is written straight to the store, as nothing now does. Built from
pieces, so no scanner reads this file as a leak."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from test_review_board import FAST, PR

SECRET = "ghp" + "_" + "aB3" * 12


async def test_a_line_posted_from_a_card_holds_no_secret(tmp_home: Path) -> None:
    """A contest builds its line from the card's title. Fails if a title that holds a secret is posted in
    the room as written."""
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    try:
        assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
        st = b.state.store

        def plant() -> None:
            rv = st.open_review(st.get_room("#build").id, PR, "", "codex-1")
            st.add_review_item(rv["id"], "finding", "raised", title=f"leaks {SECRET}", raised_by="codex-1")

        b.on_loop(plant)
        async with FakeAgent(b.home, "la") as author, FakeAgent(b.home, "lr") as reviewer:
            await author.join("#build", "claude-1")
            await reviewer.join("#build", "codex-1")
            r = await author.call("review", room="#build", url=PR, action="contest", item="F1", reason="no")
            assert r.get("ok") is not False, r
        messages = json.dumps(web.get("/api/rooms/build/messages").json()["messages"])
        assert SECRET not in messages and "[redacted: github token]" in messages
    finally:
        web.close()
        b.stop()
