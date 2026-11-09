"""Drawing by hand (real mouse events), what a pen stroke costs on the socket, and the phone layout."""
import json, math, sys, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright
from lib import server, open_board, draw

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
get = lambda base: json.loads(urllib.request.urlopen(base + "/api/board").read())

with server() as base, sync_playwright() as pw:
    b = pw.chromium.launch()
    ca, pa, fa, la = open_board(b, base, "alice")
    cb, pb, fb, lb = open_board(b, base, "bob")
    # a rectangle drawn with the mouse (key r), then a pen stroke (key p) of ~300 points
    pa.mouse.click(640, 400); pa.keyboard.press("r")
    pa.mouse.move(300, 300); pa.mouse.down(); pa.mouse.move(500, 380, steps=10); pa.mouse.up()
    pa.wait_for_timeout(600)
    pa.keyboard.press("p")
    pts = [(600 + 150 * math.cos(t / 20), 450 + 80 * math.sin(t / 10)) for t in range(300)]
    pa.mouse.move(*pts[0]); pa.mouse.down()
    for x, y in pts[1:]:
        pa.mouse.move(x, y)
    pa.mouse.up(); pa.wait_for_timeout(1500)
    tx = pa.evaluate("window.__tx"); rx = pb.evaluate("window.__rx")
    types = [e["type"] for e in get(base)["elements"]]
    print("server holds:", types)
    stroke = [m for m in tx if m["bytes"] > 2000]
    print("alice sent", len(tx), "messages,", sum(m["bytes"] for m in tx), "bytes in total; largest message", max(m["bytes"] for m in tx))
    print("bob received", len(rx), "messages,", sum(m["bytes"] for m in rx), "bytes")
    freedraw = [e for e in get(base)["elements"] if e["type"] == "freedraw"]
    print("the stroke: points", len(freedraw[0]["points"]) if freedraw else None, "json bytes", len(json.dumps(freedraw[0])) if freedraw else None)
    pb.screenshot(path=str(OUT / "bob-sees-hand-drawing.png"))
    ca.close(); cb.close()

    # the phone
    cm, pm, fm, lm = open_board(b, base, "alice", viewport={"width": 390, "height": 844}, mobile=True)
    pm.wait_for_timeout(1500)
    pm.screenshot(path=str(OUT / "phone-board.png"))
    print("phone console problems:", lm[:3])
    # tap the rectangle tool, then drag with mouse events (is_mobile emulates touch UA; Chromium turns taps into pointer events)
    tools = fm.locator("[data-testid='toolbar-rectangle']")
    print("phone toolbar rectangle visible:", tools.count() and tools.first.is_visible())
    b.close()
