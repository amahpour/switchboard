"""``InProcBroker.start`` on a start that is too slow (tests/conftest.py, the e2e failure of #832).

A start that passed its deadline used to raise "failed to start: None" while the broker thread went on: the
caller's cleanup then removed the home under it, and the thread died on a missing ``files`` folder, which was
the only error anyone saw. The timeout must say what happened, and must not leave the thread running."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import InProcBroker


def test_a_start_past_its_deadline_says_so_and_is_stopped_before_it_returns(tmp_home: Path) -> None:
    """With no time to start, start() raises that it did not start within the deadline (not 'failed to start:
    None'), and by then the broker thread has stopped, so removing the home cannot break a start under way.
    Fails if the message hides the timeout, or the thread is still alive when start() raises."""

    class Slow(InProcBroker):
        START_TIMEOUT_S = 0.0

    b = Slow(tmp_home)
    with pytest.raises(RuntimeError, match=r"did not start within 0 s"):
        b.start()
    assert b.thread is not None and not b.thread.is_alive()
    b.tcp.close()


def test_the_default_deadline_is_a_ceiling_not_a_wait(tmp_home: Path) -> None:
    """A normal start returns as soon as the server is up, whatever the ceiling. Fails if the loop waited out
    the deadline."""
    import time

    assert InProcBroker.START_TIMEOUT_S >= 30
    t = time.monotonic()
    b = InProcBroker(tmp_home).start()
    try:
        assert time.monotonic() - t < 10
    finally:
        b.stop()
