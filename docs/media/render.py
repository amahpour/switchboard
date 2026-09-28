"""Render the README's getting-started video and GIF from a capture.py run.

Every terminal frame is drawn from a screen that capture.py recorded (its text and
colours, cell for cell), and every browser frame is a screenshot it took, cropped and
scaled. Nothing is typed, drawn or retouched here beyond framing, captions and the
title cards; time is compressed where the capture was waiting.

    uv run --no-project --with pillow --with numpy --with fonttools python docs/media/render.py --build /tmp/sb-media

Writes <build>/out/switchboard.mp4 (with music from music.py) and <build>/out/switchboard.gif.
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
W, H, FPS = 1280, 720, 30
GIF_W, GIF_FPS = 960, 12
CAPTION_H = 64

FONTS = "/usr/share/fonts/truetype/"
MONO = FONTS + "dejavu/DejaVuSansMono.ttf"
MONO_B = FONTS + "dejavu/DejaVuSansMono-Bold.ttf"
SANS = FONTS + "dejavu/DejaVuSans.ttf"
SANS_B = FONTS + "dejavu/DejaVuSans-Bold.ttf"
FALLBACKS = (FONTS + "dejavu/DejaVuSans.ttf", FONTS + "noto/NotoSansMath-Regular.ttf")
# glyphs no installed font has, drawn as their nearest look-alike
SUBST = {"⏺": "●", "⎿": "└", "⏸": "‖", "⧉": "□"}

BG = (21, 24, 26)
SURFACE = (28, 32, 35)
TITLEBAR = (35, 40, 43)
INK = (233, 231, 225)
INK_2 = (168, 173, 169)
INK_3 = (124, 129, 126)
RULE = (47, 53, 56)
COPPER = (224, 138, 95)
TERM_FG = (214, 214, 208)
TERM_BG = (24, 27, 29)

# browser crops, in CSS pixels of the 860x540 viewport capture.py uses
WINDOW = (12, 12, 848, 528)
TOP = (12, 12, 848, 262)
CHAT = (12, 84, 600, 500)
LOG = (18, 90, 596, 212)


@lru_cache(maxsize=None)
def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


# ------------------------------------------------------------------ capture
@dataclass
class Capture:
    build: Path
    terms: dict[str, list[tuple[float, str]]] = field(default_factory=dict)
    shots: list[tuple[float, str, str]] = field(default_factory=list)
    marks: dict[str, float] = field(default_factory=dict)
    scenes: dict[str, tuple[float, float]] = field(default_factory=dict)
    dpr: int = 2
    tag: str = ""

    @classmethod
    def load(cls, build: Path) -> "Capture":
        c = cls(build)
        meta = json.loads((build / "meta.json").read_text())
        c.dpr = meta["view"][2]
        c.tag = meta["tag"]
        for line in (build / "timeline.jsonl").read_text().splitlines():
            r = json.loads(line)
            t, scene = r["t"], r["scene"]
            a, b = c.scenes.get(scene, (t, t))
            c.scenes[scene] = (min(a, t), max(b, t))
            if r["kind"] == "term":
                c.terms.setdefault(r["pane"], []).append((t, r["text"]))
            elif r["kind"] == "shot":
                c.shots.append((t, r["file"], r["label"]))
            elif r["kind"] == "mark":
                c.marks.setdefault(r["what"], t)
        return c

    def term(self, pane: str, t: float) -> str:
        frames = self.terms[pane]
        i = bisect.bisect_right([x[0] for x in frames], t) - 1
        return frames[max(i, 0)][1]

    def shot(self, t: float, labels: tuple[str, ...] | None = None) -> Path:
        cands = [s for s in self.shots if labels is None or s[2] in labels]
        best = cands[0]
        for s in cands:
            if s[0] <= t:
                best = s
        return self.build / best[1]

    def first(self, label: str) -> float:
        return next(s[0] for s in self.shots if s[2] == label)

    def last(self, label: str) -> float:
        return [s[0] for s in self.shots if s[2] == label][-1]

    def when(self, pane: str, pattern: str, after: float = 0.0) -> float:
        rx = re.compile(pattern)
        for t, text in self.terms[pane]:
            if t >= after and rx.search(strip(text)):
                return t
        raise SystemExit(f"never saw /{pattern}/ in {pane} after {after}")


def strip(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;:]*[A-Za-z]", "", s)


# ---------------------------------------------------------------- terminals
XTERM16 = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200), (188, 63, 188),
           (17, 168, 205), (229, 229, 229), (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67),
           (59, 142, 234), (214, 112, 214), (41, 184, 219), (255, 255, 255)]


def xterm256(n: int) -> tuple[int, int, int]:
    if n < 16:
        return XTERM16[n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return steps[n // 36], steps[(n // 6) % 6], steps[n % 6]
    v = 8 + 10 * (n - 232)
    return v, v, v


@dataclass
class Style:
    fg: tuple[int, int, int] | None = None
    bg: tuple[int, int, int] | None = None
    bold: bool = False
    dim: bool = False
    reverse: bool = False
    underline: bool = False


def sgr(style: Style, params: list[str]) -> Style:
    s = Style(style.fg, style.bg, style.bold, style.dim, style.reverse, style.underline)
    p = [int(x) if x.isdigit() else 0 for x in (params or ["0"])]
    i = 0
    while i < len(p):
        c = p[i]
        if c == 0:
            s = Style()
        elif c == 1:
            s.bold = True
        elif c == 2:
            s.dim = True
        elif c == 22:
            s.bold = s.dim = False
        elif c == 4:
            s.underline = True
        elif c == 24:
            s.underline = False
        elif c == 7:
            s.reverse = True
        elif c == 27:
            s.reverse = False
        elif 30 <= c <= 37:
            s.fg = XTERM16[c - 30]
        elif 90 <= c <= 97:
            s.fg = XTERM16[c - 90 + 8]
        elif 40 <= c <= 47:
            s.bg = XTERM16[c - 40]
        elif 100 <= c <= 107:
            s.bg = XTERM16[c - 100 + 8]
        elif c == 39:
            s.fg = None
        elif c == 49:
            s.bg = None
        elif c in (38, 48) and i + 1 < len(p):
            if p[i + 1] == 5 and i + 2 < len(p):
                col = xterm256(p[i + 2])
                i += 2
            elif p[i + 1] == 2 and i + 4 < len(p):
                col = (p[i + 2], p[i + 3], p[i + 4])
                i += 4
            else:
                col = None
            if c == 38:
                s.fg = col
            else:
                s.bg = col
        i += 1
    return s


def cells(line: str) -> list[tuple[str, Style]]:
    """One (char, style) per terminal cell; a wide char takes two cells (the second is "")."""
    out: list[tuple[str, Style]] = []
    style = Style()
    for m in re.finditer(r"\x1b\[([0-9;:]*)([A-Za-z])|(.)", line):
        if m.group(3) is None:
            if m.group(2) == "m":
                style = sgr(style, re.split(r"[;:]", m.group(1)) if m.group(1) else [])
            continue
        ch = m.group(3)
        out.append((ch, style))
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            out.append(("", style))
    return out


@lru_cache(maxsize=None)
def has_glyph(path: str, ch: str) -> bool:
    try:
        f = font(path, 20)
        return f.getmask(ch).getbbox() is not None or ch.isspace()
    except Exception:
        return False


@lru_cache(maxsize=None)
def glyph_font(ch: str, bold: bool, size: int) -> tuple[str, ImageFont.FreeTypeFont]:
    ch = SUBST.get(ch, ch)
    primary = MONO_B if bold else MONO
    if _mono_has(ch):
        return ch, font(primary, size)
    for fb in FALLBACKS:
        if _has(fb, ch):
            return ch, font(fb, size)
    return "?", font(primary, size)


@lru_cache(maxsize=None)
def _cmap(path: str) -> frozenset[int]:
    try:
        from fontTools.ttLib import TTFont  # optional: exact coverage when fontTools is around
        return frozenset(TTFont(path, lazy=True).getBestCmap())
    except Exception:
        return frozenset()


def _has(path: str, ch: str) -> bool:
    cm = _cmap(path)
    return ord(ch) in cm if cm else has_glyph(path, ch)


def _mono_has(ch: str) -> bool:
    return _has(MONO, ch)


def draw_terminal(img: Image.Image, box: tuple[int, int, int, int], screen: str, title: str,
                  rows: tuple[int, int] | None = None, cols: int = 84) -> None:
    """A terminal window at `box`, showing `rows` (start, end) of the recorded screen."""
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(box, radius=10, fill=TERM_BG, outline=RULE, width=2)
    d.rounded_rectangle((x0, y0, x1, y0 + 34), radius=10, fill=TITLEBAR)
    d.rectangle((x0, y0 + 24, x1, y0 + 34), fill=TITLEBAR)
    for n, c in enumerate(((226, 102, 94), (235, 186, 88), (118, 196, 126))):
        cx, cy = x0 + 20 + n * 20, y0 + 17
        d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=c)
    tf = font(SANS_B, 15)
    d.text(((x0 + x1 - d.textlength(title, font=tf)) / 2, y0 + 8), title, font=tf, fill=INK_2)
    lines = screen.rstrip("\n").split("\n")
    if rows is not None:
        lines = lines[rows[0]:rows[1]]
    pad = 18
    inner_w = x1 - x0 - 2 * pad
    size = max(10, int(inner_w / cols / 0.6021))
    cw = font(MONO, size).getlength("M")
    lh = int(size * 1.22)
    max_rows = (y1 - y0 - 34 - 2 * 12) // lh
    lines = lines[-max_rows:] if len(lines) > max_rows else lines
    y = y0 + 34 + 12
    for line in lines:
        x = x0 + pad
        for ch, st in cells(line):
            fg = st.fg or TERM_FG
            bg = st.bg
            if st.reverse:
                fg, bg = (bg or TERM_BG), fg
            if st.dim:
                fg = tuple((a + b) // 2 for a, b in zip(fg, TERM_BG))
            if bg is not None:
                d.rectangle((x, y - 1, x + cw, y + lh - 1), fill=bg)
            if ch and not ch.isspace():
                g, f = glyph_font(ch, st.bold, size)
                d.text((x, y), g, font=f, fill=fg)
            if st.underline and ch:
                d.line((x, y + lh - 3, x + cw, y + lh - 3), fill=fg)
            x += cw
        y += lh


def follow(screen: str, keep: int, skip_header: bool = True) -> tuple[int, int]:
    """Rows to show: the last `keep` rows up to the last non-empty one, never Claude's banner."""
    lines = [strip(x) for x in screen.rstrip("\n").split("\n")]
    last = max((i for i, x in enumerate(lines) if x.strip()), default=0)
    first = 0
    if skip_header and any("Claude Code v" in x for x in lines):
        # Claude Code's banner (logo, model, plan, notices) ends before the first rule or prompt
        for i, x in enumerate(lines):
            if x.startswith("─") or x.startswith("❯"):
                first = i
                break
    start = max(first, last + 1 - keep)
    return start, last + 1


