"""Render the issue #80 design prototype with local Playwright Chromium.

Run with a Python environment containing Playwright and an installed Chromium.
The HTML has no external dependencies. Outputs stay beside its source.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
SCREENS = [
    ("01-overview.png", {"view": "overview"}, 1440, 1000),
    ("02-review.png", {"view": "review"}, 1440, 1000),
    ("03-decision.png", {"view": "decision"}, 1440, 1000),
    ("04-evidence.png", {"view": "evidence", "item": "C2"}, 1440, 1000),
    ("05-walkthrough.png", {"view": "tour", "stop": "3"}, 1440, 1000),
    ("06-outgoing.png", {"view": "outgoing"}, 1440, 1000),
    ("07-revision.png", {"view": "review", "phase": "stale"}, 1440, 1000),
    ("08-review-phone.png", {"view": "review"}, 390, 844),
    ("09-decision-phone.png", {"view": "decision"}, 390, 844),
    ("10-review-dark.png", {"view": "review", "theme": "dark"}, 1440, 1000),
    ("11-shop-decision.png", {"view": "decision", "case": "shop"}, 1440, 1000),
    ("12-shop-pending.png", {"view": "outgoing", "case": "shop", "answer": "followup"}, 1440, 1000),
    ("13-shop-rechecked.png", {"view": "outgoing", "case": "shop", "answer": "followup", "rechecked": "1"}, 1440, 1000),
    ("14-all-claims.png", {"view": "review", "expanded": "1"}, 1440, 1000),
    ("15-in-progress.png", {"view": "review", "phase": "initial"}, 1440, 1000),
    ("16-broken-claim.png", {"view": "review", "phase": "verdict"}, 1440, 1000),
]


def main() -> None:
    report = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, query, width, height in SCREENS:
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=2)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(ROOT.joinpath("index.html").as_uri() + "?" + urlencode(query))
            page.wait_for_selector("main")
            page.evaluate("document.fonts.ready")
            page.screenshot(path=str(ROOT / name), full_page=True)
            layout = page.evaluate("({width: innerWidth, scrollWidth: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight})")
            report.append({"screen": name, **layout, "errors": errors})
            page.close()
        browser.close()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
