"""Screenshots of the CLI's coloured output (issue #28), for the PR and the docs.

    uv run python docs/media/cli_shots.py --out /tmp/cli-shots

The pictures are pull-request previews (CLAUDE.md): they go on the ``design-assets`` branch, not in
this repository, so the default output folder, ``docs/media/cli/``, is git-ignored.

Run by hand, never by pytest (nothing imports it). It needs Playwright's Chromium, once per
machine: ``uv run playwright install chromium``.

What it does:
- **Real commands, seeded data.** It isolates HOME the way ``ui_shots.py`` does and starts
  ``tests/ui_world.py``'s seeded broker: rooms, four agents, a Markdown conversation and a
  never-enabled ``fpga-pi`` remote. Then it runs the real ``switchboard`` commands in this
  process with ``--color always`` and keeps what they print. ``status``, ``who``, ``tail``,
  ``remote status`` and ``report`` run against that broker; ``install`` and ``uninstall``
  (``--dry-run``) run against a throwaway user home.
- **Stand-ins, never this machine.** The hooks an install writes run an interpreter; the diff
  shows ``/opt/switchboard/bin/python3`` in place of this checkout's venv. The PATH holds stub
  ``claude``, ``codex`` and ``devin`` commands and no Cursor CLI, so Cursor shows as skipped.
  Your own harness configs are never read or written.
- **Rendered as a terminal.** Each capture's colour codes become styled HTML: html-escaped
  text, the terminal's 8 colours plus bold and dim, and nothing else, so an unexpected code
  stops the run. Each capture sits in a window titled with its command, and Playwright's
  Chromium shoots it at DPR 2. Every command is shot in dark; install and tail also in light;
  and install once more with ``--color never``, for a before/after.
"""

from __future__ import annotations

import argparse
import contextlib
import html
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "media" / "cli"
sys.path.insert(0, str(Path(__file__).resolve().parent))  # ui_shots, for isolate()

PYTHON_STANDIN = "/opt/switchboard/bin/python3"
COLUMNS = 116  # the terminal's width in characters; longer lines wrap, as a terminal wraps them

SGR = re.compile(r"\x1b\[([0-9;]*)m")
COLOUR_CODES = {"31": "red", "32": "green", "33": "yellow", "34": "blue", "35": "magenta", "36": "cyan"}

# Two terminal themes: the 8 basic colours are the only ones switchboard asks for, so each
# theme maps just those (and bold and dim) to something that reads on its background.
THEMES: dict[str, dict[str, str]] = {
    "dark": {"bg": "#1c1e22", "fg": "#d8dbe0", "bar": "#26292e", "bar_fg": "#9aa1ab", "border": "#34383e",
             "prompt": "#8b939e", "red": "#f07178", "green": "#7fd17a", "yellow": "#e6c07b", "blue": "#6cb6ff",
             "magenta": "#d59ef5", "cyan": "#5fd0de"},
    "light": {"bg": "#fcfcfd", "fg": "#1f2328", "bar": "#eef0f3", "bar_fg": "#57606a", "border": "#d6dbe1",
              "prompt": "#6e7781", "red": "#c4271f", "green": "#1a7f37", "yellow": "#8a5a00", "blue": "#0550ae",
              "magenta": "#8250df", "cyan": "#0b6e82"},
}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title><style>
body {{ margin: 0; background: transparent; }}
.pad {{ display: inline-block; padding: 28px; }}
.win {{ border-radius: 10px; overflow: hidden; background: {bg}; border: 1px solid {border};
        box-shadow: 0 10px 30px rgba(0, 0, 0, .22); }}
.bar {{ height: 30px; display: flex; align-items: center; justify-content: center; background: {bar};
        color: {bar_fg}; border-bottom: 1px solid {border};
        font: 12px -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }}
pre {{ margin: 0; padding: 14px 18px 18px; width: {columns}ch; color: {fg}; white-space: pre-wrap;
       word-break: break-all; font: 13px/1.45 ui-monospace, "SF Mono", Menlo, Consolas, "DejaVu Sans Mono",
       monospace; }}
