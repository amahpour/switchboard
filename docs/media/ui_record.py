"""Screen recordings of a change working in the web UI, for a pull request's Verification.

    uv run --group record python docs/media/ui_record.py --list
    uv run --group record python docs/media/ui_record.py rename-room --out /tmp/recordings

Writes ``<out>/<name>.mp4`` and ``<out>/<name>.gif`` (a GIF plays inline in a pull request).
Run by hand, never by pytest or CI. It needs Playwright's Chromium (``uv run playwright install
chromium``) and the ``record`` dependency group, which is not part of the regular setup: it adds
``imageio-ffmpeg`` (its own ffmpeg binary) and ``pillow`` (the caption bar and click marks).

What it does, as ``ui_shots.py`` does for still pictures:

- **A throwaway home** and **seeded state** (``tests/ui_world.py``): nothing here reads or
  writes ``~/.switchboard``, and the names in the picture are alice and the stand-in agents.
- **The real page in headless Chromium,** while a scenario clicks and types through the change.
  A scenario is a function below: a few ``take.step("caption")`` lines with the clicks between
  them. Every wait has a deadline; a timeout exits non-zero.
- **The browser's own screencast frames,** each with the time it was drawn. Playwright's video
  recorder says nothing about when its first frame was taken, so captions drifted seconds off
  the picture; frames with their own clock line up exactly, and they are sharper.
- **Then ffmpeg:** the sign-in is cut off the front, each step's caption is shown in a bar under
  the page (never over it), and each click gets a ring where it landed, since a headless page
  has no mouse pointer to see.

A scenario shows one change working, in well under a minute. It is evidence for one pull
request, not a test: the e2e tests are what keeps the behavior.
"""

from __future__ import annotations

import argparse
import base64
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ui_shots as shots  # noqa: E402  (isolation, sign-in and the waits; nothing is shot)

SIZE = {"width": 1280, "height": 800}  # the page, and the video, in CSS pixels
BAR = 56  # the caption bar under the page
FPS = 25
GIF_WIDTH, GIF_FPS = 960, 10
RING, RING_S = 44, 0.7  # a click mark's box and how long it shows
BEFORE_CLICK_S, AFTER_STEP_S, KEY_DELAY_MS = 0.5, 0.9, 55


@dataclass
class Take:
    """One recording: the page, its frames, and when each caption and click happened. Every time
    is the browser's own clock (seconds since the epoch), the one the frames carry."""

    page: Any
    frames_dir: Path
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
            {"format": "jpeg", "quality": 92, "maxWidth": SIZE["width"], "maxHeight": SIZE["height"]},
        )

    def cut(self) -> None:
        self.pause(0.3)  # the last frames arrive
        self.cdp.send("Page.stopScreencast")

    def begin(self) -> None:
        """Everything before this (the sign-in, the first load) is cut off the front."""
        shots.settle(self.page)
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
    page.locator('#tabs .room[data-room="' + old + '"]').click()
    expect(page.locator("#room-title")).to_have_text(old[1:])
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


def room_title(take: Take, world: Any) -> None:
    """The room's name stays whole in the header while the member count and the chips give way."""
    from playwright.sync_api import expect

    page, room = take.page, "#sprint-grooming"
    world.create_room(room)
    world.add_agents(room, ("claude-1", "codex-1"))
    page.locator('#tabs .room[data-room="' + room + '"]').click()
    expect(page.locator("#room-title")).to_have_text(room[1:])
    take.begin()
    take.step("1280 px wide, Members open: the room's name is whole")
    take.pause(2.0)
    take.step("Close Members: the member count and the chips get their room back")
    take.click("#pane-toggle")
    take.pause(1.6)
    take.step("Open it again: they give way, and the name stays")
    take.click("#pane-toggle")
    take.pause(1.6)
    take.step("The name is the button that renames the room")
    take.click("#room-title-button")
    expect(page.locator("#app-dialog-name")).to_be_focused()
    take.pause(1.6)
    take.press("Escape")


SCENARIOS: dict[str, Callable[[Take, Any], None]] = {"rename-room": rename_room, "room-title": room_title}


# ------------------------------------------------------------------ recording
def record(name: str, out: Path, world: Any, browser: Any, scheme: str) -> Path:
    """Run one scenario with the screencast rolling, then write the MP4 and the GIF."""
    raw = Path(tempfile.mkdtemp(prefix="sb-rec-"))
    ctx = browser.new_context(timezone_id="UTC", locale="en-US", color_scheme=scheme, viewport=SIZE)
    ctx.set_default_timeout(shots.WAIT_MS)
    page = ctx.new_page()
    take = Take(page, raw)
    try:
        shots.sign_in(page, world.broker)
        take.roll()
        SCENARIOS[name](take, world)
        take.pause(AFTER_STEP_S)
        end = take.now()
        take.cut()
        out.mkdir(parents=True, exist_ok=True)
        finish(out / name, take, end)
    finally:
        ctx.close()
        shutil.rmtree(raw, ignore_errors=True)
    return out / (name + ".mp4")


