"""Polling helper shared by the integration and e2e tests."""

from __future__ import annotations

import time
from typing import Any


def wait_for(fn: Any, timeout: float = 10.0, interval: float = 0.05, what: str = "condition") -> Any:
    """Call ``fn`` until it returns something truthy, and return that; fail after ``timeout`` seconds.

    For state that changes in another thread or process (a broker, a browser), where there is no
    event to wait on and the test must not sleep for a fixed time."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}: {last!r}")