# ----------------------------------------------------------------- browser
@lru_cache(maxsize=64)
def load_shot(path: str) -> Image.Image:
    return Image.open(path).convert("RGB")


def draw_browser(img: Image.Image, area: tuple[int, int, int, int], shot: Path, crop: tuple[float, ...],
                 dpr: int) -> None:
    """The screenshot's `crop` (CSS px), scaled to fit `area` and centred in it."""
    pic = load_shot(str(shot))
    cx0, cy0, cx1, cy1 = (v * dpr for v in crop)
    ax0, ay0, ax1, ay1 = area
    scale = min((ax1 - ax0) / (cx1 - cx0), (ay1 - ay0) / (cy1 - cy0))
    w, h = int((cx1 - cx0) * scale), int((cy1 - cy0) * scale)
    part = pic.resize((w, h), Image.LANCZOS, box=(cx0, cy0, cx1, cy1))
    x, y = ax0 + (ax1 - ax0 - w) // 2, ay0 + (ay1 - ay0 - h) // 2
    d = ImageDraw.Draw(img)
    d.rectangle((x - 3, y - 3, x + w + 2, y + h + 2), outline=RULE, width=3)
    img.paste(part, (x, y))


def lerp_box(a: tuple[float, ...], b: tuple[float, ...], u: float) -> tuple[float, ...]:
    u = u * u * (3 - 2 * u)  # ease in and out
    return tuple(p + (q - p) * u for p, q in zip(a, b))


