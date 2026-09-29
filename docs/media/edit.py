"""Cut the README's demo video and GIF from a record.py run.

Every frame comes from the recordings: the web UI and the two agents' terminals, as they
played. The editing is choosing moments, framing them (crops and slow camera moves),
speeding up waits (with a badge saying how much), cutting between the three screens,
and adding short captions, music and an end card. Nothing on screen is redrawn.

    uv run --with pillow --with numpy python docs/media/edit.py --build /tmp/sb-rec --music track.mp3 --voice am_michael

Writes <build>/out/switchboard.mp4 (1920x1080) and <build>/out/switchboard.gif.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1920, 1080, 30
GIF_W, GIF_FPS = 960, 12
FONTS = "/usr/share/fonts/truetype/"
SANS = FONTS + "noto/NotoSans-Regular.ttf"
SANS_B = FONTS + "noto/NotoSans-Bold.ttf"
MONO = FONTS + "noto/NotoSansMono-Regular.ttf"
BG = (17, 19, 22)
INK = (240, 242, 245)
INK_2 = (160, 166, 173)
CLAUDE = (232, 135, 95)
CODEX = (106, 168, 255)
YOU = (126, 206, 142)
NAMES = {"Claude Code": CLAUDE, "Claude": CLAUDE, "Codex": CODEX, "You": YOU}


@lru_cache(maxsize=None)
def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def ease(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def lerp(a: tuple[float, ...], b: tuple[float, ...], u: float) -> tuple[float, ...]:
    return tuple(p + (q - p) * u for p, q in zip(a, b))


# ------------------------------------------------------------------ sources
class Source:
    """One recording. Frames are read in order, by wall-clock time (the recordings keep it)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        info = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=start_time", "-of", "json", str(path)],
            capture_output=True, text=True, check=True).stdout)
        self.w = info["streams"][0]["width"]
        self.h = info["streams"][0]["height"]
        self.start = float(info["format"]["start_time"])
        self._proc: subprocess.Popen[bytes] | None = None
        self._t = 0.0          # the wall-clock time of the frame in self._frame
        self._frame: Image.Image | None = None

    def _open(self, t: float) -> None:
        self.close()
        off = max(0.0, t - self.start)
        self._proc = subprocess.Popen(
            ["ffmpeg", "-v", "quiet", "-ss", f"{off:.3f}", "-i", str(self.path), "-f", "rawvideo",
             "-pix_fmt", "rgb24", "-r", str(FPS), "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._t = self.start + off - 1 / FPS
        self._frame = None

    def at(self, t: float) -> Image.Image:
        """The frame showing at wall-clock time t (reading forward; a jump back reopens)."""
        if self._proc is None or t < self._t - 0.5 / FPS or t > self._t + 5:
            self._open(t)
        assert self._proc is not None and self._proc.stdout is not None
        n = self.w * self.h * 3
        while self._frame is None or self._t + 1 / FPS <= t + 1e-6:
            data = self._proc.stdout.read(n)
            if len(data) < n:
                break
            self._frame = Image.frombuffer("RGB", (self.w, self.h), data)
            self._t += 1 / FPS
        assert self._frame is not None, f"no frame at {t} in {self.path}"
        return self._frame

    def close(self) -> None:
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait()
            self._proc = None


@dataclass
class Rec:
    build: Path
    ui: Source
    claude: Source
    codex: Source
    marks: list[dict] = field(default_factory=list)
    views: dict = field(default_factory=dict)
    terms: dict[str, list[tuple[float, str]]] = field(default_factory=dict)

    @classmethod
    def load(cls, build: Path) -> "Rec":
        r = cls(build, Source(build / "ui.mkv"), Source(build / "claude.mkv"), Source(build / "codex.mkv"))
        for line in (build / "timeline.jsonl").read_text().splitlines():
            m = json.loads(line)
            if m["kind"] == "mark":
                r.marks.append(m)
            elif m["kind"] == "term":
                r.terms.setdefault(m["pane"], []).append((m["t"], m["text"]))
        r.views = json.loads((build / "views.json").read_text())
        return r

    def mark(self, what: str, **match: str) -> dict:
        return next(m for m in self.marks if m["what"] == what and all(m.get(k) == v for k, v in match.items()))

    def messages(self) -> list[dict]:
        return [m for m in self.marks if m["what"] == "message"]

    def last_row(self, pane: str, t: float) -> int:
        """The last non-empty row of a terminal at time t."""
        text = ""
        for tt, tx in self.terms.get(pane, []):
            if tt > t:
                break
            text = tx
        rows = text.split("\n")
        return max((i for i, ln in enumerate(rows) if ln.strip()), default=len(rows) - 1)

    def work(self, pane: str | None = None) -> list[dict]:
        return [m for m in self.marks if m["what"] == "work" and (pane is None or m["pane"] == pane)]


# ------------------------------------------------------------------ drawing
def frame_of(src: Image.Image, crop: tuple[float, ...], size: tuple[int, int] = (W, H)) -> Image.Image:
    """`crop` of a source frame (its pixels), scaled to fill `size`; a crop that runs past the
    frame's edge is moved back inside it (and shrunk, keeping its aspect, if it's bigger)."""
    x0, y0, x1, y1 = crop
    k = min(1.0, src.width / (x1 - x0), src.height / (y1 - y0))
    cx, cy, w, h = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) * k, (y1 - y0) * k
    x = min(max(cx - w / 2, 0), src.width - w)
    y = min(max(cy - h / 2, 0), src.height - h)
    return src.resize(size, Image.LANCZOS, box=(x, y, x + w, y + h))


def fit_crop(box: tuple[float, ...], aspect: float, bounds: tuple[float, float]) -> tuple[float, float, float, float]:
    """The smallest crop with this aspect that holds `box`, moved inside the source's bounds."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    bw, bh = bounds
    w, h = min(w, bw), min(h, bh)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    x = min(max(cx - w / 2, 0), bw - w)
    y = min(max(cy - h / 2, 0), bh - h)
    return x, y, x + w, y + h


def draw_caption(img: Image.Image, text: str, alpha: float = 1.0, size: int = 50, top: bool = False) -> None:
    """A caption in a dark pill at the bottom (or top); the agents' names (and You) in their colours."""
    if not text or alpha <= 0:
        return
    import re
    f = font(SANS_B, size)
    d = ImageDraw.Draw(img, "RGBA")
    tw = d.textlength(text, font=f)
    k = size / 50
    padx, pady = 34 * k, 18 * k
    w, h = tw + 2 * padx, size * 1.25 + 2 * pady
    x0, y0 = (img.width - w) / 2, (56 * k if top else img.height - h - 56 * k)
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=h / 2, fill=(12, 13, 15, int(225 * alpha)))
    x, y = x0 + padx, y0 + pady - size * 0.08
    for part in re.split("(" + "|".join(NAMES) + ")", text):
        col = NAMES.get(part, INK)
        d.text((x, y), part, font=f, fill=col + (int(255 * alpha),))
        x += d.textlength(part, font=f)


