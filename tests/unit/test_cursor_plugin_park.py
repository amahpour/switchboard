"""The ten minutes that Cursor's docs, tier note and join text promise (DESIGN.md §9.4, §49.6, #519).

The number lives in two places: the plugin's hook script asks the broker to hold its Stop hook for ``wait_s``
seconds, and ``hooks.json`` gives the hook a ``timeout`` a little over that. If either moves, the limit the
adapter tells a person and an agent ("about 10 minutes") stops being true, so this fails until the words and
the numbers agree again."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

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
