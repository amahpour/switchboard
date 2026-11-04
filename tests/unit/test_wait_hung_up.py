"""The watch on a ``wait()``'s request (#559, DESIGN.md §6.1) outside any request.

The watch itself, on a real connection, is ``tests/integration/test_wait_hangup.py``. This is the one case
that has no connection to close."""

from __future__ import annotations

import asyncio

from switchboard.broker.agent_http import HttpTools


async def test_a_call_that_did_not_come_over_http_never_hangs_up() -> None:
    """A tool called with no HTTP request behind it (a unit test's, the socket's) has no connection that can
    close, so the watch never says it has. Fails if it returns at once, which would take every such wait
    back the moment it started."""
    watch = asyncio.ensure_future(HttpTools._hung_up())
    await asyncio.sleep(0)  # let it start: it has nothing to wait for but an event nobody sets
    await asyncio.sleep(0)
    assert not watch.done()
    watch.cancel()
    await asyncio.gather(watch, return_exceptions=True)
    assert watch.cancelled()