def draw_badge(img: Image.Image, text: str, alpha: float = 1.0) -> None:
    """A small tag in the top-right corner, for example the speed-up."""
    if not text or alpha <= 0:
        return
    f = font(SANS_B, 26)
    d = ImageDraw.Draw(img, "RGBA")
    tw = d.textlength(text, font=f)
    x1, y0 = img.width - 40, 36
    d.rounded_rectangle((x1 - tw - 36, y0, x1, y0 + 48), radius=24, fill=(12, 13, 15, int(200 * alpha)))
    d.text((x1 - tw - 18, y0 + 8), text, font=f, fill=INK_2 + (int(255 * alpha),))


def end_card(tag: str) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f = font(SANS_B, 110)
    t = "switchboard"
    d.text(((W - d.textlength(t, font=f)) / 2, 250), t, font=f, fill=INK)
    f2 = font(SANS, 44)
    t2 = "Your coding agents, in one group chat."
    d.text(((W - d.textlength(t2, font=f2)) / 2, 400), t2, font=f2, fill=INK)
    f2b = font(SANS, 32)
    t2b = "The Claude Code, Codex, Devin and Cursor sessions you already run  ·  on your own machines  ·  self-hosted"
    d.text(((W - d.textlength(t2b, font=f2b)) / 2, 470), t2b, font=f2b, fill=INK_2)
    mono = font(MONO, 36)
    lines = [f"uv tool install git+https://github.com/amahpour/switchboard@{tag}", "switchboard install all",
             "switchboard start"]
    bw = max(d.textlength("$ " + ln, font=mono) for ln in lines) + 80
    bx, by = (W - bw) / 2, 570
    d.rounded_rectangle((bx, by, bx + bw, by + 60 * len(lines) + 50), radius=18, fill=(26, 29, 33))
    for i, ln in enumerate(lines):
        d.text((bx + 40, by + 28 + 60 * i), "$ ", font=mono, fill=YOU)
        d.text((bx + 40 + d.textlength("$ ", font=mono), by + 28 + 60 * i), ln, font=mono, fill=INK)
    f3 = font(SANS_B, 44)
    t3 = "github.com/amahpour/switchboard"
    d.text(((W - d.textlength(t3, font=f3)) / 2, 870), t3, font=f3, fill=CLAUDE)
    return img


