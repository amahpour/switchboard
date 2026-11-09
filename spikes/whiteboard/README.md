# Whiteboard spike (issue 622)

Throwaway prototype. Not part of switchboard, not imported by it, not packaged. It answers the spike's
questions; the answers are in the comment on issue 622.

What it is: a stand-in broker (`server.py`) with a board of Excalidraw elements, a WebSocket for people, MCP tools
for agents (`add_note`, `add_frame`, `connect`, `edit_text`, `move`, `remove`, `draw_mermaid`, `read_board`), and the
board running in a sandboxed iframe (`host.html` + `host.js` relay by postMessage; `board.jsx` is the frame).

## Run it again

You need Node, `uv` (from the repo root) and an `esbuild` binary. Nothing here is installed globally.

```sh
cd spikes/whiteboard
npm ci                                  # react, react-dom, @excalidraw/excalidraw 0.18.1, mermaid-to-excalidraw
ESBUILD=/path/to/esbuild ./build.sh     # writes ./build (not committed)
```

Then, from the repo root, one thing at a time on a shared machine (`~/.local/bin/sb-suite` is the maintainer's lock):

```sh
uv run --frozen python spikes/whiteboard/smoke.py OUTDIR     # sandbox, CSP, load size
uv run --frozen python spikes/whiteboard/demo.py OUTDIR      # agent tools, merge, security checks
uv run --frozen python spikes/whiteboard/hand.py OUTDIR      # mouse drawing, stroke cost, phone
uv run --frozen python spikes/whiteboard/trial.py --task flow|story|edit|read-summary|read-image \
    --variant shapes|mermaid --out OUTDIR                    # a real Claude Code (haiku) run, graded from the board
```

`trial.py` calls `claude -p --model haiku` with only the board's MCP tools (a few cents a run). The tokens in
the server (`tok-claude-1`, `t-alice`) are stand-ins, not secrets.

## Known gaps (it is a spike)

- One board, in memory, no SQLite, no versions beyond a list of who changed how many elements.
- A browser must be open: conversion of Mermaid and shape skeletons into elements, and rasterizing, happen in it.
- Arrows made by `connect` to a shape that was already there sometimes stop short of it.
- Images, embeds and links are refused for everyone; there is no file store.
