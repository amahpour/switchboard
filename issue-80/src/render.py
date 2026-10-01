"""Render the issue-80 mockups to PNG: uv run python notes/issue-80/render.py OUT_DIR"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
src = pathlib.Path(__file__).resolve().parent
boards = [("Dossier", 1440, 900, "a-dossier"), ("Inbox", 1440, 900, "b-inbox"), ("InboxPhone", 390, 844, "b-inbox-phone"),
          ("Trial", 1440, 900, "c-argument"), ("Board", 1440, 900, "d-board"), ("Tour", 1440, 900, "e-walkthrough")]
with sync_playwright() as p:
    browser = p.chromium.launch()
    for name, w, h, fn in boards:
        ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2)
        page = ctx.new_page()
        page.goto((src / f"{name}.dc.html").as_uri())
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / f"{fn}.png"), clip={"x": 0, "y": 0, "width": w, "height": h})
        print(out / f"{fn}.png")
        ctx.close()
    browser.close()