# ------------------------------------------------------------------ shots
@dataclass
class Shot:
    """`dur` seconds of video: `draw(u)` renders the frame at u in [0, 1)."""
    dur: float
    draw: Callable[[float], Image.Image]
    caption: str = ""
    badge: str = ""
    top: bool = False          # the caption goes at the top (it would hide what's below)


def play(shots: list[Shot], fps: int, fade: float = 0.25, caption_size: int = 50):
    """The shots in order, each cross-fading into the next over `fade` s. A caption that
    changes fades out and back in rather than overlapping."""
    for k, s in enumerate(shots):
        n = max(1, round(s.dur * fps))
        nxt = shots[k + 1] if k + 1 < len(shots) else None
        nf = round(fade * fps) if nxt is not None else 0
        incoming = None
        for i in range(n):
            img = s.draw(i / n).copy()
            cap, cap_a, top = s.caption, 1.0, s.top
            if nxt is not None and i >= n - nf:
                v = ease((i - (n - nf) + 1) / (nf + 1))
                if incoming is None:
                    incoming = nxt.draw(0.0)
                img = Image.blend(img, incoming, v)
                if (nxt.caption, nxt.top) != (s.caption, s.top):
                    if v < 0.5:
                        cap_a = 1 - 2 * v
                    else:
                        cap, cap_a, top = nxt.caption, 2 * v - 1, nxt.top
            draw_caption(img, cap, cap_a, caption_size, top)
            draw_badge(img, s.badge)
            yield img


def speed_badge(speed: float) -> str:
    return f"{speed:.0f}× speed" if speed >= 1.5 else ""


# ------------------------------------------------------------------ framing
AR = W / H


def ui_scale(rec: Rec) -> float:
    return float(rec.views["ui"][2])


def tv_scale(rec: Rec) -> float:
    return float(rec.views["tv"][2])


def column(rec: Rec) -> tuple[float, float]:
    """The conversation column's left and right edges (CSS px), from the messages' boxes."""
    boxes = [c["box"] for c in rec.mark("end")["chat"]]
    return min(b[0] for b in boxes), max(b[2] for b in boxes)


def composer(rec: Rec) -> list[float]:
    return rec.mark("before-task").get("composer") or COMPOSER


def chat_camera(rec: Rec, with_composer: bool = False, pad: float = 14) -> tuple[float, ...]:
    """The column's full width, down to just above the composer (or below it): where each new
    message lands, since the log keeps its newest message at the bottom."""
    s = ui_scale(rec)
    left, right = column(rec)
    comp = composer(rec)
    bottom = (comp[3] + pad) if with_composer else (comp[1] - 4)
    w = (right - left + 2 * pad) * s
    h = w / AR
    x0 = (left - pad) * s
    y1 = bottom * s
    return x0, y1 - h, x0 + w, y1


def room_view(rec: Rec) -> tuple[float, ...]:
    """The room and its members, without the rooms sidebar: the conversation and both agents."""
    w, h, s = rec.views["ui"]
    left = (column(rec)[0] - 24) * s
    cw = w * s - left
    ch = cw / AR
    return left, h * s - ch, w * s, h * s


def full_ui(rec: Rec) -> tuple[float, ...]:
    w, h, s = rec.views["ui"]
    return 0.0, 0.0, w * s, h * s


