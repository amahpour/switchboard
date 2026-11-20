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
process.stdout.write(JSON.stringify(JSON.parse(fs.readFileSync(0, 'utf8')).map(e => r.clean(e))));"""
    result = subprocess.run(
        [shutil.which("node") or "node", "-e", code],
        input=json.dumps(cases),
        cwd=ROOT,
        env=child_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    got = json.loads(result.stdout)
    for e in (
        got
    ):  # the browser keeps up to 2000 characters of text for the broker to read; the stored copy is 200
        for key in ("text", "originalText", "name"):
            if e and key in e:
                e[key] = e[key][: board.MAX_TEXT]
    assert got == expected


def node(code: str, payload: object) -> object:
    result = subprocess.run(
        [
            shutil.which("node") or "node",
            "-e",
            "const r = require('./src/switchboard/web/static/board-rules.js');" + code,
        ],
        input=json.dumps(payload),
        cwd=ROOT,
        env=child_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


TOKEN = "ghp" + "_" + "aB3" * 12


def test_the_browser_sends_whole_text_so_a_secret_across_character_200_reaches_the_broker_to_be_cut() -> None:
    """The frame's ``flush`` and the app's change handler both run ``batch`` before the socket send. Cutting
    there at 200 left a token that crossed the cut as a prefix the broker cannot recognize (#893). The client
    keeps what it may send, and the broker redacts and then cuts. Fails if the browser cuts at 200 first:
    the stored text would hold the 19-character prefix of the token."""
    text = "x" * (board.MAX_TEXT - 20) + " " + TOKEN
    el = dict(id="s1", type="text", x=0, y=0, width=10, height=10, version=1, versionNonce=1)
    el.update(text=text, originalText=text, name=text)
    (sent,) = node(
        "process.stdout.write(JSON.stringify(r.batch(JSON.parse(require('fs').readFileSync(0,'utf8')))))",
        [el],
    )  # type: ignore[misc]
    assert sent["text"] == text and sent["originalText"] == text and sent["name"] == text  # type: ignore[index]
    (stored,) = board.merge([], [sent], agent=False).elements
    assert "ghp_" not in json.dumps(stored) and "aB3aB3" not in json.dumps(stored)


def test_a_broker_copy_replaces_the_tabs_when_its_version_is_the_same_or_higher() -> None:
    """The tab that drew keeps its own copy until the broker answers, and ``merge`` keeps the tab's copy on a
    tie (same version and nonce), so the redacted echo lost and the author kept seeing the secret (#893).
    ``adopt`` lets the broker's copy win at the same version or higher; an older one loses. Fails if the
    committed copy loses the tie."""
    base = dict(id="a", type="rectangle", x=0, y=0, width=1, height=1, versionNonce=5)
    mine = {**base, "version": 2, "text": TOKEN}
    echo = {**base, "version": 2, "text": "[redacted: github token]"}
    code = """const [cur, inc] = JSON.parse(require('fs').readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(r.adopt(cur, inc)));"""
    assert node(code, [[mine], [echo]])[0]["text"] == "[redacted: github token]"  # type: ignore[index]
    assert node(code, [[mine], [{**echo, "version": 3}]])[0]["version"] == 3  # type: ignore[index]
    assert node(code, [[mine], [{**echo, "version": 1, "text": "old"}]])[0]["text"] == TOKEN  # type: ignore[index]
    assert len(node(code, [[mine], [{**echo, "id": "b"}]])) == 2  # type: ignore[arg-type]


def test_the_browser_keeps_the_original_words_as_the_text_like_the_broker() -> None:
    """The client's copy of the allowlist reads ``text`` as the broker does: the original words when there
    are any, and a text with a line break and no original is not sent. Fails if it keeps the wrapped copy."""
    el = dict(id="s1", type="text", x=0, y=0, width=10, height=10, version=1, versionNonce=1)
    code = "process.stdout.write(JSON.stringify(r.clean(JSON.parse(require('fs').readFileSync(0,'utf8')))))"
    got = node(code, {**el, "text": "one\ntwo", "originalText": "one two"})
    assert got["text"] == "one two"  # type: ignore[index]
    assert node(code, {**el, "text": "one\ntwo"}) is None


def test_a_stored_text_with_breaks_and_no_original_loads_but_is_refused_as_a_write() -> None:
    """The write rule (a text with a line break and no ``originalText`` is not sent) must not apply to what
    the broker stored under an older rule: ``batch`` of a board drops every shape when one fails. Fails if
    ``load`` is not honoured, and if a write is let through."""
    el = dict(id="s1", type="text", x=0, y=0, width=10, height=10, version=1, versionNonce=1, text="a\nb")
    load = node(
        "const e = JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "process.stdout.write(JSON.stringify({load: r.batch([e], true), write: r.batch([e])}))",
        el,
    )
    assert load["load"][0]["text"] == "a\nb" and load["write"] is None  # type: ignore[index]


def test_the_frame_source_reads_every_scene_in_load_mode_like_the_shipped_bundle() -> None:
    """The shipped frame bundle bundles the older rules, which accept what the broker stored. ``frame.jsx``
    imports the current ``board-rules.js``, so a rebuild would refuse a stored text with a line break and no
    original at every ``rules.batch`` unless each call says ``true`` (load mode): the scene would drop, or a
    picture come out empty. Fails if a call site in the readable source lacks it."""
    source = (ROOT / "scripts" / "board" / "frame.jsx").read_text()
    calls = [m.start() for m in __import__("re").finditer(r"rules\.batch\(", source)]
    assert len(calls) >= 5
    for start in calls:
        depth, i = 0, source.index("(", start)
        while True:
            depth += source[i] == "("
            depth -= source[i] == ")"
            if depth == 0:
                break
            i += 1
        assert source[start : i + 1].endswith(", true)"), source[start : i + 1]
