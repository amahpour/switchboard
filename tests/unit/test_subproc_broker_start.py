"""``SubprocBroker.start`` waits for the socket, with a ceiling for a loaded machine (#857).

The fixture used to give up after 15 s. Under load the child was still starting: one run had
already printed the ready line (that line is printed before the lifespan creates the socket)
and one had printed nothing. The ceiling has to hold, and a start returns when the socket
answers, not when the clock runs out.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import SubprocBroker


def test_a_start_past_its_deadline_says_so_and_is_stopped(tmp_home: Path) -> None:
    """With no time to start, start() raises that it did not start within the deadline, and the
    child is gone. Fails if the message hides the timeout, or the process is still alive when
    start() raises."""

    class Slow(SubprocBroker):
        START_TIMEOUT_S = 0.0

    b = Slow(tmp_home)
    with pytest.raises(RuntimeError, match=r"did not start within 0 s"):
        b.start()
    assert b.proc is not None and b.proc.poll() is not None


def test_the_default_deadline_is_a_ceiling_not_a_wait(tmp_home: Path) -> None:
    """A normal start returns as soon as the socket answers, whatever the ceiling. Fails if the
    loop waited out the deadline, or the ceiling is still the 15 s that #857 ran out under load."""
    assert SubprocBroker.START_TIMEOUT_S >= 60
    t = time.monotonic()
    b = SubprocBroker(tmp_home).start()
    try:
        assert b.port
        assert time.monotonic() - t < b.START_TIMEOUT_S
    finally:
        b.kill()
