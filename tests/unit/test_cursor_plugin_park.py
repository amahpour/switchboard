"""The ten minutes that Cursor's docs, tier note and join text promise (DESIGN.md §9.4, §49.6, #519).

The number lives in two places: the plugin's hook script asks the broker to hold its Stop hook for ``wait_s``
seconds, and ``hooks.json`` gives the hook a ``timeout`` a little over that. If either moves, the limit the
adapter tells a person and an agent ("about 10 minutes") stops being true, so this fails until the words and
the numbers agree again."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from conftest import FakeClock
from engine_agents import listener, result, stop, woke
from engine_world import World

from switchboard.adapters.https import CURSOR_IDLE_WHY, PARK_MARGIN_S, HttpsCursorAdapter
from switchboard.config import Config
from switchboard.models import HookEvent

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "cursor"


def _stop_timeout() -> float:
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    (stop,) = hooks["hooks"]["stop"]
    return float(stop["timeout"])


def _script_wait_s() -> float:
    m = re.search(r'body\["wait_s"\] = (\d+)', (PLUGIN / "lib" / "hook.py").read_text())
    assert m is not None, "the Stop hook no longer sends wait_s"
    return float(m.group(1))


def test_the_stop_park_is_about_ten_minutes() -> None:
    """The broker holds a Cursor Stop for the script's ``wait_s`` less its margin, inside the hook's timeout,
    and that is between nine and eleven minutes: what 'about 10 minutes' means everywhere it is written."""
    wait_s, timeout = _script_wait_s(), _stop_timeout()
    assert wait_s + PARK_MARGIN_S <= timeout, "the hook would be killed before the broker's answer"
    ev = HookEvent(harness="cursor", event="stop", max_wait_s=wait_s)
    park = HttpsCursorAdapter(Config(human_name="alice")).park_s(
        cast(Any, None), ev
    )  # the participant is not read
    assert park is not None and 540 <= park <= 660


# ------------------------------------------------------------ the engine: what a member shows after the park
def test_a_cursor_that_idled_past_its_park_shows_the_limit_until_it_stops_again(tmp_path: Path) -> None:
    """#519 with a FakeClock: Cursor's Stop park runs out with nothing, so the turn is over and a mention then
    queues (nothing starts another park). Members shows Cursor's own reason, and the next Stop, which only
    typing can cause, hands over what waited. Fails if the reason is the generic one that says nothing about
    the ten minutes or about typing."""
    clock = FakeClock()
    w = World(tmp_path, clock, Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0))
    p, m, park = listener(w, "cursor-1", "cursor", status="idle")
    clock.advance(626)
    w.engine.sink_timeout(park.sink_id)  # the hook answers with nothing
    assert result(w, park) is not None and not woke(w, park)
    assert w.engine.parked_reason(m.id) is None  # nothing waiting yet, nothing to call deaf
    w.human("@cursor-1 are you there")
    assert w.engine.parked_reason(m.id) == CURSOR_IDLE_WHY
    assert "about 10 min" in CURSOR_IDLE_WHY and "type in Cursor" in CURSOR_IDLE_WHY
    again = stop(w, p)  # the person typed and the turn ended: its Stop parks, and the mention wakes it
    assert again is not None and again.kind == "park" and woke(w, again)
    assert w.engine.parked_reason(m.id) is None


def test_stop_retries_a_restart_within_the_original_park(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    """A Cursor Stop survives 503 and a connection refusal, and keeps its original deadline."""
    spec = importlib.util.spec_from_file_location("cursor_hook", PLUGIN / "lib" / "hook.py")
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    requests = []
    now = [0.0]

    def pause(seconds: float) -> None:
        now[0] += seconds

    def answer(request: Any, *, timeout: float) -> io.BytesIO:
        requests.append((json.loads(request.data)["wait_s"], timeout))
        if len(requests) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "restart", {}, io.BytesIO())
        if len(requests) == 2:
            raise urllib.error.URLError(ConnectionRefusedError())
        return io.BytesIO(b'{"followup_message":"one wake"}')

    monkeypatch.setenv("SWITCHBOARD_URL", "http://switchboard.localhost")
    monkeypatch.setenv("SWITCHBOARD_AGENT_TOKEN", "token")
    monkeypatch.setattr(sys, "argv", ["hook.py", "stop"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"conversation_id":"one"}'))
    monkeypatch.setattr(urllib.request, "urlopen", answer)
    monkeypatch.setattr(hook, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=pause), raising=False)
    hook.main()
    assert json.loads(capsys.readouterr().out) == {"followup_message": "one wake"}
    assert [wait for wait, _ in requests] == [630, 629, 627]
    assert all(timeout > wait for wait, timeout in requests)


@pytest.mark.parametrize("status", [0, 504])
def test_stop_retries_repeated_proxy_cuts(monkeypatch: Any, capsys: Any, status: int) -> None:
    """Four 60-second proxy cuts leave time for a wake in the same Cursor Stop."""
    spec = importlib.util.spec_from_file_location("cursor_hook", PLUGIN / "lib" / "hook.py")
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    now = 0.0
    requests = []

    def pause(seconds: float) -> None:
        nonlocal now
        now += seconds

    def answer(request: Any, *, timeout: float) -> io.BytesIO:
        nonlocal now
        requests.append(json.loads(request.data)["wait_s"])
        if len(requests) == 5:
            return io.BytesIO(b'{"followup_message":"wake after proxy cuts"}')
        now += 60
        if status:
            raise urllib.error.HTTPError(request.full_url, status, "proxy timeout", {}, io.BytesIO())
        raise ConnectionResetError()

    monkeypatch.setenv("SWITCHBOARD_URL", "http://switchboard.localhost")
    monkeypatch.setenv("SWITCHBOARD_AGENT_TOKEN", "token")
    monkeypatch.setattr(urllib.request, "urlopen", answer)
    monkeypatch.setattr(hook, "time", SimpleNamespace(monotonic=lambda: now, sleep=pause), raising=False)
    assert hook.park({"wait_s": 630}) == {"followup_message": "wake after proxy cuts"}
    assert requests == [630, 570, 510, 450, 390]
    assert capsys.readouterr().err == ""