def term_crop(rec: Rec, pane: str, r0: float, r1: float, zoom: float = 1.25) -> tuple[float, ...]:
    """A 16:9 crop of a terminal recording, left-aligned, around rows r0..r1."""
    cell = rec.views["cells"][pane]
    s = tv_scale(rec)
    fw, fh = rec.views["tv"][0] * s, rec.views["tv"][1] * s
    w = fw / zoom
    h = w / AR
    y0 = (cell["y"] + r0 * cell["h"]) * s
    y1 = (cell["y"] + r1 * cell["h"]) * s
    cy = (y0 + y1) / 2
    y = min(max(cy - h / 2, 0), fh - h)
    return 0.0, y, w, y + h


def ui_shot(rec: Rec, t0: float, t1: float, dur: float, crop0: tuple[float, ...],
            crop1: tuple[float, ...] | None = None, caption: str = "") -> Shot:
    """The UI from wall-clock t0 to t1, over dur seconds, the camera moving from crop0 to crop1."""
    def draw(u: float) -> Image.Image:
        crop = lerp(crop0, crop1 or crop0, ease(u))
        return frame_of(rec.ui.at(t0 + (t1 - t0) * u), crop)
    return Shot(dur, draw, caption, speed_badge((t1 - t0) / dur))


def term_shot(rec: Rec, pane: str, t0: float, t1: float, dur: float, crop0: tuple[float, ...],
              crop1: tuple[float, ...] | None = None, caption: str = "") -> Shot:
    src = rec.claude if pane == "claude" else rec.codex

    def draw(u: float) -> Image.Image:
        crop = lerp(crop0, crop1 or crop0, ease(u))
        return frame_of(src.at(t0 + (t1 - t0) * u), crop)
    return Shot(dur, draw, caption, speed_badge((t1 - t0) / dur))


