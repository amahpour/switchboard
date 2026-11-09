"""One real-agent trial for question 3 and 4: Claude Code (haiku) draws on or reads the board.

  python trial.py --task flow|story|edit|read-summary|read-image --variant shapes|mermaid --out DIR

Starts the board server, opens it as alice in headless Chromium, runs `claude -p` in a scratch folder with
only the board's tools, then grades the board from its JSON (no judgement by the agent) and takes a still.
Costs a few cents. Codex was not run (no usage left the day this was written).
"""
import argparse, json, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright
from lib import server, open_board, draw

TRANSCRIPT = """Call transcript, 2 minutes:
Alice: So when a customer signs up, we first send a verification email.
Bob: Right, and if they don't verify within a day we send one reminder.
Alice: Once verified they land on the onboarding screen, where they pick a plan.
Bob: Free plan goes straight to the dashboard. Paid plan goes to payment first.
Alice: If payment fails, back to the plan screen. If it works, the dashboard.
Bob: And the dashboard shows a welcome tour the first time."""

PROMPTS = {
    "flow": "The board is empty. Here is a call transcript. Sketch the flow it describes on the board: one shape per step, arrows between steps, short labels. Do not ask questions; just draw it, then say done.\n\n" + TRANSCRIPT,
    "story": "The board is empty. Make a storyboard of exactly six frames, left to right in order, for a 30-second ad for a tea brand: a sleepy morning, kettle, pouring, first sip, smile, logo. Each frame has a title and one short caption note inside it. Do not ask questions; draw it, then say done.",
    "edit": "A person drew a small flow on the board: Login -> Checkout -> Payment -> Done. Without disturbing anything else, rename the box 'Checkout' to 'Cart' and add a new box 'Coupon' between 'Cart' and 'Payment', connected in order (Cart -> Coupon -> Payment). Keep every other shape where it is. Read the board first. Then say done.",
    "read-summary": "Read the board once and answer in one line: the names of the boxes in order, and what the arrows connect. Nothing else.",
    "read-image": "Read the board once as an image (read_board with format image) and answer in one line: the names of the boxes in order, and what the arrows connect. Nothing else.",
}
SHAPE_TOOLS = ["add_note", "add_frame", "connect", "edit_text", "move", "remove", "read_board"]
MERMAID_TOOLS = ["draw_mermaid", "read_board", "remove"]
ALL = ["add_note", "add_frame", "connect", "edit_text", "move", "remove", "draw_mermaid", "read_board"]


def board(base):
    return json.loads(urllib.request.urlopen(base + "/api/board").read())


def seed_edit(page):
    draw(page, [
        {"type": "rectangle", "id": "p1", "x": 0, "y": 0, "width": 180, "height": 80, "label": {"text": "Login"}, "backgroundColor": "#a5d8ff"},
        {"type": "rectangle", "id": "p2", "x": 300, "y": 0, "width": 180, "height": 80, "label": {"text": "Checkout"}, "backgroundColor": "#a5d8ff"},
        {"type": "rectangle", "id": "p3", "x": 900, "y": 0, "width": 180, "height": 80, "label": {"text": "Payment"}, "backgroundColor": "#a5d8ff"},
        {"type": "rectangle", "id": "p4", "x": 1200, "y": 0, "width": 180, "height": 80, "label": {"text": "Done"}, "backgroundColor": "#b2f2bb"},
        {"type": "arrow", "x": 180, "y": 40, "start": {"id": "p1"}, "end": {"id": "p2"}},
        {"type": "arrow", "x": 480, "y": 40, "start": {"id": "p2"}, "end": {"id": "p3"}},
        {"type": "arrow", "x": 1080, "y": 40, "start": {"id": "p3"}, "end": {"id": "p4"}},
    ])


def overlaps(els):
    boxes = [e for e in els if not e.get("isDeleted") and e["type"] in ("rectangle", "ellipse", "diamond")]
    n = 0
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"] and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]:
                if not (a.get("frameId") == b["id"] or b.get("frameId") == a["id"]):
                    n += 1
    return n


