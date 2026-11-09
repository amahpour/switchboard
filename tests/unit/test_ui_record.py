"""docs/media/ui_record.py, the screen recorder for a pull request's Verification (CONTRIBUTING.md,
"Recording a change working"): the capture backend on a fake page and the parts with no browser
in them. Which frames a recording shows and for how long, how a caption breaks into lines, and
where a scenario comes from.

The script is run by hand and never under pytest, so it is loaded here as a file. Nothing in
these tests needs Playwright, ffmpeg or Pillow: the ``record`` group is not installed in CI."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "docs" / "media" / "ui_record.py"
spec = importlib.util.spec_from_file_location("ui_record", SCRIPT)
assert spec and spec.loader
rec = importlib.util.module_from_spec(spec)
sys.modules["ui_record"] = rec  # its dataclass looks the module up by name
spec.loader.exec_module(rec)

A, B, C, D = Path("a.jpg"), Path("b.jpg"), Path("c.jpg"), Path("d.jpg")


def test_loading_the_recorder_needs_none_of_its_extras() -> None:
    """The extras (and ``ui_shots``, which moves HOME) are imported when a recording starts, not
    when the file is read. Fails if one moves to the top of the file: CI has no ``record`` group,
    and this file would stop importing there."""
    assert not {"ui_shots", "PIL", "imageio_ffmpeg"} & set(sys.modules)


def test_a_recording_starts_with_the_picture_on_screen_at_its_start() -> None:
    """Video time 0 is the scenario's ``begin()``. The page was last drawn before that, so the
    first frame is held from the start, not from when it was drawn, and the sign-in before it is
    dropped. Fails if a frame's own time is used: every caption and click mark then lands late by
    however long the page had been still, which is how the first recorder's captions drifted."""
    frames = [(10.0, A), (11.0, B), (12.5, C)]  # A is the sign-in; B is on screen at the start
    held = rec.frame_list(frames, start=11.4, end=13.0)
    assert held == [(B, pytest.approx(1.1)), (C, pytest.approx(0.5))]
    assert sum(seconds for _, seconds in held) == pytest.approx(13.0 - 11.4)  # as long as the scenario


def test_a_recording_ends_at_its_end() -> None:
    """A frame drawn after the end (the screencast is stopped a moment later) is left out, and the
    last frame is held up to the end. Fails if late frames are kept: the video runs past the last
    caption's time."""
    held = rec.frame_list([(1.0, A), (2.0, B), (3.0, C), (9.0, D)], start=1.0, end=4.0)
    assert [f for f, _ in held] == [A, B, C]
    assert held[-1] == (C, pytest.approx(1.0))


def test_a_recording_with_no_frame_before_its_start_begins_at_the_first_one() -> None:
    held = rec.frame_list([(5.0, A), (6.0, B)], start=4.0, end=7.0)
    assert held == [(A, pytest.approx(1.0)), (B, pytest.approx(1.0))]


def test_no_frames_is_an_error_not_an_empty_video() -> None:
    with pytest.raises(AssertionError, match="no frames"):
        rec.frame_list([(9.0, A)], start=1.0, end=2.0)


def test_a_sample_at_the_end_adds_no_time_and_a_short_tail_keeps_its_duration() -> None:
    """Padding short samples or the final screenshot pushes captions past the pixels they name."""
    held = rec.frame_list([(1.0, A), (1.99, B), (2.0, C)], start=1.0, end=2.0)
    assert held == [(A, pytest.approx(0.99)), (B, pytest.approx(0.01))]


def test_a_caption_breaks_between_words() -> None:
    """A long caption wraps on a phone-width bar instead of running off both ends. Fails if a word
    is split, or a line is longer than fits while it still has a space to break at."""
    fits = lambda line: len(line) <= 16  # noqa: E731
    assert rec.caption_lines("Open it again: the chips give way first", fits) == [
        "Open it again:",
        "the chips give",
        "way first",
    ]
    assert rec.caption_lines("short", fits) == ["short"]
    assert rec.caption_lines("a-word-longer-than-the-bar then more", fits) == [
        "a-word-longer-than-the-bar",
        "then more",
    ]
    assert rec.caption_lines("", fits) == [""]


