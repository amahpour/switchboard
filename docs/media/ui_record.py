"""Screen recordings of a change working in the web UI, for a pull request's Verification.

    uv run --group record python docs/media/ui_record.py --list
    uv run --group record python docs/media/ui_record.py rename-room --out /tmp/recordings
    uv run --group record python docs/media/ui_record.py path/to/my_change.py --out /tmp/recordings

Writes ``<out>/<name>.mp4``, ``<out>/<name>.gif`` (a smaller copy) and ``<out>/<name>.steps.png``
(the picture at the end of each step, side by side: what an agent, or anyone without a video
player, looks at before posting the recording). None of them is ever committed, to any branch:
a recording reaches a pull request only as a GitHub attachment. Run by hand, never by pytest or
CI. It needs Playwright's Chromium (``uv run playwright install chromium``) and the
``record`` dependency group, which is not part of the regular setup: it adds ``imageio-ffmpeg``
(its own ffmpeg binary) and ``pillow`` (the caption bar and click marks).

What it does, as ``ui_shots.py`` does for still pictures:

- **A throwaway home** and **seeded state** (``tests/ui_world.py``): nothing here reads or
  writes ``~/.switchboard``, and the names in the picture are alice and the stand-in agents.
- **The real page in headless Chromium,** while a scenario clicks and types through the change.
  A scenario is a function ``scenario(take, world)``: a few ``take.step("caption")`` lines with
  the clicks between them. ``rename_room`` below is the worked example; a pull request's own
  scenario is a file given by path, pasted into the pull request, not added to this script.
  Every wait has a deadline; a timeout exits non-zero.
- **The browser's own screencast frames,** each with the time it was drawn. Playwright's video
  recorder says nothing about when its first frame was taken, so captions drifted seconds off
  the picture; frames with their own clock line up exactly, and they are sharper.
- **Then ffmpeg:** the sign-in is cut off the front, each step's caption is shown in a bar under
  the page (never over it), and each click gets a ring where it landed, since a headless page
  has no mouse pointer to see.

A scenario shows one change working, in well under a minute, at one window size (``--phone`` for
390x844): the frames are joined at the size the recording started with, so a scenario never
resizes the window. It is evidence for one pull request, not a test: the e2e tests are what
keeps the behavior.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

DESKTOP: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
PHONE: dict[str, Any] = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True}
FPS = 25
GIF_FPS = 10
GIF_WIDTH = 960  # a desktop GIF is shrunk to this; a narrower recording keeps its own width
RING, RING_S = 44, 0.7  # a click mark's box and how long it shows
BEFORE_CLICK_S, AFTER_STEP_S, KEY_DELAY_MS = 0.5, 0.9, 55
BAR_INK, BAR_PAPER = (244, 244, 245), (24, 24, 27)

Scenario = Callable[["Take", Any], None]


def shots() -> Any:
    """``ui_shots``, for its isolation, sign-in and waits. Imported on first use, so the pure parts
    of this file (the frame list, the caption's lines) can be tested without it."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import ui_shots

    return ui_shots


@dataclass
class Take:
    """One recording: the page, its frames, and when each caption and click happened. Every time
    is seconds since the epoch on this machine's clock, which is the one the browser stamps its
    frames with: the browser and this script run on the same machine."""

    page: Any
    frames_dir: Path
    width: int = DESKTOP["viewport"]["width"]
    height: int = DESKTOP["viewport"]["height"]
    frames: list[tuple[float, Path]] = field(default_factory=list)
    steps: list[tuple[float, str]] = field(default_factory=list)
    clicks: list[tuple[float, float, float]] = field(default_factory=list)
    start: float = 0.0
    cdp: Any = None

    def now(self) -> float:
        return time.time()

    def roll(self) -> None:
        """Start the screencast: Chromium sends a frame each time the page is drawn."""
        self.cdp = self.page.context.new_cdp_session(self.page)

        def frame(ev: dict[str, Any]) -> None:
            path = self.frames_dir / f"f{len(self.frames):05d}.jpg"
            path.write_bytes(base64.b64decode(ev["data"]))
            self.frames.append((float(ev["metadata"]["timestamp"]), path))
            self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})

        self.cdp.on("Page.screencastFrame", frame)
        self.cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 92, "maxWidth": self.width, "maxHeight": self.height},
        )

    def cut(self) -> None:
        self.pause(0.3)  # the last frames arrive
        self.cdp.send("Page.stopScreencast")

    def open(self, room: str) -> None:
        """Open a room from the sidebar, which on a phone is a drawer to open first. Call it before
        ``begin()``: getting to the room is not what the recording is about."""
        from playwright.sync_api import expect

        name = room.lstrip("#")
        tab = self.page.locator('#tabs .room[data-room="#' + name + '"]')
        expect(tab).to_be_attached()
        if not tab.is_visible():
            self.page.click("#rooms-toggle")
        tab.click()
        expect(self.page.locator("#room-title")).to_have_text(name)

    def begin(self) -> None:
        """Everything before this (the sign-in, the first load) is cut off the front."""
        shots().settle(self.page)
        self.start = self.now()

    def step(self, caption: str) -> None:
        """The caption shown from now until the next step."""
        self.pause(AFTER_STEP_S if self.steps else 0.2)
        self.steps.append((self.now(), caption))

    def pause(self, seconds: float) -> None:
        self.page.wait_for_timeout(int(seconds * 1000))

    def click(self, selector: str) -> None:
        """Click with a ring where it lands, after a beat so the eye gets there first."""
        target = self.page.locator(selector)
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        assert box is not None, f"{selector} has no box"
        self.pause(BEFORE_CLICK_S)
        self.clicks.append((self.now(), box["x"] + box["width"] / 2, box["y"] + box["height"] / 2))
        target.click()

    def rest(self) -> None:
        """Move the pointer to the page's corner. After a click the layout can move a control
        with a hover tip under the pointer, and the tip then covers what the step is showing."""
        self.page.mouse.move(self.width - 2, self.height - 2)

    def type(self, selector: str, text: str, *, clear: bool = False) -> None:
        """Type as a person would, one key at a time."""
        target = self.page.locator(selector)
        if clear:
            target.fill("")
        target.press_sequentially(text, delay=KEY_DELAY_MS)

    def press(self, key: str) -> None:
        self.pause(BEFORE_CLICK_S)
        self.page.keyboard.press(key)