.p {{ color: {prompt}; }}
.b {{ font-weight: 700; }}
.d {{ opacity: .62; }}
.c-red {{ color: {red}; }} .c-green {{ color: {green}; }} .c-yellow {{ color: {yellow}; }}
.c-blue {{ color: {blue}; }} .c-magenta {{ color: {magenta}; }} .c-cyan {{ color: {cyan}; }}
</style></head>
<body><div class="pad"><div class="win"><div class="bar">{title}</div><pre><span class="p">$</span> {command}
{body}</pre></div></div></body></html>
"""


# ------------------------------------------------------------------ ANSI -> HTML
def to_html(text: str) -> str:
    """``text`` with switchboard's SGR codes turned into spans. Anything else that looks like
    an escape code is an error: the CLI only ever emits these."""
    if "\x1b" in SGR.sub("", text):
        raise ValueError("an escape code that isn't switchboard's own SGR reached the capture")
    out: list[str] = []
    bold = dim = False
    fg: str | None = None
    pos = 0

    def emit(s: str) -> None:
        if not s:
            return
        classes = [c for c, on in (("b", bold), ("d", dim)) if on] + ([f"c-{fg}"] if fg else [])
        esc = html.escape(s)
        out.append(f'<span class="{" ".join(classes)}">{esc}</span>' if classes else esc)

    for m in SGR.finditer(text):
        emit(text[pos:m.start()])
        pos = m.end()
        for code in (m.group(1) or "0").split(";"):
            if code in ("", "0"):
                bold = dim = False
                fg = None
            elif code == "1":
                bold = True
            elif code == "2":
                dim = True
            elif code in COLOUR_CODES:
                fg = COLOUR_CODES[code]
            else:
                raise ValueError(f"unexpected SGR code {code!r}")
    emit(text[pos:])
    return "".join(out)


def page(command: str, text: str, theme: str) -> str:
    return PAGE.format(title=html.escape(command.split(" --home")[0]), command=html.escape(command),
                       body=to_html(text.rstrip("\n")), columns=COLUMNS, **THEMES[theme])


# ------------------------------------------------------------------ captures
def capture(argv: list[str]) -> str:
    """What ``switchboard <argv>`` prints to stdout, run in this process."""
    from switchboard import cli

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.main(argv)
    return buf.getvalue()


def user_home(base: Path) -> Path:
    """A throwaway user home with a little of the user's own config, and a PATH of stub CLIs."""
    uh = base / "alice"
    (uh / ".claude").mkdir(parents=True)
    (uh / ".claude" / "settings.json").write_text('{\n  "model": "sonnet"\n}\n')
    (uh / ".codex").mkdir()
    (uh / ".codex" / "config.toml").write_text('model = "gpt-5.5"\n')
    bin_dir = base / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex", "devin"):  # no `agent` / `cursor-agent`: Cursor is skipped
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    os.environ["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    return uh


def excerpt_report(text: str) -> str:
    """The report's title and summary, then its "Rules that fired" table (the part #28 colours
    beyond the headings): the whole report is a few screens long."""
    lines = text.split("\n")
    plain = [SGR.sub("", x) for x in lines]
    start = plain.index("## Rules that fired")
    end = next(i for i in range(start + 1, len(plain)) if plain[i].startswith("## "))
    return "\n".join([*lines[:3], "", "…", "", *lines[start:end]])


def collect(world: Any, base: Path) -> list[tuple[str, str, str, tuple[str, ...]]]:
    """(file stem, the command as typed, its output, themes) for every shot."""
    home = str(world.home)
    h = ["--home", home]
    c = ["--color", "always"]
    shots: list[tuple[str, str, str, tuple[str, ...]]] = []

    def add(stem: str, typed: str, argv: list[str], themes: tuple[str, ...] = ("dark",)) -> str:
        text = capture(argv)
        shots.append((stem, typed, text, themes))
        return text

    add("status", "switchboard status", ["status", *h, *c])
    add("who", "switchboard who '#build'", ["who", "#build", *h, *c])
    add("tail", "switchboard tail '#build' -n 12 --no-follow", ["tail", "#build", "-n", "12", "--no-follow", *h, *c],
        ("dark", "light"))
    add("remote-status", "switchboard remote status", ["remote", "status", *h, *c])

    uh = user_home(base)
    real_python = sys.executable
    sys.executable = PYTHON_STANDIN  # what the hooks would run, as the diff shows it
    try:
        u = ["--user-home", str(uh)]
        add("install", "switchboard install all --dry-run", ["install", "all", "--dry-run", *h, *u, *c],
            ("dark", "light"))
        add("install-plain", "switchboard install all --dry-run", ["install", "all", "--dry-run", *h, *u,
                                                                   "--color", "never"])
        capture(["install", "all", "--yes", "--allow-editable", *h, *u])  # so uninstall has something to show
        add("uninstall", "switchboard uninstall all --dry-run", ["uninstall", "all", "--dry-run", *h, *u, *c])
    finally:
        sys.executable = real_python

    # two rules that fire, so "Rules that fired" has something to colour
    world.command("build", "/hold codex-1")
    world.command("build", "/release codex-1")
    text = capture(["report", *h, "--room", "#build", *c])
    shots.append(("report", "switchboard report --room '#build'", excerpt_report(text), ("dark",)))
    return shots


# ------------------------------------------------------------------ main
def shoot(browser: Any, out: Path, shots: list[tuple[str, str, str, tuple[str, ...]]]) -> None:
    for stem, typed, text, themes in shots:
        for theme in themes:
            ctx = browser.new_context(device_scale_factor=2, viewport={"width": 1400, "height": 900},
                                      color_scheme=theme)
            try:
                pg = ctx.new_page()
                pg.set_content(page(typed, text, theme))
                pg.evaluate("document.fonts ? document.fonts.ready.then(() => true) : true")
                path = out / f"{stem}-{theme}.png"
                pg.locator(".pad").screenshot(path=str(path), omit_background=True, animations="disabled")
                print(path)
            finally:
                ctx.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--out", default=str(OUT), help="output directory (default docs/media/cli)")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    from ui_shots import REAL_HOME, isolate

    # Playwright's driver starts before HOME moves, so it finds its browsers under the real HOME
    pw = sync_playwright().start()
    fake_home = isolate()
    base = Path(tempfile.mkdtemp(prefix="cli-shots-", dir="/tmp"))
    try:
        from ui_world import UIWorld

        out = Path(args.out).resolve()
        out.mkdir(parents=True, exist_ok=True)
        world = UIWorld()
        try:
            world.start()
            shots = collect(world, base)
        finally:
            world.stop()
        browser = pw.chromium.launch(env={**os.environ, "HOME": REAL_HOME or os.environ["HOME"]})
        try:
            shoot(browser, out, shots)
        finally:
            browser.close()
    finally:
        pw.stop()
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(fake_home, ignore_errors=True)


if __name__ == "__main__":
    main()