def split_shot(rec: Rec, t0: float, t1: float, dur: float, caption: str = "", cols_frac: float = 0.66) -> Shot:
    """Both terminals side by side, at the same moment: the left `cols_frac` of each, full height,
    as large as two panes allow. Two processes on two screens, reacting to the same room."""
    srcs = (rec.claude, rec.codex)
    fw, fh = srcs[0].w, srcs[0].h
    cw = fw * cols_frac
    pane_w = W // 2 - 12
    pane_h = round(pane_w * fh / cw)
    y = (H - pane_h) // 2 - 20

    def draw(u: float) -> Image.Image:
        img = Image.new("RGB", (W, H), BG)
        tt = t0 + (t1 - t0) * u
        for i, src in enumerate(srcs):
            part = src.at(tt).resize((pane_w, pane_h), Image.LANCZOS, box=(0, 0, cw, fh))
            img.paste(part, (8 if i == 0 else W // 2 + 4, y))
        return img
    return Shot(dur, draw, caption, speed_badge((t1 - t0) / dur))


def zoom_in(crop: tuple[float, ...], k: float) -> tuple[float, ...]:
    """The same crop, k times tighter, about its centre (a slow push-in)."""
    x0, y0, x1, y1 = crop
    cx, cy, w, h = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / k, (y1 - y0) / k
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


# ------------------------------------------------------------------ the story
AGENTS = {"claude-1": "Claude", "codex-1": "Codex"}
PANE = {"claude-1": "claude", "codex-1": "codex"}
COMPOSER = [268, 810, 1292, 885]      # CSS px, when the recording has no composer box


def body(chat_row: dict) -> str:
    """A message's own text, without the quoted message it replies to."""
    import re
    return re.split(r"(?:Codex|Claude Code)\d\d:\d\d", chat_row["text"])[-1].strip()


def work_caption(m: dict) -> str:
    who = "Claude" if m["pane"] == "claude" else "Codex"
    line = m["line"]
    if "Write(" in line:
        return f"{who} writes the code"
    if any(k in line for k in ("Update(", "Edit(", "Edited", "Updated")):
        return f"{who} changes the code"
    return f"{who} runs it"


def beats(rec: Rec) -> list[tuple[float, str, dict]]:
    """What happened after the task, in order: ("work", mark) in a terminal and ("say", mark)
    in the room. Work in the same terminal within 3 s of the last is one beat."""
    posted = rec.mark("posted")["t"]
    out: list[tuple[float, str, dict]] = []
    for m in rec.work():
        if m["t"] > posted and not any(k == "work" and x["pane"] == m["pane"] and m["t"] - t < 3 for t, k, x in out):
            out.append((m["t"], "work", m))
    for m in rec.messages():
        if m["who"] in AGENTS and m["t"] > posted:
            out.append((m["t"], "say", m))
    return sorted(out, key=lambda b: b[0])


def first_mark(rec: Rec, what: str) -> dict | None:
    return next((m for m in rec.marks if m["what"] == what), None)


def mp4_shots(rec: Rec, tag: str, caps: dict[str, str]) -> list[Shot]:
    """The room carries the story. The typing plays at its real speed (cut short, never sped
    up), each agent gets one short cut to its terminal the first time it works, every message
    lands in the room, and the human's closing line ends it."""
    end = rec.mark("end")
    typing, posted = rec.mark("typing")["t"], rec.mark("posted")["t"]
    closing, closed = first_mark(rec, "closing"), first_mark(rec, "closed")
    cam = chat_camera(rec, with_composer=True)
    shots: list[Shot] = []

    # 1. cold open: the whole app at the end of the exchange, still (long enough for the music
    #    to come in before the first spoken line)
    shots.append(ui_shot(rec, end["t"], end["t"], 3.2, full_ui(rec), None,
                         caps.get("open", "Two agents review a PR. You referee.")))
    # 2. the task typed in at its real pace, in the whole app: the first seconds, then the
    #    posted message up close (a cut, not a speed-up: the words have to be readable)
    task_cap = caps.get("task", "You give the room one job")
    t2 = ui_shot(rec, typing - 1.0, typing + 3.4, 4.4, full_ui(rec), None, task_cap)
    t2.top = True
    shots.append(t2)
    t3 = ui_shot(rec, posted + 0.6, posted + 3.4, 2.8, cam, zoom_in(cam, 1.02), task_cap)
    t3.top = True
    shots.append(t3)
    # both terminals at once, as that one message reaches them: two processes, one human
    shots.append(split_shot(rec, posted + 1.2, posted + 4.4, 3.2,
                            caps.get("both", "Two separate sessions. One message.")))
    # 3. the exchange: every message, as it lands in the room
    n_say = 0
    for m in rec.messages():
        if m["t"] <= posted or m["who"] not in AGENTS or (closing and m["t"] >= closing["t"]):
            continue
        shots.append(ui_shot(rec, m["t"] - 0.8, m["t"] + 1.8, 2.6, cam, zoom_in(cam, 1.03),
                             caps.get(f"say-{n_say}", f"{AGENTS[m['who']]} replies")))
        n_say += 1
    # 4. the human calls it, at typing pace, and the agents' reactions
    if closing and closed:
        c = ui_shot(rec, closing["t"] - 0.2, closed["t"] + 1.6, closed["t"] - closing["t"] + 1.8, cam, cam,
                    caps.get("closing", "You make the call"))
        c.top = True
        shots.append(c)
        for m in rec.messages():
            if m["who"] in AGENTS and m["t"] > closed["t"]:
                shots.append(ui_shot(rec, m["t"] - 0.6, m["t"] + 1.6, 2.2, cam, cam, caps.get("closing", "You make the call")))
    # 5. the whole room, then the card
    shots.append(ui_shot(rec, end["t"], end["t"], 3.0, room_view(rec), zoom_in(room_view(rec), 1.03),
                         caps.get("final", "You asked once. They worked it out.")))
    card = end_card(tag)
    shots.append(Shot(3.6, lambda u: card))
    return shots


# ------------------------------------------------------------------ the GIF
def gif_shots(rec: Rec, caps: dict[str, str]) -> list[Shot]:
    """The room alone, for the top of the README: it opens and ends on the finished exchange
    (so its first frame tells the story), and in between each message arrives, as it did."""
    end = rec.mark("end")
    posted = rec.mark("posted")["t"]
    cam = chat_camera(rec, with_composer=True)
    size = (GIF_W, round(GIF_W / AR))
    opener = caps.get("open", "Claude Code and Codex, in one chat")

    def still(t: float, crop: tuple[float, ...]) -> Callable[[float], Image.Image]:
        return lambda u: frame_of(rec.ui.at(t), crop, size)

    shots = [Shot(1.8, still(end["t"], cam), opener)]
    shots.append(Shot(2.0, still(posted + 1.0, cam), caps.get("task", "You give the room one job")))
    closing = first_mark(rec, "closing")
    n = 0
    for m in rec.messages():
        if m["t"] <= posted or (closing and m["t"] >= closing["t"]):
            continue
        t0 = m["t"] - 0.6
        cap = caps.get(f"say-{n}", f"{AGENTS[m['who']]} replies") if m["who"] in AGENTS else caps.get("closing", "")

        def draw(u: float, t0: float = t0) -> Image.Image:
            return frame_of(rec.ui.at(t0 + 2.2 * u), cam, size)
        shots.append(Shot(2.2, draw, cap))
        if m["who"] in AGENTS:
            n += 1
    shots.append(Shot(2.2, still(end["t"], cam), opener))
    return shots


def encode_gif(frames, out: Path) -> None:
    ff = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{GIF_W}x{round(GIF_W / AR)}",
         "-r", str(GIF_FPS), "-i", "-", "-vf",
         "split[a][b];[a]palettegen=max_colors=128:stats_mode=full[p];[b][p]paletteuse=dither=none:diff_mode=rectangle",
         "-loop", "0", str(out)], stdin=subprocess.PIPE)
    assert ff.stdin is not None
    for img in frames:
        ff.stdin.write(img.tobytes())
    ff.stdin.close()
    if ff.wait() != 0:
        sys.exit("ffmpeg failed (gif)")