# ----------------------------------------------------------------- captions
def caption(img: Image.Image, step: str, text: str) -> None:
    d = ImageDraw.Draw(img)
    d.rectangle((0, H - CAPTION_H, W, H), fill=(12, 14, 15))
    f, fb = font(SANS, 26), font(SANS_B, 26)
    tw = (d.textlength(step + "  ", font=fb) if step else 0) + d.textlength(text, font=f)
    x = (W - tw) / 2
    y = H - CAPTION_H + 17
    if step:
        d.text((x, y), step, font=fb, fill=COPPER)
        x += d.textlength(step + "  ", font=fb)
    d.text((x, y), text, font=f, fill=INK)


def canvas() -> Image.Image:
    return Image.new("RGB", (W, H), BG)


CONTENT = (40, 24, W - 40, H - CAPTION_H - 20)


# ------------------------------------------------------------------- scenes
@dataclass
class Scene:
    name: str
    dur: float
    draw: Callable[[float], Image.Image]      # local time (s) -> frame


def timemap(points: list[tuple[float, float]]) -> Callable[[float], float]:
    """Piecewise-linear map from output time to capture time, through (out, src) points."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]

    def f(x: float) -> float:
        if x <= xs[0]:
            return ys[0]
        for i in range(1, len(xs)):
            if x <= xs[i]:
                u = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
                return ys[i - 1] + u * (ys[i] - ys[i - 1])
        return ys[-1]
    return f


def title_card(big: str, sub: str, small: str = "") -> Callable[[float], Image.Image]:
    def draw(_t: float) -> Image.Image:
        img = canvas()
        d = ImageDraw.Draw(img)
        f = font(SANS_B, 96)
        d.text(((W - d.textlength(big, font=f)) / 2, 210), big, font=f, fill=INK)
        d.line((W / 2 - 60, 345, W / 2 + 60, 345), fill=COPPER, width=4)
        sf = font(SANS, 34)
        d.text(((W - d.textlength(sub, font=sf)) / 2, 380), sub, font=sf, fill=INK_2)
        if small:
            s2 = font(SANS, 24)
            d.text(((W - d.textlength(small, font=s2)) / 2, 450), small, font=s2, fill=INK_3)
        return img
    return draw


def end_card(tag: str, _t: float) -> Image.Image:
    img = canvas()
    d = ImageDraw.Draw(img)
    f = font(SANS_B, 64)
    d.text(((W - d.textlength("switchboard", font=f)) / 2, 120), "switchboard", font=f, fill=INK)
    mf = font(MONO, 26)
    cmds = [f"uv tool install git+https://github.com/amahpour/switchboard@{tag}",
            "switchboard install all", "switchboard start"]
    bw = max(d.textlength("$ " + c, font=mf) for c in cmds) + 60
    bx = (W - bw) / 2
    d.rounded_rectangle((bx, 250, bx + bw, 250 + 50 * len(cmds) + 36), radius=12, fill=TERM_BG, outline=RULE,
                        width=2)
    for i, c in enumerate(cmds):
        d.text((bx + 30, 268 + 50 * i), "$ ", font=mf, fill=COPPER)
        d.text((bx + 30 + d.textlength("$ ", font=mf), 268 + 50 * i), c, font=mf, fill=TERM_FG)
    sf = font(SANS, 30)
    msg = "then tell your agents: join switchboard room #build"
    d.text(((W - d.textlength(msg, font=sf)) / 2, 470), msg, font=sf, fill=INK_2)
    uf = font(SANS_B, 30)
    url = "github.com/amahpour/switchboard"
    d.text(((W - d.textlength(url, font=uf)) / 2, 560), url, font=uf, fill=COPPER)
    return img



def build_scenes(c: Capture) -> list[Scene]:
    sc = []
    sc.append(Scene("title", 2.6, title_card("switchboard", "a group chat for you and your coding agents",
                                             "Claude Code · Codex · Cursor · Devin")))

    def term_scene(pane: str, title: str, tmap: Callable[[float], float], step: str, text: str,
                   window: Callable[[str, float], tuple[int, int] | None] | None = None,
                   ) -> Callable[[float], Image.Image]:
        def draw(t: float) -> Image.Image:
            img = canvas()
            scr = c.term(pane, tmap(t))
            rows = window(scr, t) if window else None
            draw_terminal(img, (140, 24, W - 140, H - CAPTION_H - 20), scr, title, rows)
            caption(img, step, text)
            return img
        return draw

    # 1. install: type at double speed, then the output, then a beat on "Installed"
    t_start = c.marks["start"]
    t_typed = c.when("term", r"switchboard@v\d", t_start)
    t_inst = c.when("term", r"Installed 1 executable", t_start)
    sc.append(Scene("install", 3.8, term_scene("term", "Terminal", timemap(
        [(0, t_start), (1.6, t_typed + 0.4), (2.8, t_inst), (3.8, t_inst + 0.1)]),
        "1", "Install switchboard from GitHub")))
    # 2. register: the diff, Apply? y, the summary
    r0, r1 = c.scenes["register"]
    t_ask = c.when("term", r"Apply\? \[y/N\]\s*$", r0)
    t_sum = c.when("term", r"claude: installed", r0)
    sc.append(Scene("register", 5.2, term_scene("term", "Terminal", timemap(
        [(0, r0), (1.0, t_ask - 0.2), (2.0, t_ask), (3.2, t_ask + 1.9), (3.9, t_sum), (5.2, t_sum + 0.2)]),
        "2", "Register it with your agents: it shows the diff and asks first")))
    # 3. start
    s0, s1 = c.scenes["start"]
    t_link = c.when("term", r"login\?t=", s0)
    sc.append(Scene("start", 2.8, term_scene("term", "Terminal", timemap(
        [(0, s0), (1.1, t_link), (2.8, t_link + 0.2)]),
        "3", "Start it: it prints a one-time sign-in link")))

    # 4. sign in and create #build
    t_in, t_room = c.first("signed-in"), c.first("room")

    def signin(t: float) -> Image.Image:
        img = canvas()
        shot = c.shot(t_in if t < 1.5 else t_room, ("signed-in", "room"))
        draw_browser(img, CONTENT, shot, WINDOW, c.dpr)
        caption(img, "4", "Open the link and create a room")
        return img
    sc.append(Scene("signin", 3.0, signin))

    # 5. join: the agent's terminal, then the buddy list
    j_ready = c.marks["claude-ready"]
    j_typed = c.when("claude", r"❯ join switchboard room #build as claude-1", j_ready)
    j_seen = c.first("joined")
    j_idle = max(t for t, _ in c.terms["claude"] if t <= c.scenes["join"][1])
    join_term = term_scene("claude", "Terminal — Claude Code", timemap(
        [(0, j_ready), (1.4, j_typed + 0.3), (2.6, j_seen), (4.4, j_idle)]),
        "5", "Tell your agent to join the room", window=lambda scr, _t: follow(scr, 22))

    def join(t: float) -> Image.Image:
        if t < 4.4:
            return join_term(t)
        img = canvas()
        draw_browser(img, CONTENT, c.shot(c.last("joined"), ("joined",)), TOP, c.dpr)
        caption(img, "5", "It shows up in the room, and its terminal stays its own")
        return img
    sc.append(Scene("join", 6.0, join))

    # 6. post, the agent wakes, it answers
    typing = [s for s in c.shots if s[2] in ("typing", "typed")]
    posted, answered = c.marks["posted"], c.marks["answered"]
    w_first = c.when("claude", r"message from alice", posted)

    def wake_window(scr: str, t: float) -> tuple[int, int]:
        """First the message as it arrives in the session, then scroll down with the work."""
        lines = [strip(x) for x in scr.rstrip("\n").split("\n")]
        top = next((i for i, x in enumerate(lines) if "sent a message" in x), 0)
        start_b, _ = follow(scr, 22)
        u = min(max((t - 1.2) / 0.8, 0.0), 1.0)
        start = round(top + (max(start_b, top) - top) * u)
        return start, start + 22

    w_done = c.when("claude", r"for \d+s · done", answered)
    wake_term = term_scene("claude", "Terminal — Claude Code", timemap(
        [(0, w_first - 0.05), (1.4, w_first + 0.8), (3.4, answered), (4.6, w_done + 0.2)]),
        "6", "The agent is woken in its own session and gets to work", window=wake_window)

    def wake(t: float) -> Image.Image:
        img = canvas()
        if t < 2.2:                                   # alice types
            i = min(int(t / 2.2 * len(typing)), len(typing) - 1)
            draw_browser(img, CONTENT, c.build / typing[i][1], CHAT, c.dpr)
            caption(img, "6", "Post a message: @claude-1 wakes that agent at once")
            return img
        if t < 3.0:                                   # sent
            draw_browser(img, CONTENT, c.shot(posted + 0.6, ("wake",)), CHAT, c.dpr)
            caption(img, "6", "Post a message: @claude-1 wakes that agent at once")
            return img
        if t < 7.6:
            return wake_term(t - 3.0)
        u = min((t - 7.6) / 1.2, 1.0)
        draw_browser(img, CONTENT, c.shot(answered + 1.0, ("answered",)), lerp_box(CHAT, LOG, u), c.dpr)
        caption(img, "7", "…and it answers in the room")
        return img
    sc.append(Scene("wake", 10.4, wake))
    sc.append(Scene("end", 3.6, lambda t: end_card(c.tag, t)))
    return sc


# ------------------------------------------------------------------ output
FADE = 0.25


def frames(scenes: list[Scene], fps: int, only: tuple[str, ...] | None = None):
    chosen = [s for s in scenes if only is None or s.name in only]
    for k, s in enumerate(chosen):
        n = int(round(s.dur * fps))
        nxt = chosen[k + 1] if k + 1 < len(chosen) else None
        for i in range(n):
            t = i / fps
            img = s.draw(t)
            if nxt is not None and t > s.dur - FADE:
                u = (t - (s.dur - FADE)) / FADE
                img = Image.blend(img, nxt.draw(0.0), u)
            yield img


def encode_mp4(scenes: list[Scene], out: Path, music: Path) -> None:
    total = sum(s.dur for s in scenes)
    ff = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-i", str(music), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "slow",
         "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-t", f"{total:.2f}",
         "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
    assert ff.stdin is not None
    for img in frames(scenes, FPS):
        ff.stdin.write(img.tobytes())
    ff.stdin.close()
    if ff.wait() != 0:
        sys.exit("ffmpeg failed (mp4)")


def encode_gif(scenes: list[Scene], out: Path, only: tuple[str, ...]) -> None:
    gh = int(H * GIF_W / W) // 2 * 2
    ff = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{GIF_W}x{gh}",
         "-r", str(GIF_FPS), "-i", "-", "-vf",
         "split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle",
         "-loop", "0", str(out)], stdin=subprocess.PIPE)
    assert ff.stdin is not None
    for img in frames(scenes, GIF_FPS, only):
        ff.stdin.write(img.resize((GIF_W, gh), Image.LANCZOS).tobytes())
    ff.stdin.close()
    if ff.wait() != 0:
        sys.exit("ffmpeg failed (gif)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--build", type=Path, required=True)
    ap.add_argument("--stills", action="store_true", help="also write one PNG per scene, for review")
    args = ap.parse_args()
    c = Capture.load(args.build)
    scenes = build_scenes(c)
    out = args.build / "out"
    out.mkdir(exist_ok=True)
    total = sum(s.dur for s in scenes)
    if args.stills:
        for s in scenes:
            for frac in (0.2, 0.6, 0.95):
                s.draw(s.dur * frac).save(out / f"still-{s.name}-{int(frac * 100):02d}.png")
    music = out / "music.wav"
    subprocess.run([sys.executable, str(HERE / "music.py"), "--seconds", f"{total:.2f}", "--out", str(music)],
                   check=True)
    encode_mp4(scenes, out / "switchboard.mp4", music)
    encode_gif(scenes, out / "switchboard.gif", ("join", "wake"))
    for f in ("switchboard.mp4", "switchboard.gif"):
        print(f"{out / f}: {(out / f).stat().st_size / 1e6:.1f} MB")
    print(f"length {total:.1f} s")


if __name__ == "__main__":
    main()
