"""diagram.js's source clean-up, run for real in node (tests/diagram_harness.js; DESIGN.md §33).

Before Mermaid sees a diagram, the event-handler attributes of any complete one-line HTML tag in
it are dropped (issue #917): Mermaid's DOMPurify parses each label in the page's own documents,
and Chromium from 157 reports a handler attribute to the CSP as soon as it is parsed, which the
e2e tests count as a failure, though nothing runs. Everything else in the source stays as it was
written, so no diagram draws differently. Skipped where node is missing, except under
SWITCHBOARD_REQUIRE_NODE=1 (CI), where that fails."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import node_for_tests

HARNESS = Path(__file__).resolve().parents[1] / "diagram_harness.js"
NODE, pytestmark = node_for_tests()


def clean(*sources: str) -> list[str]:
    assert NODE is not None
    r = subprocess.run(
        [NODE, str(HARNESS)], input=json.dumps(sources), capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_a_handler_attribute_leaves_the_source_before_mermaid_parses_it() -> None:
    """Every spelling a browser parses as a handler attribute goes, valued or not: an attribute
    left in would be reported to the CSP under Chromium 157 and fail the hostile-diagram e2e test."""
    got = clean(
        'A["<img src=x onerror=window.__pwned=1> <b>bold</b>"] --> B',
        "A[<img/onerror=a>]",
        "A[<IMG ONERROR=a>]",
        'A[<img title=">" onerror="a" alt=\'b\'>]',
        "A[<p onclick>x</p>]",
        "A[<svg><g onload = 'a'></g></svg>]",
        "A[<b class=c onmouseover=a onfocus=b>x</b>]",
    )
    assert got == [
        'A["<img src=x > <b>bold</b>"] --> B',
        "A[<img/>]",
        "A[<IMG >]",
        "A[<img title=\">\"  alt='b'>]",
        "A[<p >x</p>]",
        "A[<svg><g ></g></svg>]",
        "A[<b class=c  >x</b>]",
    ]


def test_everything_else_in_a_diagram_is_left_as_written() -> None:
    """Mermaid's own syntax, text that only looks like a tag, and a tag that doesn't close on its
    line are not touched, so a diagram draws exactly as before: only handler attributes go."""
    keep = (
        "flowchart TD\n  A --> B\n  A <--> C\n  one --> onward\n  A -- on hold --> B",
        "classDiagram\n  class Shape{\n    <<interface>>\n    onDraw()\n  }",
        "sequenceDiagram\n  A->>B: x < y and onerror=1\n  B-->>A: <b>bold</b>",
        'flowchart TD\n  A["<img src=x\n onerror=a>"]',  # the tag doesn't close on its line
        'flowchart TD\n  A["<img onerror="a\n">"]',  # nor does the value
        "graph LR\n  A[a < b] --> B[<x]",
        "flowchart TD\n  A[\"<img title='onerror=a' alt=on>\"]",  # \"on\" in a value is a value
    )
    assert clean(*keep) == list(keep)


def test_any_attribute_named_on_something_goes_its_value_with_it() -> None:
    """The rule is by name, as the browser's is: an attribute whose name starts with "on" is
    dropped with its value (DOMPurify would drop a name it doesn't know, such as `one`, anyway)."""
    assert clean("A[\"<img one=1 title='x'>\"]") == ["A[\"<img  title='x'>\"]"]