def grade(task, data, seed):
    els = [e for e in data["elements"] if not e.get("isDeleted")]
    by = {e["id"]: e for e in els}
    def label(e):
        for b in e.get("boundElements") or []:
            t = by.get(b["id"])
            if t and t["type"] == "text" and t.get("containerId") == e["id"]:
                return t["text"]
        return e.get("name", "") if e["type"] == "frame" else ""
    shapes = [e for e in els if e["type"] in ("rectangle", "ellipse", "diamond")]
    arrows = [e for e in els if e["type"] == "arrow"]
    glued = [a for a in arrows if (a.get("startBinding") or {}).get("elementId") in by and (a.get("endBinding") or {}).get("elementId") in by]
    g = {"shapes": len(shapes), "arrows": len(arrows), "arrows_attached_both_ends": len(glued), "overlapping_shape_pairs": overlaps(els)}
    if task == "story":
        frames = sorted([e for e in els if e["type"] == "frame"], key=lambda e: (round(e["y"] / 100), e["x"]))
        g["frames"] = len(frames)
        g["frames_with_a_caption_inside"] = sum(1 for f in frames if any(s.get("frameId") == f["id"] for s in shapes + [e for e in els if e["type"] == "text"]))
        g["frame_titles_in_order"] = [f.get("name") for f in frames]
        g["pass"] = len(frames) == 6 and g["frames_with_a_caption_inside"] == 6
    elif task == "flow":
        g["pass"] = len(shapes) >= 7 and len(glued) >= 6 and g["overlapping_shape_pairs"] == 0
        g["labels"] = [label(s) for s in shapes]
    elif task == "edit":
        moved = [i for i in ("p1", "p3", "p4") if i in by and (by[i]["x"], by[i]["y"]) != (seed[i]["x"], seed[i]["y"])]
        g["people_shapes_moved_or_lost"] = [i for i in ("p1", "p3", "p4") if i not in by or i in moved]
        g["p2_label"] = label(by["p2"]) if "p2" in by else None
        names = {label(s): s["id"] for s in shapes}
        ok_order = False
        if "Coupon" in names and "p3" in by:
            def link(a, b):
                return any((x.get("startBinding") or {}).get("elementId") == a and (x.get("endBinding") or {}).get("elementId") == b for x in arrows)
            ok_order = link(names.get("Cart") or "p2", names["Coupon"]) and link(names["Coupon"], "p3")
        g["coupon_between"] = ok_order
        g["pass"] = g["p2_label"] == "Cart" and ok_order and not g["people_shapes_moved_or_lost"]
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True); ap.add_argument("--variant", default="shapes"); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    tag = f"{a.task}-{a.variant}"
    with server() as base, sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx, page, frame, logs = open_board(b, base, "alice")
        seed = {}
        if a.task in ("edit", "read-summary", "read-image"):
            seed_edit(page); page.wait_for_timeout(1500)
            seed = {e["id"]: e for e in board(base)["elements"]}
        work = Path(tempfile.mkdtemp(prefix="board-trial-"))
        cfg = work / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"board": {"type": "http", "url": base + "/agent/mcp", "headers": {"Authorization": "Bearer tok-claude-1"}}}}))
        allowed = SHAPE_TOOLS if a.variant == "shapes" else MERMAID_TOOLS
        if a.task.startswith("read"):
            allowed = ["read_board"]
        deny = [t for t in ALL if t not in allowed]
        prompt = PROMPTS[a.task]
        if a.variant == "mermaid" and not a.task.startswith("read"):
            prompt += "\n\nDraw it by writing Mermaid with the draw_mermaid tool."
        cmd = ["claude", "-p", prompt, "--model", "haiku", "--effort", "low", "--output-format", "json", "--max-turns", "25",
               "--mcp-config", str(cfg), "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
               "--tools", "", "--allowedTools", *[f"mcp__board__{t}" for t in allowed], "--disallowedTools", *[f"mcp__board__{t}" for t in deny]]
        t0 = time.time()
        # the (sync) browser must keep serving the socket while the agent runs: poll it from here
        p = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while p.poll() is None:
            page.wait_for_timeout(200)
            if time.time() - t0 > 300:
                p.kill()
        so, se = p.communicate()
        wall = time.time() - t0
        try:
            res = json.loads(so)
        except Exception:
            res = {"raw": so[:2000], "err": se[:2000]}
        page.wait_for_timeout(800)
        frame.locator(".excalidraw canvas.interactive").click(position={"x": 640, "y": 700})
        page.keyboard.press("Shift+1")  # zoom to fit
        page.wait_for_timeout(800)
        page.screenshot(path=str(out / f"{tag}.png"))
        data = board(base)
        (out / f"{tag}.elements.json").write_text(json.dumps(data["elements"]))
        # what reading this board costs: the text summary, and a picture of it
        read = {}
        if not a.task.startswith("read"):
            import asyncio, base64, struct
            from demo_client import read_image
            png = None
            import threading
            box = {}
            t = threading.Thread(target=lambda: box.update(png=asyncio.run(read_image(base))))
            t.start()
            while t.is_alive():
                page.wait_for_timeout(100)
            t.join()
            png = box["png"]
            w, h = struct.unpack(">II", png[16:24])
            chars = len(urllib.request.urlopen(base + "/api/summary").read())
            read = {"summary_chars": chars, "summary_tokens_est": round(chars / 3.5), "png_bytes": len(png), "png_px": [w, h], "image_tokens_est": round(w * h / 750)}
        rep = {"task": a.task, "variant": a.variant, "wall_s": round(wall, 1), "turns": res.get("num_turns"), "cost_usd": res.get("total_cost_usd"),
               "usage": res.get("usage"), "result": (res.get("result") or "")[:400], "is_error": res.get("is_error"),
               "grade": grade(a.task, data, seed) if not a.task.startswith("read") else None,
               "board_bytes": len(json.dumps(data["elements"])), "summary_chars": len(urllib.request.urlopen(base + "/api/summary").read()),
               "read_cost": read, "elements_total": len(data["elements"]), "history_entries": len(data["history"]), "rejected": data["rejected"], "console_problems": logs[:3]}
        (out / f"{tag}.json").write_text(json.dumps(rep, indent=1))
        print(json.dumps(rep, indent=1))
        b.close()


main()
