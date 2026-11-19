"""Live Cursor Agent over HTTPS (DESIGN.md §12.4). Opt-in: ``SWITCHBOARD_LIVE=cursor uv run pytest -m live``.

One real ``agent`` session in a private tmux server, with project config made from this checkout's
``plugins/cursor`` and a temp $HOME (only the login's ``~/.config/cursor`` is the real one), against a
throwaway broker in a temp home. Cursor's quota may be used up: the first test needs no model (it lists
the broker's tools over HTTP with the token), and the rest are skipped, with the reason, when a one-word
call to Cursor's model fails (always on macOS, where its login is in the Keychain; run them on Linux, #530).
The real ``~/.cursor`` must not change.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from harness import scenarios
from harness.cursor import CursorRun
from harness.scenarios import delivery, latency, nonce, record, say_text
from harness.world import skip_reason

pytestmark = pytest.mark.live

_why = skip_reason("cursor", "agent")
if _why:
    pytest.skip(_why, allow_module_level=True)


@pytest.fixture(scope="module")
def run() -> Iterator[CursorRun]:
    r = CursorRun("cursor-1")
    try:
        r.start()
        yield r
    finally:
        r.stop()
    assert r.results["drift_fail"] == [], f"the user's own Cursor config changed: {r.results['drift_fail']}"
    assert r.results["leftover_processes"] == [], "processes of the run outlived it"


@pytest.fixture(scope="module")
def model(run: CursorRun) -> CursorRun:
    """The run, once the account can make a call; the tests that ask the model for something use it."""
    why = run.model_unavailable()
    if why:
        pytest.skip(f"Cursor can't make a call: {why}")
    return run


def test_1_the_broker_serves_its_tools_to_cursor_over_http(run: CursorRun) -> None:
    """No model needed: the project's server is approved, Cursor connects with the token and lists the 11
    tools (``switchboard_join``, ``switchboard_say`` ...), and the broker saw the token used."""
    scenarios.cursor_lists_tools(run)


def test_2_join_and_say(model: CursorRun) -> None:
    scenarios.join_and_say(model)


def test_3_woken_while_idle_by_a_mention(model: CursorRun) -> None:
    scenarios.idle_wake(model)


def test_3b_still_woken_after_more_than_eleven_minutes_parked(model: CursorRun) -> None:
    """SLOW: about 12 minutes of real time (#885). The Stop hook was held 10 minutes before the plugin's wait
    was raised to an hour, so an idle Cursor stopped hearing the room then. Park it, check the broker still
    shows it parked 11.5 minutes on (no re-park, no typing), then an @mention wakes it and it answers. Fails
    on a plugin that still holds 630 s, where the park has ended by then and the mention only queues."""
    assert model.joined, "the join scenario did not complete"
    model.wait_settled()
    since: list[float] = []

    def parked_long() -> bool:
        m = model.member()
        if not (m and m["parked"]):
            since.clear()
            return False
        if not since:
            since.append(time.monotonic())
        return time.monotonic() - since[0] >= 11.5 * 60

    model.wait(parked_long, 15 * 60, "Cursor to stay parked for 11.5 minutes", step=5)
    word = f"pong-{nonce()}"
    asked = model.mention(say_text(word))
    answer = model.reply(word, after=asked)
    d = delivery(model, asked)
    assert d["path"] in model.wake_paths, (d["path"], sorted(model.wake_paths), d)
    record(model, "long_park_wake", path=d["path"], seconds=latency(model, asked, answer))


def test_4_a_mention_mid_task_arrives_after_the_next_tool_call(model: CursorRun) -> None:
    scenarios.mid_task(model)


def test_5_quit_shows_offline(model: CursorRun) -> None:
    scenarios.quit_offline(model)


def test_6_resume_comes_back_and_is_woken(model: CursorRun) -> None:
    scenarios.resume(model)


def test_7_a_revoked_token_ends_it(model: CursorRun) -> None:
    scenarios.revoked_token(model)