# ------------------------------------------------------------------ narration
KOKORO_PY = Path.home() / "kokoro-venv" / "bin" / "python"
LEAD_IN = 4.4          # s of music alone before the first spoken line: where the guitar pauses
MUSIC_FADE_IN = 0.8    # s
GAP = 0.4              # s between one spoken line's end and the next line's start, at least
KOKORO_SCRIPT = """
import json, sys
import numpy as np, soundfile as sf
from kokoro import KPipeline
spec = json.load(open(sys.argv[1]))
voice = spec["voice"]
p = KPipeline(lang_code="b" if voice[:2] in ("bm", "bf") else "a", repo_id="hexgrad/Kokoro-82M")
for item in spec["lines"]:
    chunks = [a for _, _, a in p(item["text"], voice=voice, speed=spec["speed"])]
    sf.write(item["wav"], np.concatenate(chunks), 24000)
"""


def narration(shots: list[Shot], caps: dict, voice: str, out_dir: Path, speed: float = 1.0) -> Path | None:
    """The spoken track, synthesized locally (Kokoro, in its own venv). `caps["voice"]` maps a
    caption key to what is said from that beat on: a string, or {"text": ..., "through": key}
    for a passage that runs over several beats ("through": "end" runs to the last shot). Without
    a "voice" table every caption is read at its beat. Beats stretch when a passage needs the
    room, so speech never runs into the next passage. Returns lines.json, or None with no voice."""
    if not voice:
        return None
    if not KOKORO_PY.exists():
        sys.exit(f"no Kokoro venv at {KOKORO_PY} (set it up, or drop --voice)")
    first_shot: dict[str, int] = {}
    last_shot: dict[str, int] = {}
    for i, s in enumerate(shots):
        for key, text in caps.items():
            if text == s.caption:
                first_shot.setdefault(key, i)
                last_shot[key] = i
    table = caps.get("voice")
    lines: list[dict] = []
    if table:
        for key, spec in table.items():
            if key not in first_shot:
                continue
            text, through = (spec, key) if isinstance(spec, str) else (spec["text"], spec.get("through", key))
            end = len(shots) - 1 if through == "end" else last_shot.get(through, first_shot[key])
            lines.append({"text": text, "shot": first_shot[key], "end": end})
    else:
        for key, i in first_shot.items():
            lines.append({"text": caps[key], "shot": i, "end": i})
    lines.sort(key=lambda ln: ln["shot"])
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, ln in enumerate(lines):
        ln["wav"] = str(out_dir / f"{i:02d}.wav")
    spec_path = out_dir / "lines.json"
    spec_path.write_text(json.dumps({"voice": voice, "speed": speed, "lines": lines}, indent=1))
    script = out_dir / "kokoro_lines.py"
    script.write_text(KOKORO_SCRIPT)
    r = subprocess.run([str(KOKORO_PY), str(script), str(spec_path)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("Kokoro failed:\n" + r.stderr[-2000:])
    # the beats a passage spans last at least as long as it: the shots stretch evenly (their
    # footage plays a little slower, which on a chat is invisible). The first passage waits
    # for the music to come in.
    for i, ln in enumerate(lines):
        ln["dur"] = round(len(decode(Path(ln["wav"]))) / SR, 3)
        lead = LEAD_IN if i == 0 else 0.25
        span = shots[ln["shot"]:ln["end"] + 1]
        short = lead + ln["dur"] + GAP - sum(s.dur for s in span)
        if short > 0:
            for s in span:
                s.dur += short / len(span)
    starts = [0.0]
    for s in shots:
        starts.append(starts[-1] + s.dur)
    for i, ln in enumerate(lines):
        ln["beat"] = round(starts[ln["shot"]], 3)
        ln["at"] = round(ln["beat"] + (LEAD_IN if i == 0 else 0.25), 3)
    spec_path.write_text(json.dumps({"voice": voice, "speed": speed, "lines": lines}, indent=1))
    return spec_path


# ------------------------------------------------------------------ audio
SR = 48000


def decode(path: Path, start: float = 0.0, dur: float | None = None):
    """A file's audio as float32 stereo at SR, via ffmpeg."""
    import numpy as np
    args = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}"] + (["-t", f"{dur:.3f}"] if dur else []) + \
        ["-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"]
    raw = subprocess.run(args, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)


def write_wav(path: Path, pcm) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ac", "2", "-ar", str(SR), "-i", "-",
                    "-c:a", "pcm_s16le", str(path)], input=pcm.astype("float32").tobytes(), check=True)