# ------------------------------------------------------------------ scenarios
def rename_room(take: Take, world: Any) -> None:
    """#316: a room's name is fixed from its title, and everything that names the room follows."""
    from playwright.sync_api import expect

    page, old, new = take.page, "#sprint-groming", "#sprint-grooming"
    world.create_room(old)
    world.add_agents(old, ("claude-1",))
    take.open(old)
    take.begin()
    take.step("A room with a typo in its name, and an agent already in it")
    take.pause(1.2)
    take.step("Click the room's name")
    take.click("#room-title-button")
    expect(page.locator("#app-dialog-name")).to_be_focused()
    take.step("Type the new name and press Enter")
    take.type("#app-dialog-name", new, clear=True)
    take.press("Enter")
    expect(page.locator("#room-title")).to_have_text(new[1:])
    expect(page.locator("#log")).to_contain_text("renamed " + old + " to " + new)
    take.step("The title, the sidebar, the address and the message box follow; the agent stays")
    take.pause(2.2)
    take.step("/rename does the same from the message box")
    take.click("#input")
    take.type("#input", "/rename sprint-planning")
    take.press("Enter")
    expect(page.locator("#room-title")).to_have_text("sprint-planning")
    take.pause(2.2)


SCENARIOS: dict[str, Scenario] = {"rename-room": rename_room}


def load_scenario(spec: str) -> tuple[str, Scenario]:
    """A scenario by name (the ones above) or from a file: a ``.py`` with a function
    ``scenario(take, world)``, recorded under the file's name."""
    if spec in SCENARIOS:
        return spec, SCENARIOS[spec]
    path = Path(spec)
    if path.suffix != ".py" or not path.is_file():
        raise SystemExit(f"{spec}: not a scenario ({', '.join(sorted(SCENARIOS))}) and not a .py file")
    found = importlib.util.spec_from_file_location("ui_record_scenario", path)
    assert found is not None and found.loader is not None
    module = importlib.util.module_from_spec(found)
    sys.modules[found.name] = module  # a dataclass in the file looks its module up by name
    found.loader.exec_module(module)
    fn = getattr(module, "scenario", None)
    if not callable(fn):
        raise SystemExit(f"{spec}: no scenario(take, world) function")
    return path.stem.replace("_", "-"), fn


# ------------------------------------------------------------------ recording
def record(fn: Scenario, stem: Path, world: Any, browser: Any, scheme: str, device: dict[str, Any]) -> None:
    """Run one scenario with the screencast rolling, then write the MP4, the GIF and the sheet."""
    raw = Path(tempfile.mkdtemp(prefix="sb-rec-"))
    ctx = browser.new_context(timezone_id="UTC", locale="en-US", color_scheme=scheme, **device)
    ctx.set_default_timeout(shots().WAIT_MS)
    page = ctx.new_page()
    take = Take(page, raw, device["viewport"]["width"], device["viewport"]["height"])
    try:
        shots().sign_in(page, world.broker)
        take.roll()
        fn(take, world)
        assert take.steps, "the scenario has no take.step(): a recording needs its captions"
        take.pause(AFTER_STEP_S)
        end = take.now()
        take.cut()
        stem.parent.mkdir(parents=True, exist_ok=True)
        finish(stem, take, end)
    finally:
        ctx.close()
        shutil.rmtree(raw, ignore_errors=True)


