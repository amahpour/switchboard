"""A background wait's result is not offered again at its prompt (#558, DESIGN.md §6.1)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from conftest import FakeClock, InProcBroker
from test_agents_http import make_token
from test_agents_http_claude import SESSION_1, Claude
from test_agents_http_devin import open_waits
from test_agents_http_wake import claude_hook, until
from test_mcp_agent import q, states


def test_background_wait_result_does_not_return_as_hook_context(tmp_home: Path, clock: FakeClock) -> None:
    """Use the Claude wire and the broker's clock: background, return a message, then start its
    notification turn with no PostToolUse. Before #558 the hook repeats the wait's text."""
    b = InProcBroker(tmp_home, clock=clock).start()
    c = Claude(b, make_token(b), SESSION_1)
    try:
        b.call("room.create", {"name": "#build"})
        c.call("join", room="#build", screen_name="claude-1")
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(c.call, "wait", room="#build", timeout_s=600)
            until(lambda: open_waits(b, "claude-1") == 1)
            # Leave the client cap to its own tests: the baseline's 110 s would otherwise end this
            # call before reproducing the engine bug. Only this test's sink deadline is extended.
            b.on_loop(lambda: setattr(b.state.engine.sinks.open_sinks()[0], "deadline", clock.now() + 600))
            b.on_loop(clock.advance, 121)
            assert claude_hook(c, event="Stop") == "{}"
            b.on_loop(clock.advance, 9)
            mid = b.call("human.say", {"room": "#build", "text": "@claude-1 answer exactly once"})["id"]
            result = waiting.result(30)
        assert "answer exactly once" in result["text"]
        b.on_loop(clock.advance, 0.1)
        prompt = claude_hook(c, event="UserPromptSubmit")
        assert prompt == "{}", prompt
        assert states(b, "claude-1")[mid] == "in_context"
        assert q(b, "SELECT state FROM batches WHERE id = ?", result["batch_id"])[0][0] == "confirmed"
        assert not q(b, "SELECT id FROM batches WHERE path = 'hook_ups'")
        assert c.call("read", room="#build")["count"] == 0
    finally:
        c.c.close()
        b.stop()
