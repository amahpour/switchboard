"""A trace of each screenshot that fails (#1001): ``ui_shots.py --traces DIR`` records every browser
context it opens, and keeps the trace of one that closes while a shot is failing. A shot that hangs in CI
then shows why in the job's artifact: the page's screenshots, DOM snapshots, console and network up to the
moment it stopped. A shot that passes keeps nothing, so the artifact holds only what a failure needs.

#1001 is why: a Settings click in CI's screenshot step stopped at Playwright's "performing click action"
for 20 s, once in 40 failed runs, and never again locally. A call log alone can't say what the page was
doing; a trace can.

Loaded by ``ui_shots.py`` (run by hand and by CI's ``frontend`` job) and by its unit test. It needs no
Playwright of its own: it wraps the browser it is given."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any


def _shot_name() -> str:
    """The ``shoot_*`` function opening a context: the nearest one up the stack, since some shots open
    theirs in a helper of their own."""
    frame = inspect.currentframe()
    while frame is not None:
        if frame.f_code.co_name.startswith("shoot"):
            return frame.f_code.co_name
        frame = frame.f_back
    return "shot"


class TracedBrowser:
    """A Playwright browser whose new contexts are traced (the module's docstring says why). Everything
    else is the wrapped browser's own."""

    def __init__(self, browser: Any, traces: Path) -> None:
        self._browser = browser
        self._traces = traces
        self._count = 0
        self._open: list[tuple[Any, Path]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser, name)

    def new_context(self, *args: Any, **kwargs: Any) -> Any:
        ctx = self._browser.new_context(*args, **kwargs)
        self._count += 1
        entry = (ctx, self._traces / f"{_shot_name()}-{self._count}.zip")
        ctx.tracing.start(screenshots=True, snapshots=True)
        self._open.append(entry)
        close = ctx.close

        def close_keeping_a_failure(*a: Any, **k: Any) -> Any:
            # a shot closes its context in a `finally`: an exception on its way out is the shot failing
            if entry in self._open:
                self._open.remove(entry)
                self._stop(ctx, entry[1] if sys.exc_info()[0] is not None else None)
            return close(*a, **k)

        ctx.close = close_keeping_a_failure
        return ctx

    def close(self, *args: Any, **kwargs: Any) -> Any:
        """Closing the browser while a shot fails keeps the traces of the contexts still open: those of a
        shot that raised before it closed its own."""
        failing = sys.exc_info()[0] is not None
        for ctx, path in self._open:
            self._stop(ctx, path if failing else None)
        self._open.clear()
        return self._browser.close(*args, **kwargs)

    def _stop(self, ctx: Any, path: Path | None) -> None:
        try:
            if path is None:
                ctx.tracing.stop()  # a passing shot: its trace is dropped
                return
            self._traces.mkdir(parents=True, exist_ok=True)
            ctx.tracing.stop(path=str(path))
            print(f"kept the trace of a failed shot: {path}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - a trace that can't be kept never hides the shot's own error
            print(f"couldn't keep a trace ({path}): {e}", file=sys.stderr)