def caption_lines(text: str, fits: Callable[[str], bool]) -> list[str]:
    """The caption broken into lines that fit: whole words, as many to a line as ``fits`` allows.
    A single word wider than the bar gets a line to itself."""
    lines: list[str] = []
    for word in text.split():
        if lines and fits(lines[-1] + " " + word):
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines or [""]


def caption_font(width: int) -> tuple[Any, int]:
    from PIL import ImageFont

    size = 22 if width >= 900 else 15
    return ImageFont.load_default(size=size), size


def caption_wrap(text: str, width: int) -> list[str]:
    font, _ = caption_font(width)
    return caption_lines(text, lambda line: font.getlength(line) <= width - 32)


def bar_height(captions: list[str], width: int) -> int:
    """The caption bar is as tall as the longest caption needs, the same for the whole recording
    (a video has one size), and even (H.264 wants even sides)."""
    _, size = caption_font(width)
    most = max(len(caption_wrap(c, width)) for c in captions)
    tall = 2 * 14 + most * size + (most - 1) * round(size * 0.35)
    return tall + tall % 2


def caption_png(text: str, path: Path, width: int, bar: int) -> None:
    """One caption, light on the bar's dark grey, centred."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, bar), BAR_PAPER)
    draw = ImageDraw.Draw(img)
    font, size = caption_font(width)
    lines = caption_wrap(text, width)
    step = size + round(size * 0.35)
    top = (bar - (len(lines) * step - round(size * 0.35))) / 2 - 2
    for i, line in enumerate(lines):
        draw.text(
            ((width - draw.textlength(line, font=font)) / 2, top + i * step), line, font=font, fill=BAR_INK
        )
    img.save(path)


def ring_png(path: Path) -> None:
    """The click mark: a ring with a soft fill, on a transparent square."""
    from PIL import Image, ImageDraw

    scale = 4  # drawn large and shrunk, for a smooth edge
    big = Image.new("RGBA", (RING * scale, RING * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    edge = 3 * scale
    draw.ellipse(
        (edge, edge, RING * scale - edge, RING * scale - edge),
        fill=(37, 99, 235, 60),
        outline=(37, 99, 235, 235),
        width=edge,
    )
    big.resize((RING, RING), Image.Resampling.LANCZOS).save(path)


def frame_list(frames: list[tuple[float, Path]], start: float, end: float) -> list[tuple[Path, float]]:
    """The frames to show and for how long: from ``start`` to ``end``, each held until the next
    one was drawn. The frame on screen at ``start`` comes first, counted from ``start``, so video
    time 0 is ``start`` and a caption or click at browser time t is at video time t - start."""
    frames = [(t, f) for t, f in frames if t <= end]
    assert frames, "the screencast sent no frames"
    before = [i for i, (t, _) in enumerate(frames) if t <= start]
    frames = frames[before[-1] if before else 0 :]
    held = []
    for i, (t, f) in enumerate(frames):
        until = frames[i + 1][0] if i + 1 < len(frames) else end
        held.append((f, max(0.02, until - max(t, start))))
    return held


def concat_file(held: list[tuple[Path, float]], path: Path) -> None:
    lines = [line for f, seconds in held for line in (f"file '{f}'", f"duration {seconds:.3f}")]
    lines.append(f"file '{held[-1][0]}'")  # the concat demuxer needs the last file named twice
    path.write_text("\n".join(lines) + "\n")


def finish(stem: Path, take: Take, end: float) -> None:
    """Join the frames, add the caption bar and the click marks, and write the MP4, the GIF and
    the sheet of steps."""
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    work = Path(tempfile.mkdtemp(prefix="sb-rec-art-"))
    zero = take.start  # video time 0
    w, h = take.width, take.height
    bar = bar_height([text for _, text in take.steps], w)
    paper = "0x%02x%02x%02x" % BAR_PAPER
    try:
        listing = work / "frames.txt"
        concat_file(frame_list(take.frames, take.start, end), listing)
        inputs = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        chain = [f"[0:v]fps={FPS},scale={w}:{h},pad={w}:{h + bar}:0:0:color={paper}[v0]"]
        last, n = "v0", 0
        steps = [(max(0.0, t - zero), text) for t, text in take.steps]
        bounds = [t for t, _ in steps[1:]] + [end - zero]
        for (t, text), until in zip(steps, bounds, strict=True):
            n += 1
            png = work / f"caption-{n}.png"
            caption_png(text, png, w, bar)
            inputs += ["-i", str(png)]
            chain.append(f"[{last}][{n}:v]overlay=0:{h}:enable='between(t,{t:.3f},{until:.3f})'[v{n}]")
            last = f"v{n}"
        if take.clicks:
            ring = work / "ring.png"
            ring_png(ring)
            for at, x, y in take.clicks:
                t = at - zero
                n += 1
                inputs += ["-i", str(ring)]
                chain.append(
                    f"[{last}][{n}:v]overlay={x - RING / 2:.0f}:{y - RING / 2:.0f}:"
                    f"enable='between(t,{t:.3f},{t + RING_S:.3f})'[v{n}]"
                )
                last = f"v{n}"
        gif_w = min(GIF_WIDTH, w)
        chain.append(f"[{last}]split[mp4][g];[g]fps={GIF_FPS},scale={gif_w}:-1:flags=lanczos,split[g1][g2]")
        chain.append(
            "[g1]palettegen=stats_mode=diff[pal];[g2][pal]paletteuse=dither=bayer:bayer_scale=4[gif]"
        )
        cmd = [ffmpeg, "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(chain)]
        cmd += [
            "-map",
            "[mp4]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "23",
            "-movflags",
            "+faststart",
        ]
        cmd += [str(stem) + ".mp4", "-map", "[gif]", "-loop", "0", str(stem) + ".gif"]
        subprocess.run(cmd, check=True, timeout=300)
        steps_sheet(ffmpeg, Path(str(stem) + ".mp4"), [until - 0.15 for until in bounds], work, w)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def steps_sheet(ffmpeg: str, mp4: Path, times: list[float], work: Path, width: int) -> None:
    """``<name>.steps.png``: the video's picture just before each step ends, caption and all, in
    a grid. It is what to look at before posting: does each picture show what its caption says?"""
    from PIL import Image

    cell_w = 640 if width >= 900 else 260
    cols = 2 if width >= 900 else 4
    cells = []
    for i, t in enumerate(times):
        png = work / f"step-{i}.png"
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(mp4)]
        subprocess.run([*cmd, "-frames:v", "1", str(png)], check=True, timeout=60)
        im = Image.open(png).convert("RGB")
        cells.append(im.resize((cell_w, round(im.height * cell_w / im.width)), Image.Resampling.LANCZOS))
    gap, cell_h = 8, cells[0].height
    rows = -(-len(cells) // cols)
    sheet = Image.new(
        "RGB", (cols * cell_w + (cols + 1) * gap, rows * cell_h + (rows + 1) * gap), (113, 113, 122)
    )
    for i, im in enumerate(cells):
        sheet.paste(im, (gap + (i % cols) * (cell_w + gap), gap + (i // cols) * (cell_h + gap)))
    sheet.save(mp4.with_name(mp4.stem + ".steps.png"))


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument(
        "scenario", nargs="?", help="a scenario's name (--list) or a .py file with scenario(take, world)"
    )
    ap.add_argument("--list", action="store_true", help="list the built-in scenarios and exit")
    ap.add_argument("--out", default="recordings", help="output directory (default ./recordings)")
    ap.add_argument("--name", help="the output files' name (default: the scenario's)")
    ap.add_argument("--dark", action="store_true", help="record in the dark theme")
    ap.add_argument(
        "--phone", action="store_true", help="record a 390x844 phone instead of a 1280x800 window"
    )
    ap.add_argument("--chrome", help="a Chrome or Chromium binary to use instead of Playwright's own")
    args = ap.parse_args()
    if args.list or not args.scenario:
        for name, fn in sorted(SCENARIOS.items()):
            print(f"{name}\t{(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0 if args.list else 2
    name, fn = load_scenario(args.scenario)
    stem = Path(args.out).resolve() / (args.name or name)
    from playwright.sync_api import sync_playwright

    # Playwright's driver starts before HOME moves, as in ui_shots.py
    pw = sync_playwright().start()
    fake_home = shots().isolate()
    try:
        from ui_world import UIWorld

        world = UIWorld()
        try:
            world.start()
            env = {**os.environ, "TZ": "UTC", "HOME": shots().REAL_HOME or os.environ["HOME"]}
            browser = pw.chromium.launch(executable_path=args.chrome or None, env=env)
            try:
                record(
                    fn,
                    stem,
                    world,
                    browser,
                    "dark" if args.dark else "light",
                    PHONE if args.phone else DESKTOP,
                )
            finally:
                browser.close()
        finally:
            world.stop()
    finally:
        pw.stop()
        shutil.rmtree(fake_home, ignore_errors=True)
    for suffix in (".mp4", ".gif", ".steps.png"):
        path = Path(str(stem) + suffix)
        print(f"{path}  {path.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