def lufs(path: Path) -> float:
    """Integrated loudness (EBU R128, so silence between lines doesn't count)."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    import re
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    return float(m[-1]) if m else -23.0


def gain_to(path: Path, target: float) -> float:
    return 10 ** ((target - lufs(path)) / 20)


def soundtrack(total: float, out_dir: Path, music: Path | None, start: float, lines_spec: Path | None,
               voice_lufs: float = -16.0, bed_lufs: float = -23.0, duck_db: float = 0.0):
    """The stems and the mix, all `total` s long: `narration.wav` (the lines at their times, one
    static gain to voice_lufs), `music.wav` (the track from `start` at a steady bed_lufs, under the
    voice, fading out at the end; duck_db > 0 dips it under each line) and `mix.wav` (their sum)."""
    import numpy as np
    n = int(total * SR)
    stems: dict[str, np.ndarray] = {}
    lines = json.loads(lines_spec.read_text())["lines"] if lines_spec else []
    if lines:
        voice = np.zeros((n, 2), dtype=np.float32)
        for ln in lines:
            pcm = decode(Path(ln["wav"]))
            i = int(ln["at"] * SR)
            pcm = pcm[: max(0, n - i)]
            voice[i:i + len(pcm)] += pcm
        raw = out_dir / "narration-raw.wav"
        write_wav(raw, voice)
        voice *= gain_to(raw, voice_lufs)
        raw.unlink()
        stems["narration"] = voice
    if music is not None:
        bed = decode(music, start, total)
        if len(bed) < n:
            bed = np.concatenate([bed, np.zeros((n - len(bed), 2), dtype=np.float32)])
        bed = bed[:n] * gain_to(music, bed_lufs)   # the whole track's loudness: close enough for a bed
        env = np.ones(n, dtype=np.float32)
        ramp = int(0.5 * SR)
        low = 10 ** (-duck_db / 20)
        for ln in lines if duck_db > 0 else []:
            a, b = int((ln["at"] - 0.3) * SR), int((ln["at"] + ln["dur"] + 0.4) * SR)
            a, b = max(a, 0), min(b, n)
            env[a:b] = np.minimum(env[a:b], low)
            ra, rb = max(a - ramp, 0), min(b + ramp, n)
            env[ra:a] = np.minimum(env[ra:a], np.linspace(1.0, low, a - ra, dtype=np.float32))
            env[b:rb] = np.minimum(env[b:rb], np.linspace(low, 1.0, rb - b, dtype=np.float32))
        fade_in, fade_out = int(MUSIC_FADE_IN * SR), int(2.5 * SR)
        env[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
        env[n - fade_out:] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)
        stems["music"] = bed * env[:, None]
    if not stems:
        return None
    mix = sum(stems.values())
    peak = float(np.abs(mix).max())
    if peak > 0.95:
        mix = mix * (0.95 / peak)
    for name, pcm in stems.items():
        write_wav(out_dir / f"{name}.wav", pcm)
    write_wav(out_dir / "mix.wav", mix)
    return out_dir


# ------------------------------------------------------------------ output
def encode_mp4(frames, out: Path, music: Path | None, start: float, lines_spec: Path | None = None,
               stems: bool = False) -> float:
    ff = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p", "-an",
         str(out.with_suffix(".silent.mp4"))], stdin=subprocess.PIPE)
    assert ff.stdin is not None
    n = 0
    for img in frames:
        ff.stdin.write(img.tobytes())
        n += 1
    ff.stdin.close()
    if ff.wait() != 0:
        sys.exit("ffmpeg failed (mp4)")
    total = n / FPS
    silent = out.with_suffix(".silent.mp4")
    audio = soundtrack(total, out.parent, music, start, lines_spec)
    if audio is None:
        silent.replace(out)
        return total
    # the mix is the audio track; with `stems`, the narration and the music ride along as extra
    # tracks too (the WAVs are beside the file either way). GitHub takes at most 10 MB.
    inputs = ["-i", str(silent), "-i", str(audio / "mix.wav")]
    maps = ["-map", "0:v", "-map", "1:a"]
    meta = ["-metadata:s:a:0", "title=mix", "-metadata:s:a:0", "handler_name=mix"]
    k = 2
    for name in ("narration", "music") if stems else ():
        if (audio / f"{name}.wav").exists():
            inputs += ["-i", str(audio / f"{name}.wav")]
            maps += ["-map", f"{k}:a"]
            meta += [f"-metadata:s:a:{k - 1}", f"title={name}", f"-metadata:s:a:{k - 1}", f"handler_name={name}"]
            k += 1
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, *maps, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        *meta, "-disposition:a:0", "default", "-shortest", "-movflags", "+faststart", str(out)])
    if r.returncode != 0:
        sys.exit("ffmpeg failed (audio)")
    silent.unlink()
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--build", type=Path, required=True)
    ap.add_argument("--music", type=Path, help="a music track for the MP4 (see CREDITS.md)")
    ap.add_argument("--music-start", type=float, default=0.0)
    ap.add_argument("--tag", default="", help="the release tag the end card installs (default: this version)")
    ap.add_argument("--name", default="switchboard")
    ap.add_argument("--no-mp4", action="store_true")
    ap.add_argument("--no-gif", action="store_true")
    ap.add_argument("--voice", default="", help="narrate the captions with this Kokoro voice (e.g. am_michael)")
    ap.add_argument("--voice-speed", type=float, default=1.0)
    ap.add_argument("--audio-only", action="store_true", help="only the soundtrack (out/mix.wav), no video render")
    ap.add_argument("--stems", action="store_true", help="add the narration and the music as extra audio tracks")
    args = ap.parse_args()
    rec = Rec.load(args.build)
    caps = json.loads((args.build / "captions.json").read_text()) if (args.build / "captions.json").exists() else {}
    tag = args.tag
    if not tag:
        from switchboard import __version__
        tag = f"v{__version__}"
    out = args.build / "out"
    out.mkdir(exist_ok=True)
    if args.audio_only:
        shots = mp4_shots(rec, tag, caps)
        spec = narration(shots, caps, args.voice, out / f"{args.name}-voice", args.voice_speed)
        soundtrack(sum(s.dur for s in shots), out, args.music, args.music_start, spec)
        print(f"{out / 'mix.wav'} (and narration.wav, music.wav)")
        return
    if not args.no_mp4:
        shots = mp4_shots(rec, tag, caps)
        spec = narration(shots, caps, args.voice, out / f"{args.name}-voice", args.voice_speed)
        total = encode_mp4(play(shots, FPS), out / f"{args.name}.mp4", args.music, args.music_start, spec, args.stems)
        print(f"{out / (args.name + '.mp4')}: {total:.1f} s")
    if not args.no_gif:
        encode_gif(play(gif_shots(rec, caps), GIF_FPS, caption_size=30), out / f"{args.name}.gif")
        print(f"{out / (args.name + '.gif')}: {(out / (args.name + '.gif')).stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
