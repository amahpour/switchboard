"""Both sides of the opaque frame enforce the broker's element contract (§56.8, #657)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from conftest import child_env

from switchboard import board

ROOT = Path(__file__).resolve().parents[2]


def test_browser_scrubs_the_same_fields_and_rejects_hostile_shapes_as_the_server() -> None:
    """A compromised frame cannot turn a link, embed, oversized stroke or unknown field into a page write."""
    base = dict(id="s1", type="rectangle", x=0, y=0, width=100, height=50, version=1, versionNonce=1)
    cases = [
        {**base, "text": "<img onerror=alert(1)>", "link": "https://example.com", "script": "bad"},
        {**base, "type": "image"},
        {**base, "x": 1e12, "fontFamily": 9, "opacity": 101},
        {**base, "type": "freedraw", "points": [[0, 0]] * 501},
        {
            **base,
            "type": "arrow",
            "points": [[0, 0], [100, 20]],
            "endBinding": {"elementId": "other", "focus": 9, "gap": -10, "href": "bad"},
        },
        {
            **base,
            "type": "arrow",
            "points": [[0, 0], [9, 9]],
            "startBinding": {"elementId": "o", "fixedPoint": [0.25, 1]},
        },
        {
            **base,
            "type": "arrow",
            "points": [[0, 0], [9, 9]],
            "endBinding": {"elementId": "o", "fixedPoint": [0.5, 1.5]},
        },
        {
            **base,
            "type": "arrow",
            "points": [[0, 0], [9, 9]],
            "endBinding": {"elementId": "o", "fixedPoint": [0.5]},
        },
        {
            **base,
            "type": "arrow",
            "points": [[0, 0], [9, 9]],
            "endBinding": {"elementId": "o", "fixedPoint": ["a", 0]},
        },
        {
            **base,
            "type": "arrow",
            "points": [[0, 0], [9, 9]],
            "endBinding": {"elementId": "o", "fixedPoint": [True, 0]},
        },
        {
            **base,
            "type": "arrow",
            "points": [[0, 0], [9, 9]],
            "endBinding": {"elementId": "o", "fixedPoint": [-0.1, 0]},
        },
        {**base, "text": "Ａ\u202e\u200b\ue000\x00\r\n\t界"},
        {**base, "id": "../bad"},
        {**base, "version": True},
        {**base, "text": "x" * 300, "boundElements": [{"id": "t1", "type": "text", "extra": 1}]},
    ]
    expected = []
    for raw in cases:
        try:
            expected.append(board.clean_element(raw, agent=False))
        except board.Reject:
            expected.append(None)
    code = """const fs = require('fs');
const r = require('./src/switchboard/web/static/board-rules.js');
process.stdout.write(JSON.stringify(JSON.parse(fs.readFileSync(0, 'utf8')).map(r.clean)));"""
    result = subprocess.run(
        [shutil.which("node") or "node", "-e", code],
        input=json.dumps(cases),
        cwd=ROOT,
        env=child_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == expected
