"""Signal ownership during graceful restart (DESIGN.md §15, §49)."""

from __future__ import annotations

import signal
from pathlib import Path

import uvicorn
from sse_starlette.sse import AppStatus

from switchboard.broker.app import create_app
from switchboard.broker.restarts import BrokerServer
from switchboard.config import Config
from switchboard.paths import Paths


def test_sigterm_leaves_sse_open_until_the_broker_can_answer(tmp_home: Path) -> None:
    """The SSE library must not cancel open POST streams before BrokerServer releases their sinks."""
    app = create_app(Paths.from_home(tmp_home), Config())
    server = BrokerServer(uvicorn.Config(app), app.state.broker)
    original_exit = AppStatus.should_exit
    automatic = AppStatus.enable_automatic_graceful_drain
    try:
        AppStatus.should_exit = False
        AppStatus.enable_automatic_graceful_drain_mode()
        with server.capture_signals():
            try:
                server.handle_exit(signal.SIGTERM, None)
                assert server.should_exit
                assert not AppStatus.should_exit, "SSE streams closed before the broker drained its waits"
            finally:
                # Uvicorn normally replays this signal after cleanup; this unit test sends none.
                server._captured_signals.clear()
        assert AppStatus.enable_automatic_graceful_drain
    finally:
        AppStatus.should_exit = original_exit
        AppStatus.enable_automatic_graceful_drain = automatic
