"""Helpers for the spike's drivers: start/stop the board server (by pid), open a board as a person."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).parent


@contextmanager
def server(port: int = 8765):
    env = {**os.environ, "BOARD_PORT": str(port)}
    p = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/summary", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        p.terminate()
        p.wait(10)


def open_board(browser, base: str, who: str = "alice", viewport=None, mobile: bool = False):
    ctx = browser.new_context(viewport=viewport or {"width": 1280, "height": 800}, has_touch=mobile, is_mobile=mobile)
    page = ctx.new_page()
    logs: list[str] = []
    page.on("console", lambda m: logs.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: logs.append(f"pageerror: {e}"))
    page.goto(f"{base}/?who={who}&ticket=t-{who}")
    frame = page.frame_locator("#board")
    frame.locator(".excalidraw canvas.interactive").wait_for(timeout=30000)
    return ctx, page, frame, logs


def draw(page, skeleton):
    """Draw as a person would (through the editor's own API, in the browser)."""
    page.evaluate("s => window.__draw(s)", skeleton)
