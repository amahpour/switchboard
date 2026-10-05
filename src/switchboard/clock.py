"""Clocks: the engine reads time only through this protocol (DESIGN.md §8.2, §12.1).

The engine is synchronous and reads "store + clock + cfg only" (§8.2): the broker
gives it a SystemClock, and every delivery rule is tested against a FakeClock
instead (§12.1), so a test can step time rather than sleep for it.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()
