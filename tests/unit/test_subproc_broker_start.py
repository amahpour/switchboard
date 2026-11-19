"""``SubprocBroker.start`` waits for the socket, and that wait can pass the old 15 s (#857).

The fixture used to give up after 15 s. Under load the child was still starting. A real start usually
answers in a couple of seconds, so timing one cannot tell a 60 s ceiling from a loop that still stops
at 15. These tests stub the process and the clock, and no broker is spawned: a test that needs a
process belongs in ``tests/integration``.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import SubprocBroker


class _Clock:
    """Moves only when the wait loop sleeps, and stops a loop that never checks its deadline."""

    def __init__(self, limit: float) -> None:
        self.now = 0.0
        self.limit = limit

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.now > self.limit:
            raise RuntimeError(f"the wait loop ran to {self.now:g} s")


class _Proc:
    """The child ``start`` polls. ``code`` is None while it is running."""

    def __init__(self, code: int | None = None) -> None:
        self.code = code
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.code = -sig

    def wait(self, timeout: float | None = None) -> int:
        return 0 if self.code is None else self.code

    def kill(self) -> None:
        self.code = -9


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    proc: _Proc,
    ping: Callable[..., dict[str, int] | None],
) -> None:
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr("switchboard.mcp.client.ping", ping)


def test_start_returns_when_the_socket_answers_after_the_old_deadline(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ping stays down through 15 s, then answers. Fails if the loop still gives up at 15 s (a
    hardcoded 15, or ``min(START_TIMEOUT_S, 15)``) while the ceiling says 60, or if it waits out
    the ceiling instead of returning on that answer."""
    clock = _Clock(SubprocBroker.START_TIMEOUT_S + 1)
    proc = _Proc()

    def ping(path: object, timeout: float = 1.0) -> dict[str, int] | None:
        if clock.now < 15:
            return None
        return {"port": 4321, "pid": 99}

    _patch(monkeypatch, clock, proc, ping)
    assert SubprocBroker.START_TIMEOUT_S > 15
    b = SubprocBroker(tmp_home)
    b.start()
    try:
        assert b.port == 4321
        assert b.pid == 99
        assert clock.now >= 15
        assert clock.now < b.START_TIMEOUT_S
        assert proc.signals == []
    finally:
        b.kill()


def test_a_start_past_its_deadline_says_so_and_is_stopped(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ping never answers and the child stays up, so ``start`` reaches the ceiling. Fails if the
    message hides that deadline, calls the timeout a child exit, or leaves the process running."""
    ceiling = SubprocBroker.START_TIMEOUT_S
    clock = _Clock(ceiling + 1)
    proc = _Proc()
    (tmp_home / "subproc.out").write_text("still starting\n")

    def ping(path: object, timeout: float = 1.0) -> None:
        return None

    _patch(monkeypatch, clock, proc, ping)
    b = SubprocBroker(tmp_home)
    with pytest.raises(RuntimeError, match=rf"did not start within {ceiling:g} s") as raised:
        b.start()
    text = str(raised.value)
    assert "exited" not in text
    assert "still starting" in text
    assert proc.signals
    assert clock.now >= ceiling
    b.kill()


def test_an_early_exit_is_not_reported_as_a_slow_start(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that exits before the socket answers is reported with its exit code. Fails if that
    shares the deadline's 'did not start within' message, so a crash looks like another slow start."""
    clock = _Clock(SubprocBroker.START_TIMEOUT_S + 1)
    proc = _Proc(code=2)
    (tmp_home / "subproc.out").write_text("refused\n")

    def ping(path: object, timeout: float = 1.0) -> None:
        return None

    _patch(monkeypatch, clock, proc, ping)
    b = SubprocBroker(tmp_home)
    with pytest.raises(RuntimeError, match=r"exited 2 before the socket answered") as raised:
        b.start()
    text = str(raised.value)
    assert "did not start within" not in text
    assert "refused" in text
    assert clock.now < 1
    assert proc.signals == []
    b.kill()
