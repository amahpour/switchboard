"""docs/media/shot_traces.py (#1001): ``ui_shots.py --traces DIR`` keeps a Playwright trace of a screenshot
that fails, and nothing of one that passes, so a shot that hangs in CI leaves the page's console, DOM
and screenshots in the job's artifact.

The script is run by hand and by CI, never under pytest, so it is loaded here as a file, around a fake
browser: no Playwright, no broker."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "docs" / "media" / "shot_traces.py"
spec = importlib.util.spec_from_file_location("shot_traces", SCRIPT)
assert spec and spec.loader
shot_traces = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shot_traces)


class FakeTracing:
    def __init__(self, fail: bool = False) -> None:
        self.started: dict[str, Any] | None = None
        self.stopped: list[str | None] = []
        self.fail = fail

    def start(self, **kwargs: Any) -> None:
        self.started = kwargs

    def stop(self, path: str | None = None) -> None:
        if self.fail:
            raise RuntimeError("the trace couldn't be written")
        self.stopped.append(path)
        if path:
            Path(path).write_bytes(b"a trace")


class FakeContext:
    def __init__(self, fail_trace: bool) -> None:
        self.tracing = FakeTracing(fail_trace)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, fail_trace: bool = False) -> None:
        self.contexts: list[FakeContext] = []
        self.closed = False
        self.version = "fake 1.0"
        self.fail_trace = fail_trace

    def new_context(self, **kwargs: Any) -> FakeContext:
        self.contexts.append(FakeContext(self.fail_trace))
        return self.contexts[-1]

    def close(self) -> None:
        self.closed = True


def shoot_passes(browser: Any) -> None:
    ctx = browser.new_context(color_scheme="light")
    try:
        pass
    finally:
        ctx.close()


def shoot_hangs(browser: Any) -> None:
    ctx = browser.new_context(color_scheme="dark")
    try:
        raise TimeoutError("Page.click: Timeout 20000ms exceeded.")
    finally:
        ctx.close()


def shoot_from_a_helper(browser: Any) -> None:
    def one_pass() -> None:
        ctx = browser.new_context()
        try:
            raise TimeoutError("hung")
        finally:
            ctx.close()

    one_pass()


def kept(tmp_path: Path) -> list[str]:
    folder = tmp_path / "traces"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_a_failed_shot_keeps_its_trace_and_a_passing_one_keeps_nothing(tmp_path: Path) -> None:
    """The bug it guards: a shot that hangs in CI leaves only Playwright's call log, which can't say what
    the page was doing (#1001). Every context is traced with screenshots and DOM snapshots; the one that
    closes while its shot raises keeps its trace, named after the shot, and the passing one's is dropped."""
    fake = FakeBrowser()
    browser = shot_traces.TracedBrowser(fake, tmp_path / "traces")
    shoot_passes(browser)
    with pytest.raises(TimeoutError):
        shoot_hangs(browser)
    passed, failed = fake.contexts
    assert passed.tracing.started == {"screenshots": True, "snapshots": True}
    assert passed.tracing.stopped == [None] and passed.closed
    assert failed.tracing.stopped == [str(tmp_path / "traces" / "shoot_hangs-2.zip")] and failed.closed
    assert kept(tmp_path) == ["shoot_hangs-2.zip"]


def test_a_trace_is_named_after_the_shot_even_from_a_helper(tmp_path: Path) -> None:
    """Some shots open their contexts in a helper of their own: the trace still carries the shot's name,
    so the artifact says which shot failed."""
    browser = shot_traces.TracedBrowser(FakeBrowser(), tmp_path / "traces")
    with pytest.raises(TimeoutError):
        shoot_from_a_helper(browser)
    assert kept(tmp_path) == ["shoot_from_a_helper-1.zip"]


def test_the_browser_closing_on_a_failure_keeps_the_traces_still_open(tmp_path: Path) -> None:
    """A shot that raises before it closes its own context: ``run()`` closes the browser in a `finally`,
    and that keeps the trace. A browser closed after a clean run keeps nothing."""
    browser = shot_traces.TracedBrowser(FakeBrowser(), tmp_path / "traces")
    browser.new_context()
    with pytest.raises(TimeoutError):
        try:
            raise TimeoutError("hung before its own close")
        finally:
            browser.close()
    assert len(kept(tmp_path)) == 1

    clean = shot_traces.TracedBrowser(FakeBrowser(), tmp_path / "clean")
    clean.new_context()
    clean.close()
    assert not (tmp_path / "clean").exists()


def test_a_trace_that_cant_be_kept_never_hides_the_shots_own_error(tmp_path: Path) -> None:
    """Writing the trace fails too (a full disk, a browser already gone): the shot's own timeout still
    reaches the caller, and the context still closes."""
    fake = FakeBrowser(fail_trace=True)
    browser = shot_traces.TracedBrowser(fake, tmp_path / "traces")
    with pytest.raises(TimeoutError, match="20000ms"):
        shoot_hangs(browser)
    assert fake.contexts[0].closed


def test_everything_else_is_the_wrapped_browsers_own(tmp_path: Path) -> None:
    """The shots use the browser as Playwright's own (`browser.version`, `browser.close()`): the wrapper
    passes through what it doesn't change."""
    fake = FakeBrowser()
    browser = shot_traces.TracedBrowser(fake, tmp_path / "traces")
    assert browser.version == "fake 1.0"
    browser.close()
    assert fake.closed
