"""The prototype's end-to-end check (questions 3, 5, 7 and the live-in-browser claim).

An agent is an MCP client of /agent/mcp with a bearer token; two people have the board open in headless
Chromium. Prints what happened; saves screenshots to the folder in argv[1].
"""
import asyncio, base64, json, sys, urllib.request
from pathlib import Path
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from playwright.sync_api import sync_playwright
from lib import server, open_board, draw

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)


def agent(base, token="tok-claude-1"):
    return Client(StreamableHttpTransport(base + "/agent/mcp", headers={"Authorization": "Bearer " + token}))


def get(base, path):
    return urllib.request.urlopen(base + path).read().decode()


async def call(c, name, **kw):
    r = await c.call_tool(name, kw, raise_on_error=False)
    return r.content[0].text if r.content and hasattr(r.content[0], "text") else r.content


def pump(page, coro):
    """Run an agent's coroutine in a thread while this thread keeps the (sync) browser serving."""
    import threading
    box = {}
    def go():
        try:
            box["r"] = asyncio.run(coro)
        except BaseException as e:
            box["e"] = e
    t = threading.Thread(target=go); t.start()
    while t.is_alive():
        page.wait_for_timeout(100)
    t.join()
    if "e" in box:
        raise box["e"]
    return box.get("r")


def run():
    with server() as base, sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx_a, pa, fa, la = open_board(b, base, "alice")
        ctx_b, pb, fb, lb = open_board(b, base, "bob")
        # 0. membership: no ticket, no board
        bad = b.new_context().new_page(); bad.goto(f"{base}/?who=mallory&ticket=x"); bad.wait_for_timeout(800)
        print("non-member socket:", bad.inner_text("#status"))
        r = None
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/agent/mcp", data=b"{}", headers={"Authorization": "Bearer nope"}))
        except Exception as e:
            r = str(e)
        print("agent without a token:", r)

        # 1. alice draws two boxes by hand-equivalent (API), an arrow between them
        draw(pa, [{"type": "rectangle", "id": "h1", "x": 0, "y": 0, "width": 200, "height": 80, "label": {"text": "Checkout"}},
                  {"type": "rectangle", "id": "h2", "x": 400, "y": 0, "width": 200, "height": 80, "label": {"text": "Dashboard"}},
                  {"type": "arrow", "x": 200, "y": 40, "start": {"id": "h1"}, "end": {"id": "h2"}}])
        pb.wait_for_timeout(1200)

        async def agent_part():
            async with agent(base) as c:
                tools = await c.list_tools()
                print("tools:", [t.name for t in tools])
                print("read:", await call(c, "read_board"))
                f1 = await call(c, "add_frame", title="Scene 1: sign in")
                print(f1)
                print(await call(c, "add_note", text="Alice opens the app", frame_id="f1"))
                print(await call(c, "add_note", text="Gets a code by text", frame_id="f1"))
                print(await call(c, "add_note", text="A new step", x=100, y=-200))
                print(await call(c, "connect", from_id="h2", to_id="n3", label="then"))
                print(await call(c, "draw_mermaid", source="flowchart LR\n  A[Start] --> B{OK?}\n  B -->|yes| C[Done]\n  B -->|no| A"))
                print("hostile:", await call(c, "add_note", text="<img src=x onerror=alert(1)> " + "A" * 500))
                print("bad type via mermaid click:", await call(c, "draw_mermaid", source='flowchart LR\n A --> B\n click A href "https://example.com/x"'))
                print("read:", await call(c, "read_board"))
        pump(pa, agent_part())
        pa.wait_for_timeout(1500)
        pa.screenshot(path=str(OUT / "alice-sees-agent.png")); pb.screenshot(path=str(OUT / "bob-sees-agent.png"))
        print("alice problems:", [l for l in la if "esm.sh" not in l][:5], "| esm.sh font errors:", sum("esm.sh" in l for l in la))

        # 2. live merge: alice and bob edit at once; the agent moves a note meanwhile
        draw(pa, [{"type": "rectangle", "id": "m1", "x": 0, "y": 900, "width": 160, "height": 60, "backgroundColor": "#a5d8ff"},
                  {"type": "rectangle", "id": "m2", "x": 300, "y": 900, "width": 160, "height": 60}])
        pb.wait_for_timeout(1200)
        def el(i):
            return next(e for e in json.loads(get(base, "/api/board"))["elements"] if e["id"] == i)
        patch = lambda pg, i, **props: pg.evaluate("a => window.__send({t:'patch', id:a[0], props:a[1]})", [i, props])
        # A: two people, two different shapes at the same moment
        patch(pa, "m1", x=100); patch(pb, "m2", x=400)
        pa.wait_for_timeout(1000)
        print("A different shapes:", "m1.x", el("m1")["x"], "m2.x", el("m2")["x"], "(both kept)" if el("m1")["x"] == 100 and el("m2")["x"] == 400 else "(LOST ONE)")
        # B: two people, the SAME shape at the same moment, different properties
        patch(pa, "m1", y=950); patch(pb, "m1", backgroundColor="#ffc9c9")
        pa.wait_for_timeout(1200)
        e = el("m1"); print("B same shape, two properties:", "y", e["y"], "color", e["backgroundColor"], "v", e["version"], "-> one writer wins the whole shape" if (e["y"] == 950) != (e["backgroundColor"] == "#ffc9c9") else "-> both survived")
        # C: the agent moves the shape while a person edits it
        async def move_it():
            async with agent(base) as c:
                return await call(c, "move", shape_id="m2", x=700, y=900)
        patch(pa, "m2", backgroundColor="#b2f2bb")
        print("C agent move during a person's edit:", pump(pa, move_it()))
        pa.wait_for_timeout(1200)
        e = el("m2"); print("   m2 now x", e["x"], "color", e["backgroundColor"], "v", e["version"])
        # convergence: what each browser holds, compared with the server's copy
        rx = pa.evaluate("window.__rx"), pb.evaluate("window.__rx")
        print("alice received", len(rx[0]), "messages,", sum(m["bytes"] for m in rx[0]), "bytes; bob", len(rx[1]), "messages,", sum(m["bytes"] for m in rx[1]), "bytes")
        print("history (who changed what):", [ (h["seq"], h["who"], h["n"]) for h in json.loads(get(base, "/api/board"))["history"]][-6:])
        a_png = pa.screenshot(); 
        # D: hostile checks: no links survive, no images, no script
        links = [e["id"] for e in json.loads(get(base, "/api/board"))["elements"] if e.get("link")]
        print("elements with a link on the broker:", links, "| rejected:", json.loads(get(base, "/api/board"))["rejected"][:6])
        print("done")
        b.close()


run()
