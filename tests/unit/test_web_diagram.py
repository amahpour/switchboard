"""diagram.js's source clean-up, run for real in node (tests/diagram_harness.js; DESIGN.md §33).

Before Mermaid sees a diagram, the event-handler attributes of any complete one-line HTML tag in
it are dropped (issue #917): Mermaid's DOMPurify parses each label in the page's own documents,
and Chromium from 157 reports a handler attribute to the CSP as soon as it is parsed, which the
e2e tests count as a failure, though nothing runs. Everything else in the source stays as it was
written, so no diagram draws differently. Skipped where node is missing, except under
SWITCHBOARD_REQUIRE_NODE=1 (CI), where that fails."""

from __future__ import annotations

import json
import random
import subprocess
import time
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


def test_a_comparison_in_a_label_that_runs_into_an_arrow_is_left_as_written() -> None:
    """The review's case on #918: `a<b only` in a quoted label read as a tag `<b` with an
    attribute `only"]`, closed by the edge's `-->`, and the strip took the label's closing quote
    and bracket with it, so Mermaid refused a valid flowchart. It isn't a tag: nothing changes."""
    keep = (
        'flowchart TD\n  A["if a<b only"] --> B["done"]',
        "flowchart TD\n  A[x<y one] --> B",
        "flowchart TD\n  A(a<b on=1) --> B",
        "flowchart TD\n  A[a<b onx=1]-->B",
        'flowchart TD\n  A["n<m onwards"] -- on hold --> B["n>m"]',
        "sequenceDiagram\n  alice->>bob: i<j only, and onward -->> back",
    )
    assert clean(*keep) == list(keep)


# Pieces of the labels of ordinary diagrams: comparisons, quotes, arrows, entities, <br> and <b>
# tags, and words that start with "on". OPEN leaves a '<' and a letter unclosed; a label that has
# one gets no later piece with a '>' or a '=', so no piece list can spell a real tag with an on…
# attribute (that would be a hostile diagram, which the tests above cover).
WORDS = ("one", "only", "on", "onward", "x", "total", "it's", "a = b", "(b)", "n:1", "#quot;")
SIGNS = (" < ", " > ", "<=", ">=", "&lt;", "&gt;", "&amp;", "#lt;", "-->", "->>", "<br>", "<br/>")
TAGS = ("<b>bold</b>", "<i>it</i>", "<<interface>>")
OPEN = ("a<b", "x<y", "n<m")


def label(rng: random.Random) -> str:
    out: list[str] = []
    opened = False
    for _ in range(rng.randint(1, 6)):
        pool = WORDS + SIGNS + TAGS + OPEN
        piece = rng.choice(pool)
        if opened and ("=" in piece or ">" in piece):
            piece = rng.choice(WORDS[:5])
        opened = opened or piece in OPEN
        out.append(piece)
    return rng.choice(("", " ")).join(out)


def flowchart(rng: random.Random) -> str:
    lines = [rng.choice(("flowchart TD", "flowchart LR", "graph LR"))]
    for n in range(rng.randint(1, 8)):
        a, b = f"N{n}", f"N{n + 1}"
        lines.append(
            "  "
            + rng.choice(
                (
                    f'{a}["{label(rng)}"] --> {b}["{label(rng)}"]',
                    f'{a}["{label(rng)}"]-->{b}',
                    f"{a} -->|{label(rng)}| {b}",
                    f"{a} -- {label(rng)} --> {b}",
                    f'{a}("{label(rng)}") ==> {b}{{"{label(rng)}"}}',
                    f"{a} -.-> {b}",
                )
            )
        )
    return "\n".join(lines)


def sequence(rng: random.Random) -> str:
    lines = ["sequenceDiagram"]
    for _ in range(rng.randint(1, 8)):
        lines.append(
            "  "
            + rng.choice(
                (
                    f"alice->>bob: {label(rng)}",
                    f"bob-->>alice: {label(rng)}",
                    f"Note over alice,bob: {label(rng)}",
                )
            )
        )
    return "\n".join(lines)


def test_any_ordinary_diagram_comes_out_byte_for_byte() -> None:
    """Over many generated flowcharts and sequence diagrams with no hostile tag in them, the
    output is the input, byte for byte: a strip that changes a valid diagram can break it (#918)."""
    rng = random.Random(918)
    sources = [rng.choice((flowchart, sequence))(rng) for _ in range(3000)]
    got = clean(*sources)
    changed = [(s, g) for s, g in zip(sources, got, strict=True) if s != g]
    assert not changed, changed[:3]


def test_a_handler_in_an_ordinary_diagram_still_goes_and_nothing_else_does() -> None:
    """The same diagrams with a hostile tag in a label lose just its handler attribute."""
    rng = random.Random(917)
    hostile = "<img src=x onerror=alert(1)>"
    sources = [f'{flowchart(rng)}\n  H["{label(rng)} {hostile}"] --> N0' for _ in range(300)]
    sources += [f"{sequence(rng)}\n  alice->>bob: {hostile} {label(rng)}" for _ in range(300)]
    want = [s.replace(hostile, "<img src=x >") for s in sources]
    assert clean(*sources) == want


def test_a_very_long_line_is_read_once() -> None:
    """One 200 KB line of quoted attributes in a tag that never closes is read in one pass: the
    first clean-up looked for the line's end again after every quoted value, so a line of n
    attributes cost n times its length (#918's review). The harness counts the characters
    indexOf reads, which is where that rescan was; the clock bound is only a backstop."""
    assert NODE is not None
    line = 'flowchart TD\n  A["<img' + ' t="x"' * 33_000 + '"] --> B'
    started = time.monotonic()
    r = subprocess.run(
        [NODE, str(HARNESS), "--count"], input=json.dumps([line]), capture_output=True, text=True, timeout=60
    )
    elapsed = time.monotonic() - started
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["out"] == [line]
    assert got["read"] <= 4 * len(line), got["read"]  # one pass, not one per attribute (~10**9)
    assert elapsed < 30, elapsed
