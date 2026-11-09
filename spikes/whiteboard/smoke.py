"""Open the board in the sandboxed frame; report console errors, CSP violations, load size; screenshot."""
import json, sys
from pathlib import Path
from playwright.sync_api import sync_playwright
from lib import server, open_board

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
with server() as base, sync_playwright() as pw:
    b = pw.chromium.launch()
    reqs = []
    ctx, page, frame, logs = open_board(b, base)
    page.on("requestfinished", lambda r: reqs.append(r))
    page.wait_for_timeout(1500)
    page.screenshot(path=str(OUT / "smoke-desktop.png"))
    try:
        page.evaluate("document.getElementById('board').contentWindow.location.href"); print("frame is same-origin (BAD)")
    except Exception as e:
        print("host cannot reach into the frame (opaque origin):", "cross-origin" in str(e))
    import re, collections
    c = collections.Counter(re.sub(r"[0-9a-f]{32}", "<hash>", re.sub(r"Xiaolai-Regular-\w+", "Xiaolai", l))[:230] for l in logs)
    print("console problems:", len(logs)); [print("  ", n, "x", k) for k, n in c.most_common(8)]
    ctx.close()
    # transfer sizes of a cold load
    ctx = b.new_context(viewport={"width": 1280, "height": 800}); pg = ctx.new_page()
    sizes = []
    pg.on("response", lambda r: sizes.append((r.url, len(r.body()) if r.ok else 0)))
    pg.goto(f"{base}/?who=alice&ticket=t-alice"); pg.frame_locator("#board").locator(".excalidraw canvas.interactive").wait_for(timeout=30000); pg.wait_for_timeout(1500)
    tot = sum(s for _, s in sizes)
    print("cold load files:", len(sizes), "bytes:", tot)
    for u, s in sorted(sizes, key=lambda x: -x[1])[:8]: print("  ", s, u.replace(base, ""))
    b.close()