def caption_png(text: str, path: Path) -> None:
    """One caption, white on the bar's dark grey, centred."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (SIZE["width"], BAR), (24, 24, 27))
    draw = ImageDraw.Draw(img)
    size = 22
    while True:
        font = ImageFont.load_default(size=size)
        width = draw.textlength(text, font=font)
        if width <= SIZE["width"] - 48 or size <= 14:
            break
        size -= 1
    draw.text(((SIZE["width"] - width) / 2, (BAR - size) / 2 - 2), text, font=font, fill=(244, 244, 245))
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
    big.resize((RING, RING), Image.LANCZOS).save(path)


def frame_list(take: Take, end: float, path: Path) -> None:
    """The frames from ``take.start`` to ``end`` as an ffmpeg concat list: each is held until the
    next one was drawn. The frame on screen at the start comes first, from the start."""
    frames = [(t, f) for t, f in take.frames if t <= end]
    assert frames, "the screencast sent no frames"
    before = [i for i, (t, _) in enumerate(frames) if t <= take.start]
    frames = frames[before[-1] if before else 0 :]
    lines = []
    for i, (t, f) in enumerate(frames):
        until = frames[i + 1][0] if i + 1 < len(frames) else end
        lines += [f"file '{f}'", f"duration {max(0.02, until - max(t, take.start)):.3f}"]
    lines.append(f"file '{frames[-1][1]}'")  # the concat demuxer needs the last file named twice
    path.write_text("\n".join(lines) + "\n")


def finish(stem: Path, take: Take, end: float) -> None:
    """Join the frames, add the caption bar and the click marks, and write the MP4 and the GIF."""
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    work = Path(tempfile.mkdtemp(prefix="sb-rec-art-"))
    zero = take.start  # video time 0
    try:
        listing = work / "frames.txt"
        frame_list(take, end, listing)
        inputs = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        w, h = SIZE["width"], SIZE["height"]
        chain = [f"[0:v]fps={FPS},scale={w}:{h},pad={w}:{h + BAR}:0:0:color=0x18181b[v0]"]
        last, n = "v0", 0
        steps = [(max(0.0, t - zero), text) for t, text in take.steps]
        bounds = [t for t, _ in steps[1:]] + [end - zero]
        for (t, text), until in zip(steps, bounds, strict=True):
            n += 1
            png = work / f"caption-{n}.png"
            caption_png(text, png)
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
        chain.append(f"[{last}]split[mp4][g];[g]fps={GIF_FPS},scale={GIF_WIDTH}:-1:flags=lanczos,split[g1][g2]")
        chain.append("[g1]palettegen=stats_mode=diff[pal];[g2][pal]paletteuse=dither=bayer:bayer_scale=4[gif]")
        cmd = [ffmpeg, "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(chain)]
        cmd += ["-map", "[mp4]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", "-movflags", "+faststart"]
        cmd += [str(stem) + ".mp4", "-map", "[gif]", "-loop", "0", str(stem) + ".gif"]
        subprocess.run(cmd, check=True, timeout=300)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("scenario", nargs="?", choices=sorted(SCENARIOS), help="what to record")
    ap.add_argument("--list", action="store_true", help="list the scenarios and exit")
    ap.add_argument("--out", default="recordings", help="output directory (default ./recordings)")
    ap.add_argument("--dark", action="store_true", help="record in the dark theme")
    ap.add_argument("--chrome", help="a Chrome or Chromium binary to use instead of Playwright's own")
    args = ap.parse_args()
    if args.list or not args.scenario:
        for name, fn in sorted(SCENARIOS.items()):
            print(f"{name}\t{(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0 if args.list else 2
    out = Path(args.out).resolve()
    from playwright.sync_api import sync_playwright

    # Playwright's driver starts before HOME moves, as in ui_shots.py
    pw = sync_playwright().start()
    fake_home = shots.isolate()
    try:
        from ui_world import UIWorld

        world = UIWorld()
        try:
            world.start()
            env = {**os.environ, "TZ": "UTC", "HOME": shots.REAL_HOME or os.environ["HOME"]}
            browser = pw.chromium.launch(executable_path=args.chrome or None, env=env)
            try:
                mp4 = record(args.scenario, out, world, browser, "dark" if args.dark else "light")
            finally:
                browser.close()
        finally:
            world.stop()
    finally:
        pw.stop()
        shutil.rmtree(fake_home, ignore_errors=True)
    for path in (mp4, mp4.with_suffix(".gif")):
        print(f"{path}  {path.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