def test_a_scenario_is_a_name_or_a_file(tmp_path: Path) -> None:
    """A pull request's scenario is a file of its own, recorded under the file's name; the ones in
    the script go by name. Fails if a file without ``scenario()``, or something that isn't a
    ``.py`` file, is accepted and only breaks once the browser is up."""
    assert rec.load_scenario("rename-room") == ("rename-room", rec.rename_room)
    mine = tmp_path / "room_title.py"
    mine.write_text("def scenario(take, world):\n    take.step('hello')\n")
    name, fn = rec.load_scenario(str(mine))
    assert name == "room-title" and fn.__name__ == "scenario"
    empty = tmp_path / "empty.py"
    empty.write_text("x = 1\n")
    with pytest.raises(SystemExit, match="no scenario"):
        rec.load_scenario(str(empty))
    with pytest.raises(SystemExit, match="not a scenario"):
        rec.load_scenario("no-such-thing")
    with pytest.raises(SystemExit, match="not a scenario"):
        rec.load_scenario(str(tmp_path / "missing.py"))


def test_the_capture_backend_records_fullscreen_pixels_on_the_caption_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CDP can keep sending the room under native fullscreen; screenshots see its top layer.

    A fake page exposes those two different pixel sources. Capture time includes screenshot
    latency, so captions cannot drift by assuming each sample took a fixed amount of time.
    """
    import base64

    clock = [10.0]
    samples: list[tuple[float, bytes]] = []
    monkeypatch.setattr(rec.time, "time", lambda: clock[0])

    class Page:
        pixels = b"room"
        context: Page

        def __init__(self) -> None:
            self.context = self

        def new_cdp_session(self, page):
            return CDP()

        def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

        def screenshot(self, *, path, type, quality, scale):
            assert (type, quality, scale) == ("jpeg", 92, "css")
            clock[0] += 0.03  # taking a screenshot costs time too
            Path(path).write_bytes(self.pixels)
            samples.append((clock[0], self.pixels))

    class CDP:
        def on(self, event, callback):
            self.callback = callback

        def send(self, method, params=None):
            if method == "Page.startScreencast":
                self.callback(
                    {"data": base64.b64encode(b"room"), "metadata": {"timestamp": clock[0]}, "sessionId": 1}
                )

    page = Page()
    take = rec.Take(page, tmp_path)
    take.roll()
    take.start = take.now()
    take.steps.append((take.now(), "Play frames in full screen"))
    page.pixels = b"native fullscreen with storyboard caption"
    take.pause(0.36)
    end = take.now()
    take.cut()
    assert take.frames[-1][1].read_bytes() == page.pixels
    assert len(samples) >= 3
    assert [t for t, _ in take.frames] == [t for t, _ in samples]
    assert all(
        path.read_bytes() == pixels for (_, path), (_, pixels) in zip(take.frames, samples, strict=True)
    )
    assert sum(seconds for _, seconds in rec.frame_list(take.frames, take.start, end)) == pytest.approx(
        end - take.start
    )


def test_typing_is_sampled_before_the_whole_value_has_been_entered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching to screenshots must still show text as it is typed, including Unicode."""
    clock, typed, pictures = [10.0], [""], []
    monkeypatch.setattr(rec.time, "time", lambda: clock[0])

    class Input:
        def fill(self, text):
            typed[0] = text

        def press_sequentially(self, text, *, delay):
            typed[0] += text
            clock[0] += len(text) * delay / 1000

    class Page:
        def locator(self, selector):
            return Input()

        def screenshot(self, *, path, **kwargs):
            Path(path).write_text(typed[0])
            pictures.append(typed[0])

    take = rec.Take(Page(), tmp_path)
    take.type("#input", "Hello café 👋", clear=True)
    assert typed[0] == "Hello café 👋"
    assert len(pictures) > 1
    assert pictures[0] and pictures[0] != typed[0]
    assert pictures[-1] == typed[0]
