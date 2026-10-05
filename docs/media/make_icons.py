"""Make the web UI's PNG icons from its SVG mark.

    uv run python docs/media/make_icons.py

``src/switchboard/web/static/favicon.svg`` is the source. This writes, next to it:
- ``favicon-32.png``: 32x32 with transparent corners, for browsers without SVG favicons and for
  ``/favicon.ico``;
- ``apple-touch-icon.png``: 180x180, square-cornered and fully opaque (iOS rounds the corners
  itself and shows any transparency as black).

It renders them in Playwright's Chromium at 1:1 pixels (``uv run playwright install chromium``
once per machine). Run it whenever favicon.svg changes, and commit the three files together.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "src" / "switchboard" / "web" / "static"


def render(pw, svg: str, size: int, out: Path, *, transparent: bool) -> None:
    browser = pw.chromium.launch()
    try:
        page = browser.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
        sized = svg.replace("<svg ", f'<svg width="{size}" height="{size}" ', 1)
        page.set_content(f'<!doctype html><html><body style="margin:0">{sized}</body></html>')
        page.screenshot(
            path=str(out), omit_background=transparent, clip={"x": 0, "y": 0, "width": size, "height": size}
        )
        print(out)
    finally:
        browser.close()


def main() -> None:
    from playwright.sync_api import sync_playwright

    svg = (STATIC / "favicon.svg").read_text()
    # the touch icon's tile runs to the edges: iOS applies its own rounded mask
    square = re.sub(r'(<rect width="32" height="32") rx="[0-9.]+"', r"\1", svg, count=1)
    assert square != svg, "favicon.svg's tile is no longer the first <rect ... rx=...>"
    with sync_playwright() as pw:
        render(pw, svg, 32, STATIC / "favicon-32.png", transparent=True)
        render(pw, square, 180, STATIC / "apple-touch-icon.png", transparent=False)


if __name__ == "__main__":
    main()
